"""Perspective → relation coverage (Stage I-B spec §4.1): which canonical relations a perspective
explains. Candidates are computed deterministically from published connections; the generator
proposes from them and the verifier (the other role) confirms each one. Only agreed relations
are kept. Runtime never infers coverage."""

import hashlib
from collections.abc import Iterable, Mapping

from pydantic import BaseModel

from evowiki.content.models import Connection, Perspective, ReviewStatus, Segment
from evowiki.content.relations import relation_id
from evowiki.llm.models import LLMRequest
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.report import LenientBatch, StageReport
from evowiki.pipeline.review import ReviewQueue, ReviewTarget, review_fingerprint

COVERS_REASON = "covers_review"
PROPOSE_PURPOSE = "perspective_covers_propose"
PROPOSE_PROMPT_VERSION = "perspective_covers.propose.v1"
VERIFY_PURPOSE = "perspective_covers_verify"
VERIFY_PROMPT_VERSION = "perspective_covers.verify.v2"

PROPOSE_SYSTEM = (
    "You decide which concept relations a perspective explanation makes clear. You get the "
    "explanation and a list of candidate relations, each with the connection text that states "
    "it. Return only candidate relation ids that the explanation itself helps a reader "
    "understand. Output JSON only."
)
VERIFY_SYSTEM = (
    "You independently check, for each listed relation, whether the given perspective "
    "explanation helps a reader understand that relation. Answer covers true or false per "
    "relation id with a short reason. Output JSON only."
)


class CoversProposal(BaseModel):
    relation_ids: list[str]


class CoversVerdict(BaseModel):
    relation_id: str
    covers: bool
    reason: str = ""


class CoversVerdicts(LenientBatch):
    verdicts: list[CoversVerdict]


def covers_candidates(p: Perspective,
                      connections: Iterable[Connection]) -> dict[str, tuple[str, ...]]:
    presumed = (set(p.presumes_required) | set(p.presumes_helpful)) - {p.concept_id}
    best: dict[str, tuple[str, ...]] = {}
    for conn in sorted(connections, key=lambda c: (len(c.path), c.segment_id)):
        a, b = conn.path[0], conn.path[-1]
        if p.concept_id not in (a, b) or (b if a == p.concept_id else a) not in presumed:
            continue
        best.setdefault(relation_id(a, b), tuple(conn.path))
    return dict(sorted(best.items()))


def _cleared(p: Perspective) -> Perspective:
    return p.model_copy(update={"covers_relation_ids": [], "covers_review_status": None})


def review_covers(ctx: StageContext, p: Perspective, seg: Segment,
                  connections: Iterable[Connection],
                  segments: Mapping[str, Segment]) -> tuple[Perspective, StageReport]:
    report = ctx.report("covers")
    conns = list(connections)
    cands = covers_candidates(p, conns)
    if not cands:
        return _cleared(p), report
    by_rel = {relation_id(c.path[0], c.path[-1]): c for c in
              sorted(conns, key=lambda c: (len(c.path), c.segment_id), reverse=True)}
    lines: dict[str, str] = {}
    for rid, path in cands.items():
        text = segments[by_rel[rid].segment_id].body if by_rel[rid].segment_id in segments else ""
        lines[rid] = f"- {rid}: {' -> '.join(path)}. {text}".rstrip()
    body = f"Perspective ({p.framing_label}) on {p.concept_id}:\n{seg.body}\n\n"
    prompt = body + "Candidate relations:\n" + "\n".join(lines.values())
    out, call = ctx.gateway.call("generator", LLMRequest(
        purpose=PROPOSE_PURPOSE, prompt_version=PROPOSE_PROMPT_VERSION, subject=p.segment_id,
        system=PROPOSE_SYSTEM, prompt=prompt + "\nReturn relation_ids."), CoversProposal)
    report.llm_call_ids.append(call.id)
    proposed = [r for r in dict.fromkeys(out.relation_ids) if r in cands]
    report.dropped.extend(f"{p.segment_id}: not a candidate {r}"
                          for r in out.relation_ids if r not in cands)
    if not proposed:
        return _cleared(p), report
    vout, vcall = ctx.gateway.call("verifier", LLMRequest(
        purpose=VERIFY_PURPOSE, prompt_version=VERIFY_PROMPT_VERSION, subject=p.segment_id,
        system=VERIFY_SYSTEM,
        prompt=body + "Relations to check:\n" + "\n".join(lines[r] for r in proposed)),
        CoversVerdicts)
    report.llm_call_ids.append(vcall.id)
    report.dropped.extend(f"{p.segment_id} {n}" for n in vout.invalid_notes("covers"))
    agreed = {v.relation_id for v in vout.verdicts if v.covers}
    kept = [r for r in proposed if r in agreed]
    report.dropped.extend(f"{p.segment_id}: verifier rejected {r}"
                          for r in proposed if r not in agreed)
    if not kept:
        return _cleared(p), report
    out_p = p.model_copy(update={"covers_relation_ids": kept,
                                 "covers_review_status": ReviewStatus.AUTO_VERIFIED})
    ctx.queue.enqueue(ReviewTarget.PERSPECTIVE, p.segment_id, COVERS_REASON,
                      payload={"covers": kept}, spot_check=True,
                      fingerprint=covers_fingerprint(out_p, seg))
    report.created.append(p.segment_id)
    return apply_covers_review(ctx.queue, out_p, seg), report


def perspective_text_fingerprint(p: Perspective, seg: Segment) -> str:
    """A perspective review binds to the perspective metadata AND the perspective text: segment
    ids come from the label slug, so the text can change under the same id."""
    key = f"{review_fingerprint(p)}|{review_fingerprint(seg)}"
    return hashlib.sha256(key.encode()).hexdigest()


def covers_fingerprint(p: Perspective, seg: Segment) -> str:
    """Binds the covers decision. The framing phase is reviewed independently, so it is left
    out: clearing a rejected phase must not invalidate the covers decision (and a perspective
    without a phase hashes exactly as before the phase existed)."""
    return perspective_text_fingerprint(
        p.model_copy(update={"framing_phase": None, "framing_phase_status": None}), seg)


def apply_covers_review(queue: ReviewQueue, p: Perspective, seg: Segment) -> Perspective:
    if not p.covers_relation_ids:
        return _cleared(p)
    resolved = queue.resolve(ReviewTarget.PERSPECTIVE, p.segment_id, p.covers_review_status,
                             fingerprint=covers_fingerprint(p, seg), reason=COVERS_REASON)
    if resolved is None:
        return _cleared(p)
    return p.model_copy(update={"covers_review_status": resolved})
