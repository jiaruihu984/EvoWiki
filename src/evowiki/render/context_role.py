"""Default vs context role (Stage I-B spec §1.4–1.5) and perspective resolution (§2.3).
Context decisions are per request and are NEVER written back to V."""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from evowiki.belief.beta import Belief
from evowiki.content.graph import ContentGraph
from evowiki.content.relations import RelationCatalog
from evowiki.render.plan import PerspectiveSource, RoleSource
from evowiki.view.constraints import (
    MAX_PATH_HOPS,
    mastered,
    persistent_role_allowed,
    perspective_admissible,
)
from evowiki.view.features import FeatureContext, has_reviewed_relation
from evowiki.view.perspective import PerspectiveScore, select_context_bridge
from evowiki.view.roles import Role
from evowiki.view.state import ViewState

if TYPE_CHECKING:
    from evowiki.view.eligibility import EligibilityPolicy


class NavLink(StrEnum):
    DIRECT = "direct"
    PREREQUISITE = "prerequisite"
    CROSS_DOMAIN = "cross_domain"


@dataclass(frozen=True)
class NavContext:
    from_concept: str
    link: NavLink


@dataclass(frozen=True)
class RoleDecision:
    """`requested_anchor` is the anchor of the requested context role (the navigation source),
    None for a default decision; it survives a context fallback (final-review ruling I2), so
    the trace keeps what was asked for even when the page is a default page."""
    default: Role
    default_anchor: str | None
    requested: Role
    effective: Role
    anchor: str | None
    source: RoleSource
    reason: str
    requested_anchor: str | None = None


@dataclass(frozen=True)
class PerspectiveDecision:
    default: str | None
    effective: str | None
    source: PerspectiveSource
    reason: str
    scores: tuple[PerspectiveScore, ...]


def resolve_role(c: str, view: ViewState, nav: NavContext | None, graph: ContentGraph,
                 relations: RelationCatalog) -> RoleDecision:
    cv = view.concept(c)
    d_role, d_anchor = cv.role or Role.LEARN, cv.role_anchor

    def default(reason: str) -> RoleDecision:
        return RoleDecision(d_role, d_anchor, d_role, d_role, d_anchor, RoleSource.DEFAULT,
                            reason)

    if nav is None or nav.link is NavLink.DIRECT or nav.from_concept == c:
        return default("default")
    x = nav.from_concept
    if nav.link is NavLink.PREREQUISITE and (
            c in graph.prerequisite_closure(x, MAX_PATH_HOPS)
            or has_reviewed_relation(relations, c, x)):
        return RoleDecision(d_role, d_anchor, Role.SUPPORT, Role.SUPPORT, x, RoleSource.CONTEXT,
                            "context_support", requested_anchor=x)
    if nav.link is NavLink.CROSS_DOMAIN and (
            has_reviewed_relation(relations, c, x) or graph.path_supported({c, x}, MAX_PATH_HOPS)):
        return RoleDecision(d_role, d_anchor, Role.BRIDGE, Role.BRIDGE, x, RoleSource.CONTEXT,
                            "context_bridge", requested_anchor=x)
    return default("context_unsupported")


def resolve_perspective(c: str, role: RoleDecision, view: ViewState,
                        fctx: FeatureContext) -> PerspectiveDecision:
    ok = frozenset(k for k, b in fctx.beliefs.items() if mastered(b))
    pid = view.concept(c).perspective_segment_id
    by_id = {p.segment_id: p for p in fctx.graph.perspectives_for(c)}
    default = pid if pid in by_id and perspective_admissible(by_id[pid], ok) else None
    if role.source is RoleSource.CONTEXT and role.requested is Role.BRIDGE:
        ch = select_context_bridge(c, role.anchor or "", fctx)
        src = PerspectiveSource.CONTEXT if ch.segment_id else PerspectiveSource.NONE
        return PerspectiveDecision(default, ch.segment_id, src, ch.reason, ch.scores)
    if default is None:
        return PerspectiveDecision(None, None, PerspectiveSource.NONE,
                                   "ineligible_at_render" if pid else "no_default", ())
    return PerspectiveDecision(default, default, PerspectiveSource.DEFAULT, "default", ())


def finalize(c: str, role: RoleDecision, persp: PerspectiveDecision, view: ViewState,
             fctx: FeatureContext, *, anchor_connection_available: bool
             ) -> tuple[RoleDecision, PerspectiveDecision]:
    """Joint finalize (decision R3). Only a context role can fall back; it then restores the
    persistent default role AND recomputes the default perspective, so no context-derived
    state leaks into a default page. A persistent role is never swapped: missing content is
    skipped with a reason by the template (spec §1.3)."""
    if role.source is not RoleSource.CONTEXT:
        return role, persp
    ok = {Role.SUPPORT: anchor_connection_available,
          Role.BRIDGE: anchor_connection_available or persp.effective is not None}.get(
        role.requested, True)
    if ok:
        return role, persp
    fallback = replace(role, effective=role.default, anchor=role.default_anchor,
                       reason=MISSING_SUPPORTED_CONTENT)
    default_role = RoleDecision(role.default, role.default_anchor, role.default, role.default,
                                role.default_anchor, RoleSource.DEFAULT, "default")
    return fallback, resolve_perspective(c, default_role, view, fctx)


MISSING_SUPPORTED_CONTENT = "missing_supported_content"
INELIGIBLE_AT_RENDER = "ineligible_at_render"


def enforce_persistent_eligibility(role: RoleDecision, belief: Belief, *,
                                   policy: "EligibilityPolicy | None" = None,
                                   concept: str | None = None,
                                   graph: ContentGraph | None = None,
                                   relations: RelationCatalog | None = None) -> RoleDecision:
    """Spec §1.4 (layer A), final-review ruling I3: a persistent support/reference/bridge role
    needs mastered(c); otherwise the persistent role can only be learn. Applied at render after
    `finalize`, to roles that came from the default path (source DEFAULT, or a context fallback
    onto the default): an ineligible one renders as LEARN with anchor None and reason
    ineligible_at_render. `requested` keeps V's role; V is never written. A granted context
    role needs no mastery and is returned unchanged.

    Plan 4b-1d: with an amended `policy` (θ_rep²), support/bridge need a grounded anchor
    instead of mastery (reference still needs mastered); without one, the v1 invariant."""
    from_default = (role.source is RoleSource.DEFAULT
                    or role.reason == MISSING_SUPPORTED_CONTENT)
    if not from_default:
        return role
    if policy is None or not policy.amended:
        ok = persistent_role_allowed(role.effective, belief)
    else:
        if concept is None or graph is None or relations is None:
            raise ValueError("an amended eligibility policy needs concept, graph and relations")
        ok = policy.persistent_ok(role.effective, belief, concept=concept, anchor=role.anchor,
                                  graph=graph, relations=relations)
    if ok:
        return role
    return replace(role, effective=Role.LEARN, anchor=None, reason=INELIGIBLE_AT_RENDER)
