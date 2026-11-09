import re
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, Field

from evowiki.content.models import (
    Concept,
    Passage,
    Perspective,
    PrereqKind,
    ReviewStatus,
    Segment,
    SegmentKind,
)
from evowiki.llm.models import LLMRequest
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.covers import perspective_text_fingerprint
from evowiki.pipeline.edgeview import EdgeView
from evowiki.pipeline.linking import concept_names, link_concepts
from evowiki.pipeline.report import LenientBatch, StageReport
from evowiki.pipeline.review import ReviewQueue, ReviewTarget
from evowiki.pipeline.support import (
    DraftSentence,
    build_segment,
    sample_spot_checks,
    segment_id,
    vet_sentences,
)

PURPOSE = "perspectives_generate"
PROMPT_VERSION = "perspectives.generate.v3"
# Versions the passage selection fed to the generator; sent in LLMRequest.params, so it is part
# of the replay input hash. bridge-v1: focal top-k plus passages that jointly mention the
# focal concept and one grounded neighbour (see docs/content-pipeline.md).
RETRIEVAL_VERSION = "perspectives.retrieval.bridge-v1"
BRIDGE_PER_NEIGHBOUR = 2
PRESUMES_PURPOSE = "presumes_verify"
PRESUMES_PROMPT_VERSION = "presumes.verify.v1"
PHASE_VERIFY_PURPOSE = "perspective_phase_verify"
PHASE_VERIFY_PROMPT_VERSION = "perspective_phase.verify.v1"
PHASE_REASON = "framing_phase_review"
Phase = Literal["LA", "ML", "DL"]
PHASE_DEFINITIONS = (
    "LA = linear algebra / probability-statistics view, ML = classical machine-learning view, "
    "DL = deep-learning / representation-learning view"
)
MAX_PERSPECTIVES = 4

SYSTEM = (
    "You identify distinct ways of understanding a concept (perspectives) that the given "
    "passages actually present, e.g. 'variance maximization' vs 'best low-rank reconstruction'. "
    "Prefer perspectives that frame the concept through different neighbouring fields (for "
    "example a linear-algebra framing, a machine-learning framing and a deep-learning framing) "
    "whenever the passages support each one; never invent a framing the passages do not "
    "present. Every perspective states its framing_phase: the field of study through which it "
    f"frames the concept ({PHASE_DEFINITIONS}). When the passages support it, give "
    "perspectives in different phases. Write each as short cited sentences using only the "
    "passages. Output JSON only."
)
PRESUMES_SYSTEM = (
    "You judge which prerequisite concepts a reader must already understand to follow a "
    "given explanation. Answer per candidate: required, helpful, or not_needed. Output JSON only."
)
PHASE_VERIFY_SYSTEM = (
    "You independently judge through which field of study an explanation frames a concept "
    f"({PHASE_DEFINITIONS}). Give the phase the explanation itself uses, whether you agree "
    "with the proposed phase, and a short reason. Output JSON only."
)


class DraftPerspective(BaseModel):
    framing_label: str = Field(min_length=1)
    framing_phase: Phase
    sentences: list[DraftSentence] = Field(min_length=1)


class PerspectiveDraftBatch(LenientBatch):
    perspectives: list[DraftPerspective]


class PresumeVerdict(BaseModel):
    concept_id: str
    level: Literal["required", "helpful", "not_needed"]


class PresumeVerdicts(LenientBatch):
    verdicts: list[PresumeVerdict]


class PhaseVerdict(BaseModel):
    phase: Phase
    agrees: bool
    reason: str = ""


def slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "view"


def presume_candidates(concept_id: str, mentioned: set[str], view: EdgeView) -> set[str]:
    return (view.prereq_closure(mentioned | {concept_id}, 2, PrereqKind.FORMAL)
            - mentioned - {concept_id})


def derive_presumes(concept_id: str, mentioned: set[str], view: EdgeView,
                    verdicts: Mapping[str, str]) -> tuple[list[str], list[str]]:
    required = set(mentioned) - {concept_id}
    candidates = presume_candidates(concept_id, required, view)
    required |= {c for c in candidates if verdicts.get(c) == "required"}
    helpful = {c for c in candidates if verdicts.get(c) == "helpful"}
    helpful |= {p for c in required | {concept_id}
                for p in view.direct_prereqs(c, PrereqKind.PEDAGOGICAL)}
    helpful -= required | {concept_id}
    return sorted(required), sorted(helpful)


