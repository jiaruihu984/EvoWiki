from collections.abc import Mapping

from evowiki.belief.beta import Belief
from evowiki.belief.theta import ThetaInfer
from evowiki.content.graph import ContentGraph


def prior_beliefs(graph: ContentGraph, theta: ThetaInfer, background: str) -> dict[str, Belief]:
    domain_priors = theta.prior_by_background[background]
    out: dict[str, Belief] = {}
    for cid in graph.concept_ids:
        c = graph.concept(cid)
        pr = domain_priors.get(c.domain, theta.default_prior)
        out[cid] = Belief(
            alpha=pr.mean * pr.strength,
            beta=(1.0 - pr.mean) * pr.strength,
            stability=theta.forgetting.S0_by_difficulty[c.difficulty],
            last_recall_ts=None,
        )
    return out


def select_diagnostic_concepts(
    beliefs: Mapping[str, Belief], graph: ContentGraph, n: int, exclude: set[str] = frozenset()
) -> list[str]:
    scored = [
        (-(beliefs[cid].uncertainty * (1 + graph.downstream_count(cid))), cid)
        for cid in graph.concept_ids
        if cid not in exclude
    ]
    scored.sort()
    return [cid for _, cid in scored[:n]]
