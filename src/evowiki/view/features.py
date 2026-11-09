"""θ_rep¹ feature vocabulary (Stage I-B spec §3 B layer; design decision D2 for downstream).
Inputs: content (graph + reviewed relation catalog), K (beliefs), observable usage. Never V."""

from dataclasses import dataclass, fields
from datetime import datetime
from itertools import combinations
from math import exp

from evowiki.belief.beta import Belief
from evowiki.content.graph import ContentGraph
from evowiki.content.relations import RelationCatalog, relation_id
from evowiki.rep.theta import POLICY_VERSION_V5, POLICY_VERSION_V6, RepParams
from evowiki.view.constraints import MAX_PATH_HOPS, WINDOW_SESSIONS, compressible, mastered
from evowiki.view.usage import UsageIndex


@dataclass(frozen=True)
class FeatureContext:
    graph: ContentGraph
    relations: RelationCatalog
    beliefs: dict[str, Belief]
    usage: UsageIndex
    now: datetime
    params: RepParams
    # R2 (θ_rep@v5): bridge co-usage = co-location of the pair's latest uses (see
    # `_cross_candidates`). False for every other policy version and every legacy caller.
    bridge_colocation: bool = False


def bridge_colocation(policy_version: str) -> bool:
    """R2 is active under θ_rep@v5 and θ_rep@v6 (= R2 + the R2.1 bridge lease)."""
    return policy_version in (POLICY_VERSION_V5, POLICY_VERSION_V6)


@dataclass(frozen=True)
class ConceptFeatures:
    m: float
    u: float
    R: float
    mastered: float
    compressible: float
    domain_active: float
    co_usage_cross: float
    has_grounded_bridge: float
    downstream_active: float
    used_in_window: float
    sessions_since_seen: float
    user_accept_rate: float
    cohesion: float
    best_bridge_anchor: str | None
    best_support_anchor: str | None

    def value(self, name: str) -> float:
        v = getattr(self, name)
        if not isinstance(v, float):
            raise KeyError(name)
        return v

    def as_dict(self) -> dict[str, float | str | None]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def has_reviewed_relation(relations: RelationCatalog, a: str, b: str) -> bool:
    if a == b:
        return False
    try:
        relations.get(relation_id(a, b))
    except KeyError:
        return False
    return True


def downstream(c: str, ctx: FeatureContext) -> frozenset[str]:
    """D2: directional approved evidence only — concepts reachable from c over approved
    prerequisite_of edges. An undirected relation never creates direction."""
    return frozenset(ctx.graph.descendants(c))


def supportable_downstream(c: str, ctx: FeatureContext) -> list[str]:
    """Active downstream concepts that also have a reviewed relation (hence a primary connection
    segment) with c: exactly the anchors a support(X) page can render (D2)."""
    return sorted(d for d in downstream(c, ctx)
                  if ctx.usage.used_in_window(d) and has_reviewed_relation(ctx.relations, c, d))


def _rec(ctx: FeatureContext, x: str) -> float:
    age = ctx.usage.age_sessions(x)
    return 0.0 if age is None else exp(-age / ctx.params.tau_rec)


def _argmax_smallest_id(cands: list[str], key) -> str | None:
    """Highest key; ties go to the lexicographically smallest id."""
    best = None
    for x in sorted(cands):
        if best is None or key(x) > key(best):
            best = x
    return best


def cohesion(group: frozenset[str], ctx: FeatureContext) -> float:
    pairs = list(combinations(sorted(group), 2))
    return sum(ctx.usage.co_usage_w(a, b) for a, b in pairs) / len(pairs) if pairs else 0.0


def group_m(group: frozenset[str], ctx: FeatureContext) -> float:
    return min(ctx.beliefs[c].mastery for c in group)


def candidate_groups(ctx: FeatureContext, min_size: int) -> list[frozenset[str]]:
    recent = ctx.usage.recent()
    seen: set[frozenset[str]] = set()
    for c in sorted(recent):
        grp = frozenset({c} | {d for d in recent if d != c and ctx.graph.adjacent(c, d)})
        if len(grp) >= min_size:
            seen.add(grp)
    return sorted(seen, key=lambda s: tuple(sorted(s)))