def uncovered_mentions(body: str, concepts: Iterable[Concept], p: Perspective) -> set[str]:
    covered = set(p.presumes_required) | set(p.presumes_helpful) | {p.concept_id}
    return link_concepts(body, concepts) - covered


def _neighbours(concept_id: str, view: EdgeView) -> list[str]:
    related = {b if a == concept_id else a for a, b in view.related_pairs()
               if concept_id in (a, b)}
    return sorted(related | view.direct_prereqs(concept_id)
                  | {c for c in view.prereq_closure({concept_id}, 1) if c != concept_id})


def bridging_passages(ctx: StageContext, concept: Concept, neighbours: list[Concept],
                      per_neighbour: int = BRIDGE_PER_NEIGHBOUR) -> list[Passage]:
    """Per neighbour (in the given order), the top `per_neighbour` passages of the context's
    eligible pool that mention both the focal concept and that neighbour, ranked by the
    index's lexical score over the two concepts' names. Deduplicated, first occurrence kept."""
    out: dict[str, Passage] = {}
    for n in neighbours:
        phrases = [*concept_names(concept), *concept_names(n)]
        ranked = ctx.index.search(phrases, k=len(ctx.passages))
        joint = [p for p in ranked
                 if {concept.id, n.id} <= link_concepts(p.text, [concept, n])]
        for p in joint[:per_neighbour]:
            out.setdefault(p.id, p)
    return list(out.values())


def perspective_passages(ctx: StageContext, concept: Concept, neighbours: list[Concept],
                         per_neighbour: int = BRIDGE_PER_NEIGHBOUR) -> list[Passage]:
    """Focal evidence (plain top-k, in order) followed by bridging passages not already
    present, with at most 2 * len(neighbours) bridging additions."""
    merged = {p.id: p for p in ctx.retrieve([concept, *neighbours])}
    added = 0
    for p in bridging_passages(ctx, concept, neighbours, per_neighbour):
        if added >= 2 * len(neighbours):
            break
        if p.id not in merged:
            merged[p.id] = p
            added += 1
    return list(merged.values())


def _prompt(concept: Concept, passages: list, neighbours: list[Concept]) -> str:
    near = "\n".join(f"- {c.id} ({c.name_en}, domain {c.domain})" for c in neighbours)
    return (
        f"Concept: {concept.id} ({concept.name_en}, domain {concept.domain}).\n"
        f"Neighbouring concepts:\n{near}\n"
        f"List up to {MAX_PERSPECTIVES} perspectives presented by these passages. For each: "
        "framing_label (a few words), framing_phase (LA, ML or DL: the field of study through "
        f"which it frames the concept; {PHASE_DEFINITIONS}) and sentences with citations "
        "(passage ids).\n"
        "When the passages support it, give perspectives in different phases. If a "
        "neighbouring concept is from deep learning (e.g. autoencoder, "
        "representation_learning), explicitly look for a representation-learning / autoencoder "
        "framing. Never invent a framing the passages do not present.\n"
        "Passages:\n" + "\n".join(f"[{p.id}] {p.text}" for p in passages)
    )


def _presumes_prompt(seg: Segment, candidates: set[str], ctx: StageContext) -> str:
    lines = "\n".join(f"- {c}: {ctx.concepts[c].name_en}" for c in sorted(candidates))
    return (f"Explanation:\n{seg.body}\n\nCandidate prerequisite concepts:\n{lines}\n"
            "Return verdicts with concept_id and level.")


