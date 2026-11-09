"""Persistent-role eligibility by policy version (Plan 4b-1d Task 3).

θ_rep¹ (`theta_rep@v1`): the original layer-A invariant (spec §1.4) — support / reference /
bridge need mastered(c) — with usage-v1. θ_rep² (`theta_rep@v2`): the amended invariant
(4b-1d design §2) — support(X) / bridge(X) need a grounded anchor X (reviewed relation or an
approved path of ≤ MAX_PATH_HOPS hops), reference needs mastered, learn is always allowed —
with usage-v2. The same policy object is used by `ViewLog.append` (through the engine), the
engine's usage index and the renderer's render-time check (ruling I3), so the three never
disagree. Compression is untouched: the SafetyClamp still requires `compressible`."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from evowiki.belief.beta import Belief
from evowiki.content.graph import ContentGraph
from evowiki.content.relations import RelationCatalog
from evowiki.rep.theta import (
    POLICY_VERSION_V1,
    POLICY_VERSION_V2,
    POLICY_VERSION_V3,
    POLICY_VERSION_V4,
    POLICY_VERSION_V5,
    POLICY_VERSION_V6,
)
from evowiki.view.constraints import (
    MAX_PATH_HOPS,
    persistent_role_allowed,
    persistent_role_allowed_v2,
)
from evowiki.view.features import has_reviewed_relation
from evowiki.view.ops import OpArgs, SetRole
from evowiki.view.roles import Role
from evowiki.view.usage import USAGE_V1, USAGE_V2


def anchor_grounded(c: str, anchor: str | None, graph: ContentGraph,
                    relations: RelationCatalog) -> bool:
    """A reviewed relation c~X or an approved path of ≤ MAX_PATH_HOPS hops between them."""
    if anchor is None or anchor == c:
        return False
    return (has_reviewed_relation(relations, c, anchor)
            or graph.path_supported({c, anchor}, MAX_PATH_HOPS))


@dataclass(frozen=True)
class EligibilityPolicy:
    policy_version: str
    usage_semantics: str

    @property
    def amended(self) -> bool:
        return self.policy_version in (POLICY_VERSION_V2, POLICY_VERSION_V3, POLICY_VERSION_V4,
                                       POLICY_VERSION_V5, POLICY_VERSION_V6)

    def persistent_ok(self, role: Role, belief: Belief, *, concept: str, anchor: str | None,
                      graph: ContentGraph, relations: RelationCatalog) -> bool:
        if not self.amended:
            return persistent_role_allowed(role, belief)
        return persistent_role_allowed_v2(
            role, belief, anchor_grounded=anchor_grounded(concept, anchor, graph, relations))

    def checker(self, beliefs: Mapping[str, Belief], graph: ContentGraph,
                relations: RelationCatalog) -> Callable[[OpArgs], list[str]]:
        """The `ViewLog.append(eligibility=...)` check. v1 keeps the original message."""
        def check(args: OpArgs) -> list[str]:
            if not isinstance(args, SetRole):
                return []
            b = beliefs[args.concept_id]
            if self.persistent_ok(args.role, b, concept=args.concept_id, anchor=args.anchor,
                                  graph=graph, relations=relations):
                return []
            if not self.amended or args.role is Role.REFERENCE:
                return [f"role {args.role} for {args.concept_id} requires mastered"]
            return [(f"role {args.role} for {args.concept_id} requires a grounded anchor "
                     f"(got {args.anchor})")]
        return check


ELIGIBILITY_V1 = EligibilityPolicy(POLICY_VERSION_V1, USAGE_V1)
ELIGIBILITY_V2 = EligibilityPolicy(POLICY_VERSION_V2, USAGE_V2)
ELIGIBILITY_V3 = EligibilityPolicy(POLICY_VERSION_V3, USAGE_V2)   # R1: v2 eligibility
ELIGIBILITY_V4 = EligibilityPolicy(POLICY_VERSION_V4, USAGE_V2)   # R1.1: v2 eligibility
ELIGIBILITY_V5 = EligibilityPolicy(POLICY_VERSION_V5, USAGE_V2)   # R2: v2 eligibility
ELIGIBILITY_V6 = EligibilityPolicy(POLICY_VERSION_V6, USAGE_V2)   # R2.1: v2 eligibility
_BY_VERSION = {POLICY_VERSION_V1: ELIGIBILITY_V1, POLICY_VERSION_V2: ELIGIBILITY_V2,
               POLICY_VERSION_V3: ELIGIBILITY_V3, POLICY_VERSION_V4: ELIGIBILITY_V4,
               POLICY_VERSION_V5: ELIGIBILITY_V5, POLICY_VERSION_V6: ELIGIBILITY_V6}


def eligibility_for(policy_version: str) -> EligibilityPolicy:
    """v1 → (layer-A v1, usage-v1); v2 → (amended layer A, usage-v2). Unknown → ValueError."""
    try:
        return _BY_VERSION[policy_version]
    except KeyError:
        raise ValueError(f"unknown θ_rep policy version {policy_version!r}") from None