def colocated(c: str, x: str, usage: UsageIndex) -> bool:
    """R2: the latest uses of c and x fall within one usage window of each other."""
    lc, lx = usage.last_use(c), usage.last_use(x)
    return lc is not None and lx is not None and abs(lc - lx) <= WINDOW_SESSIONS - 1


def _cross_candidates(c: str, ctx: FeatureContext) -> list[str]:
    """Cross-domain related partners of c (reviewed relation or path-supported). θ_rep ≤ v4:
    partners used in the current usage window. R2 (v5): partners co-located with c."""
    u, g = ctx.usage, ctx.graph
    dom = g.concept(c).domain
    pool = (g.concept_ids if ctx.bridge_colocation else u.recent())
    return [x for x in pool if x != c and g.concept(x).domain != dom
            and (not ctx.bridge_colocation or colocated(c, x, u))
            and (has_reviewed_relation(ctx.relations, c, x)
                 or g.path_supported({c, x}, MAX_PATH_HOPS))]


def _best_bridge_partner(c: str, cross: list[str], ctx: FeatureContext) -> str | None:
    """Ranking unchanged by R2 (reviewer ruling D2): same-window co-usage, recency, id."""
    return _argmax_smallest_id(cross, lambda x: (ctx.usage.co_usage_w(c, x), _rec(ctx, x)))


def bridge_partner_blocked(c: str, ctx: FeatureContext) -> bool:
    """Instrumentation (R2 ruling D2): the best cross partner has no reviewed relation (so no
    grounded bridge) although a reviewed cross partner is among the candidates. Ranking is
    deliberately not changed; this only counts the cases."""
    cross = _cross_candidates(c, ctx)
    best = _best_bridge_partner(c, cross, ctx)
    return (best is not None and not has_reviewed_relation(ctx.relations, c, best)
            and any(has_reviewed_relation(ctx.relations, c, x) for x in cross))


def bridge_partner_audit(c: str, ctx: FeatureContext) -> tuple[str | None, tuple[str, ...]]:
    """Read-only instrumentation detail (R2 dev recorder): c's best cross partner under the
    unchanged ranking, and every reviewed cross candidate of c (sorted). Changes nothing."""
    cross = _cross_candidates(c, ctx)
    return (_best_bridge_partner(c, cross, ctx),
            tuple(sorted(x for x in cross if has_reviewed_relation(ctx.relations, c, x))))


def concept_features(c: str, ctx: FeatureContext) -> ConceptFeatures:
    b, u, g = ctx.beliefs[c], ctx.usage, ctx.graph
    dom = g.concept(c).domain
    cross = _cross_candidates(c, ctx)
    best_x = _best_bridge_partner(c, cross, ctx)
    down = supportable_downstream(c, ctx)
    best_s = _argmax_smallest_id(down, lambda d: u.co_usage_w(c, d) + _rec(ctx, d))
    return ConceptFeatures(
        m=b.mastery, u=b.uncertainty, R=b.recall(ctx.now),
        mastered=float(mastered(b)), compressible=float(compressible(b, ctx.now)),
        domain_active=float(u.active_domain == dom),
        # R2: co-location is binary (τ_co unchanged: ≥ τ_co ⟺ co-located)
        co_usage_cross=(float(best_x is not None) if ctx.bridge_colocation
                        else u.co_usage_w(c, best_x) if best_x else 0.0),
        has_grounded_bridge=float(best_x is not None
                                  and has_reviewed_relation(ctx.relations, c, best_x)),
        downstream_active=float(len(down)),
        used_in_window=float(u.used_in_window(c)),
        sessions_since_seen=float(u.sessions_since_seen(c)),
        user_accept_rate=u.accept_rate,
        cohesion=0.0,
        best_bridge_anchor=best_x, best_support_anchor=best_s)
