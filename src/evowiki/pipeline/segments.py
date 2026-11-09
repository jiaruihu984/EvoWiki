from collections.abc import Set as AbstractSet

from pydantic import BaseModel, Field

from evowiki.content.models import BASE_SEGMENT_KINDS, Concept, Segment, SegmentKind
from evowiki.llm.models import LLMRequest
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.report import LenientBatch, StageReport
from evowiki.pipeline.support import (
    DraftSentence,
    build_segment,
    sample_spot_checks,
    segment_id,
    vet_sentences,
)

PURPOSE = "segments_generate"
PROMPT_VERSION = "segments.generate.v1"
REPAIR_PURPOSE = "segments_repair"
REPAIR_PROMPT_VERSION = "segments.repair.v1"
REQUIRED_KINDS = (SegmentKind.ONE_LINE, SegmentKind.DEFINITION)

SYSTEM = (
    "You write canonical wiki segments strictly from the given source passages. You are a "
    "rewriting tool, not a source of facts: every sentence must be supported by the passages "
    "it cites. Output JSON only."
)


class DraftSegment(BaseModel):
    kind: SegmentKind
    sentences: list[DraftSentence] = Field(min_length=1)


class SegmentDraftBatch(LenientBatch):
    segments: list[DraftSegment]


def _prompt(concept: Concept, passages: list) -> str:
    return (
        f"Concept: {concept.id} ({concept.name_en}).\n"
        "Write these segments when the passages support them: one_line (one sentence), "
        "intuition, definition, derivation, example, advanced. one_line and definition are "
        "required. Each sentence lists citations = passage ids that support it. Use only "
        "the passages below; skip a kind rather than invent content.\n"
        "Passages:\n" + "\n".join(f"[{p.id}] {p.text}" for p in passages)
    )


def _repair_prompt(concept: Concept, passages: list, missing: list[SegmentKind]) -> str:
    kinds = ", ".join(k.value for k in missing)
    return (
        f"Concept: {concept.id} ({concept.name_en}).\n"
        f"Write only these segments: {kinds}. one_line is one sentence. Each sentence lists "
        "citations = passage ids that directly support it. Use only the passages below; skip "
        "a kind rather than invent content.\n"
        "Passages:\n" + "\n".join(f"[{p.id}] {p.text}" for p in passages)
    )


def _build_drafts(ctx: StageContext, concept: Concept, drafts: list[DraftSegment],
                  wanted: AbstractSet[SegmentKind], allowed: set[str],
                  report: StageReport, unwanted_note: str) -> list[Segment]:
    seen: set[SegmentKind] = set()
    segments: list[Segment] = []
    for draft in drafts:
        if draft.kind not in wanted or draft.kind in seen:
            report.dropped.append(f"{concept.id}.{draft.kind.value}: {unwanted_note}")
            continue
        seen.add(draft.kind)
        sid = segment_id(concept.id, draft.kind)
        vetted = vet_sentences(ctx, sid, draft.sentences, allowed)
        seg = build_segment(ctx, sid, concept.id, draft.kind, vetted, report)
        if seg is not None:
            segments.append(seg)
    return segments


def generate_base_segments(ctx: StageContext,
                           concept: Concept) -> tuple[list[Segment], StageReport]:
    report = ctx.report("segments")
    passages = ctx.retrieve([concept])
    allowed = {p.id for p in passages}
    request = LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=concept.id,
                         system=SYSTEM, prompt=_prompt(concept, passages))
    out, call = ctx.gateway.call("generator", request, SegmentDraftBatch)
    report.llm_call_ids.append(call.id)
    report.dropped.extend(out.invalid_notes("segments"))
    segments = _build_drafts(ctx, concept, out.segments, BASE_SEGMENT_KINDS, allowed, report,
                             "duplicate or non-base kind")
    missing = [k for k in REQUIRED_KINDS if k not in {s.kind for s in segments}]
    if missing and passages:
        # One targeted follow-up for the missing required kinds, from the same passages (with
        # no passages nothing could be cited, so the call is skipped).
        repair = LLMRequest(purpose=REPAIR_PURPOSE, prompt_version=REPAIR_PROMPT_VERSION,
                            subject=concept.id, system=SYSTEM,
                            prompt=_repair_prompt(concept, passages, missing))
        rout, rcall = ctx.gateway.call("generator", repair, SegmentDraftBatch)
        report.llm_call_ids.append(rcall.id)
        report.dropped.extend(rout.invalid_notes("segments_repair"))
        segments += _build_drafts(ctx, concept, rout.segments, set(missing), allowed, report,
                                  "duplicate or not requested by repair")
    present = {s.kind for s in segments}
    for kind in REQUIRED_KINDS:
        if kind not in present:
            report.dropped.append(f"{concept.id}: missing required kind {kind.value}")
    sample_spot_checks(ctx, segments)
    return segments, report
