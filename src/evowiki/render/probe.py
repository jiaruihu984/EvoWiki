"""Active knowledge tracing probe (spec §5.1 step 4)."""

from collections.abc import Set as AbstractSet
from datetime import datetime

from evowiki.belief.beta import predict_correct
from evowiki.belief.theta import ThetaInfer
from evowiki.belief.tracer import BeliefState
from evowiki.content.graph import ContentGraph
from evowiki.render.plan import Probe
from evowiki.render.policy import decision_flips
from evowiki.render.theta import ThetaRender


def choose_probe(
    concept_id: str,
    state: BeliefState,
    graph: ContentGraph,
    theta_rep: ThetaRender,
    theta_infer: ThetaInfer,
    now: datetime,
    probes_used: int,
    asked_item_ids: AbstractSet[str] = frozenset(),
) -> Probe | None:
    """Pick at most one quiz item on a direct prerequisite whose treatment is in doubt.

    Candidates have u > θ_infer.probe.uncertainty_threshold and a treatment that flips within
    ±1σ. Attribution-ambiguous prerequisites come first, then higher u, then concept id. The
    item must test only that prerequisite; human-verified items win, then lowest id.
    """
    policy = theta_infer.probe
    if probes_used >= policy.max_per_session:
        return None
    ranked: list[tuple[bool, float, str]] = []
    for p, kind in graph.direct_prerequisites(concept_id):
        b = state.beliefs[p]
        if b.uncertainty <= policy.uncertainty_threshold:
            continue
        if not decision_flips(kind, b, now, theta_rep.refresher):
            continue
        ranked.append((p not in state.ambiguous, -b.uncertainty, p))
    for _, _, p in sorted(ranked):
        items = [
            q for q in graph.items_for_concept(p)
            if q.concept_ids == [p] and q.id not in asked_item_ids
        ]
        if not items:
            continue
        item = min(items, key=lambda q: (not q.answer_verified_by_human, q.id))
        p_correct = predict_correct([state.beliefs[p]], now, theta_infer)
        return Probe(item_id=item.id, concept_id=p, predicted_p_correct=p_correct)
    return None
