"""Perspective scoring (Stage I-B spec §2; decisions D1, D4). dom(p) is the spec-literal share
of A(p) in active_domain; the reviewed framing_phase is a label for readiness and trace only."""

from dataclasses import dataclass
from math import exp

from evowiki.content.models import PUBLISHABLE, Perspective
from evowiki.view.constraints import MAX_PATH_HOPS, mastered, perspective_admissible
from evowiki.view.features import FeatureContext


@dataclass(frozen=True)
class PerspectiveScore:
    segment_id: str
    dom: float
    co: float
    rec: float
    gap: float
    score: float
    n_required: int
    n_presumed: int


@dataclass(frozen=True)
class PerspectiveChoice:
    segment_id: str | None
    reason: str
    scores: tuple[PerspectiveScore, ...]


def admissible_perspectives(c: str, ctx: FeatureContext) -> list[Perspective]:
    """Layer-A candidate set (spec §2.1): the perspective segment is reviewed/publishable AND
    presumes_required ⊆ mastered. Publishability is checked explicitly, not assumed."""
    ok = frozenset(k for k, b in ctx.beliefs.items() if mastered(b))
    return [p for p in ctx.graph.perspectives_for(c)
            if ctx.graph.segment(p.segment_id).review_status in PUBLISHABLE
            and perspective_admissible(p, ok)]


def score_perspective(p: Perspective, ctx: FeatureContext) -> PerspectiveScore:
    w, u = ctx.params, ctx.usage
    a = (set(p.presumes_required) | set(p.presumes_helpful)) - {p.concept_id}
    g = ctx.graph
    dom = (sum(1 for x in a if g.concept(x).domain == u.active_domain) / len(a)
           if a and u.active_domain is not None else 0.0)
    co = sum(u.co_usage_w(p.concept_id, x) for x in a) / len(a) if a else 0.0
    ages = [g for g in (u.age_sessions(x) for x in a) if g is not None]
    rec = sum(exp(-g / w.tau_rec) for g in ages) / len(ages) if ages else 0.0
    helpful = set(p.presumes_helpful)
    gap = (sum(1 for x in helpful if not mastered(ctx.beliefs[x])) / len(helpful)
           if helpful else 0.0)
    score = w.w_dom * dom + w.w_co * co + w.w_rec * rec - w.w_gap * gap
    return PerspectiveScore(p.segment_id, dom, co, rec, gap, score,
                            len(p.presumes_required), len(a))


def _ranked(scores: list[PerspectiveScore]) -> list[PerspectiveScore]:
    return sorted(scores, key=lambda s: (-s.score, s.n_required, s.n_presumed, s.segment_id))


def select_default(c: str, ctx: FeatureContext, current: str | None) -> PerspectiveChoice:
    scores = _ranked([score_perspective(p, ctx) for p in admissible_perspectives(c, ctx)])
    if not scores:
        return PerspectiveChoice(None, "no_admissible", ())
    best = scores[0]
    if best.score < ctx.params.theta_min:
        return PerspectiveChoice(None, "below_theta_min", tuple(scores))
    cur = next((s for s in scores if s.segment_id == current), None)
    if cur is not None and cur.segment_id != best.segment_id and (
            best.score - cur.score < ctx.params.delta_switch):
        return PerspectiveChoice(cur.segment_id, "within_delta_switch", tuple(scores))
    return PerspectiveChoice(best.segment_id, "best", tuple(scores))


def select_context_bridge(c: str, anchor: str, ctx: FeatureContext) -> PerspectiveChoice:
    """Spec §2.3: only candidates whose A(p) contains X or reaches X in ≤ 3 approved hops."""
    g = ctx.graph

    def reaches(p: Perspective) -> bool:
        a = (set(p.presumes_required) | set(p.presumes_helpful)) - {c}
        return anchor in a or any(g.path_supported({anchor, x}, MAX_PATH_HOPS) for x in a)

    scores = _ranked([score_perspective(p, ctx) for p in admissible_perspectives(c, ctx)
                      if reaches(p)])
    if not scores:
        return PerspectiveChoice(None, "no_bridge_candidate", ())
    if scores[0].score < ctx.params.theta_min:
        return PerspectiveChoice(None, "below_theta_min", tuple(scores))
    return PerspectiveChoice(scores[0].segment_id, "best", tuple(scores))
