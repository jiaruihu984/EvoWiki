from collections.abc import Mapping
from dataclasses import dataclass, field

from evowiki.belief.beta import Belief
from evowiki.belief.theta import ThetaInfer
from evowiki.content.graph import ContentGraph
from evowiki.evidence.models import RECALL_TYPES, Evidence, EvidenceType, Polarity


@dataclass
class UpdateResult:
    updated: dict[str, Belief]
    ambiguous: set[str] = field(default_factory=set)


def apply_evidence(
    beliefs: Mapping[str, Belief], ev: Evidence, graph: ContentGraph, theta: ThetaInfer
) -> UpdateResult:
    out = dict(beliefs)
    strength = theta.weight(ev.evidence_type) * ev.extraction_confidence
    if strength <= 0.0:
        return UpdateResult(out)

    fg = theta.forgetting
    b = out[ev.concept_id]
    alpha, beta, stab, last = b.alpha, b.beta, b.stability, b.last_recall_ts
    positive = ev.polarity is Polarity.POSITIVE

    if ev.evidence_type is EvidenceType.SELF_REPORT_FORGETTING:
        out[ev.concept_id] = b.model_copy(update={
            "stability": max(fg.S_min, stab * fg.shrink**strength),
            "evidence_count": b.evidence_count + 1,
        })
        return UpdateResult(out)

    if positive:
        alpha += strength
    else:
        beta += strength * b.recall(ev.ts)
    if ev.evidence_type in RECALL_TYPES:
        if positive:
            stab = min(fg.S_max, stab * fg.growth**strength)
        else:
            stab = max(fg.S_min, stab * fg.shrink**strength)
        last = ev.ts

    out[ev.concept_id] = Belief(
        alpha=alpha, beta=beta, stability=stab, last_recall_ts=last,
        evidence_count=b.evidence_count + 1,
    )

    prereqs = graph.direct_prerequisites(ev.concept_id)
    if not positive:
        return UpdateResult(out, {p for p, _ in prereqs})

    for p, kind in prereqs:
        pb = out[p]
        out[p] = pb.model_copy(
            update={"alpha": pb.alpha + theta.up_alpha.for_kind(kind) * strength}
        )
    return UpdateResult(out)
