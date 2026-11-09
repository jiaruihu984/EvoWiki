from itertools import pairwise

from pydantic import BaseModel, Field

from evowiki.content.models import Connection, Segment, SegmentKind
from evowiki.llm.models import LLMRequest
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.edgeview import EdgeView
from evowiki.pipeline.linking import mentions
from evowiki.pipeline.report import StageReport
from evowiki.pipeline.review import ReviewTarget
from evowiki.pipeline.support import (
    DraftSentence,
    build_segment,
    sample_spot_checks,
    vet_sentences,
)

PURPOSE = "connections_generate"
PROMPT_VERSION = "connections.generate.v1"

SYSTEM = (
    "You explain how consecutive concepts on a path relate, strictly from the given passages. "
    "Every sentence cites passage ids; for every hop list the passage ids that support that "
    "hop. Output JSON only."
)


class DraftConnection(BaseModel):
    sentences: list[DraftSentence] = Field(min_length=1)
    hop_citations: list[list[str]]


def connection_segment_id(path: list[str]) -> str:
    return "conn." + ".".join(path) + ".en"


def connection_targets(view: EdgeView,
                       curated_paths: list[list[str]]) -> tuple[list[list[str]], list[str]]:
    targets = [list(p) for p in view.related_pairs()]
    invalid: list[str] = []
    for path in curated_paths:
        if not view.path_valid(path):
            invalid.append(f"{'->'.join(path)}: not a valid <=3-hop path over "
                           "prerequisite_of/related_to")
        elif list(path) not in targets and sorted(path) not in targets:
            targets.append(list(path))
    return targets, invalid


def _prompt(ctx: StageContext, path: list[str], passages: list) -> str:
    names = " -> ".join(f"{c} ({ctx.concepts[c].name_en})" for c in path)
    return (
        f"Path: {names}\n"
        f"Write 1-4 sentences explaining how each consecutive pair relates. Return sentences "
        f"(text, citations) and hop_citations: a list of {len(path) - 1} lists of passage ids, "
        "one per hop in order.\nPassages:\n" + "\n".join(f"[{p.id}] {p.text}" for p in passages)
    )


def generate_connection(
    ctx: StageContext, path: list[str]
) -> tuple[Segment | None, Connection | None, StageReport]:
    report = ctx.report("connections")
    sid = connection_segment_id(path)
    passages = ctx.index.for_concepts([ctx.concepts[c] for c in path],
                                      k=ctx.retrieval_k * 2)
    allowed = {p.id for p in passages}
    request = LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION,
                         subject="->".join(path), system=SYSTEM,
                         prompt=_prompt(ctx, path, passages))
    out, call = ctx.gateway.call("generator", request, DraftConnection)
    report.llm_call_ids.append(call.id)
    if len(out.hop_citations) != len(path) - 1:
        report.dropped.append(f"{sid}: hop citation count mismatch")
        return None, None, report
    vetted = vet_sentences(ctx, sid, out.sentences, allowed)
    kept = {c for s in vetted.kept for c in s.citations}
    for (a, b), hop in zip(pairwise(path), out.hop_citations, strict=True):
        ca, cb = ctx.concepts[a], ctx.concepts[b]
        if not any(pid in kept and mentions(ctx.passages[pid].text, ca)
                   and mentions(ctx.passages[pid].text, cb) for pid in hop):
            report.llm_call_ids.extend(vetted.llm_call_ids)
            report.dropped.extend(f"{sid} {n}" for n in vetted.notes)
            report.dropped.append(f"{sid}: hop {a}-{b} lacks a supporting citation")
            return None, None, report
    seg = build_segment(ctx, sid, path[0], SegmentKind.CONNECTION, vetted, report,
                        target=ReviewTarget.CONNECTION)
    if seg is None:
        return None, None, report
    sample_spot_checks(ctx, [seg], target=ReviewTarget.CONNECTION)
    return seg, Connection(segment_id=sid, path=list(path)), report