def generate_perspectives(
    ctx: StageContext, concept: Concept, view: EdgeView
) -> tuple[list[Segment], list[Perspective], StageReport]:
    report = ctx.report("perspectives")
    neighbours = [ctx.concepts[c] for c in _neighbours(concept.id, view) if c in ctx.concepts]
    passages = perspective_passages(ctx, concept, neighbours)
    allowed = {p.id for p in passages}
    request = LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=concept.id,
                         system=SYSTEM, prompt=_prompt(concept, passages, neighbours),
                         params={"retrieval": RETRIEVAL_VERSION})
    out, call = ctx.gateway.call("generator", request, PerspectiveDraftBatch)
    report.llm_call_ids.append(call.id)
    report.dropped.extend(out.invalid_notes("perspectives"))
    segments: list[Segment] = []
    perspectives: list[Perspective] = []
    for draft in out.perspectives[:MAX_PERSPECTIVES]:
        sid = segment_id(concept.id, SegmentKind.PERSPECTIVE, slug=slugify(draft.framing_label))
        if any(s.id == sid for s in segments):
            report.dropped.append(f"{sid}: duplicate framing")
            continue
        vetted = vet_sentences(ctx, sid, draft.sentences, allowed)
        seg = build_segment(ctx, sid, concept.id, SegmentKind.PERSPECTIVE, vetted, report)
        if seg is None:
            continue
        mentioned = link_concepts(seg.body, ctx.concepts.values()) - {concept.id}
        candidates = presume_candidates(concept.id, mentioned, view)
        verdicts: dict[str, str] = {}
        if candidates:
            preq = LLMRequest(purpose=PRESUMES_PURPOSE, prompt_version=PRESUMES_PROMPT_VERSION,
                              subject=sid, system=PRESUMES_SYSTEM,
                              prompt=_presumes_prompt(seg, candidates, ctx))
            pout, pcall = ctx.gateway.call("verifier", preq, PresumeVerdicts)
            report.llm_call_ids.append(pcall.id)
            report.dropped.extend(f"{sid} {n}" for n in pout.invalid_notes("presumes"))
            verdicts = {v.concept_id: v.level for v in pout.verdicts if v.concept_id in candidates}
        required, helpful = derive_presumes(concept.id, mentioned, view, verdicts)
        persp = Perspective(segment_id=sid, concept_id=concept.id,
                            framing_label=draft.framing_label, presumes_required=required,
                            presumes_helpful=helpful)
        missing = uncovered_mentions(seg.body, ctx.concepts.values(), persp)
        if missing:
            report.dropped.append(f"{sid}: mentions not covered by presumes {sorted(missing)}")
            continue
        segments.append(seg)
        perspectives.append(review_phase(ctx, concept, persp, seg, draft.framing_phase, report))
    sample_spot_checks(ctx, segments)
    return segments, perspectives, report


def _phase_cleared(p: Perspective) -> Perspective:
    return p.model_copy(update={"framing_phase": None, "framing_phase_status": None})


def review_phase(ctx: StageContext, concept: Concept, p: Perspective, seg: Segment,
                 proposed: Phase, report: StageReport) -> Perspective:
    """The verifier independently checks the generator's framing phase. Agreement keeps it as
    auto_verified with a human spot-check; anything else leaves the perspective without a
    phase (fail closed)."""
    vout, vcall = ctx.gateway.call("verifier", LLMRequest(
        purpose=PHASE_VERIFY_PURPOSE, prompt_version=PHASE_VERIFY_PROMPT_VERSION,
        subject=p.segment_id, system=PHASE_VERIFY_SYSTEM,
        prompt=(f"Concept: {concept.id} ({concept.name_en}).\n"
                f"Perspective ({p.framing_label}), proposed framing_phase {proposed}:\n"
                f"{seg.body}\n\nReturn phase, agrees and reason.")), PhaseVerdict)
    report.llm_call_ids.append(vcall.id)
    if not (vout.agrees and vout.phase == proposed):
        report.dropped.append(f"{p.segment_id}: verifier rejected framing_phase {proposed} "
                              f"(verifier phase {vout.phase}: {vout.reason})")
        return _phase_cleared(p)
    out = p.model_copy(update={"framing_phase": proposed,
                               "framing_phase_status": ReviewStatus.AUTO_VERIFIED})
    ctx.queue.enqueue(ReviewTarget.PERSPECTIVE, p.segment_id, PHASE_REASON,
                      payload={"framing_phase": proposed}, spot_check=True,
                      fingerprint=phase_fingerprint(out, seg))
    return apply_phase_review(ctx.queue, out, seg)


def phase_fingerprint(p: Perspective, seg: Segment) -> str:
    """Binds the phase decision to the perspective and its text. Covers are reviewed
    independently (and later), so they are left out."""
    return perspective_text_fingerprint(
        p.model_copy(update={"covers_relation_ids": [], "covers_review_status": None}), seg)


def apply_phase_review(queue: ReviewQueue, p: Perspective, seg: Segment) -> Perspective:
    if p.framing_phase is None:
        return _phase_cleared(p)
    resolved = queue.resolve(ReviewTarget.PERSPECTIVE, p.segment_id, p.framing_phase_status,
                             fingerprint=phase_fingerprint(p, seg), reason=PHASE_REASON)
    if resolved is None:
        return _phase_cleared(p)
    return p.model_copy(update={"framing_phase_status": resolved})
