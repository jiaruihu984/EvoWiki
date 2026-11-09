"""R1 — exposure-bounded role persistence (policy version `theta_rep@v3`; reviewer ruling
2026-10-05, the product specification).

A concept c is *protected* iff its current V role is support or bridge, that role was set at
time t by an executed SetRole op, and the observable stream holds no page view of c after t.
While protected, every SetRole candidate for c is removed before resolve: neither the learn
fallback nor a reference overwrite nor an anchor change can replace the role, and nothing is
fed to the hysteresis for it. The first page view of c after t (the role's exposure) ends the
protection; c is then evaluated from current evidence as under θ_rep². Persistence is bounded
by the next exposure — never permanent, never a longer usage window.

R1.1 — application-bounded persistence (policy version `theta_rep@v4`; reviewer ruling
2026-10-05, the product specification). Protection is the same, but
it acts only at application: SetRole candidates of a protected concept go through history
filtering, resolve, the V-equal no-op rule and the N = 2 hysteresis exactly as under θ_rep². A
*stable* winner of a protected concept is withheld (not materialized, not appended) and recorded
as `persistence_suppressed`. Counterfactual cap reservation: the first withheld occurrence of a
value per protection episode (concept, assignment time) is the pass where θ_rep² would have
applied it, so it reserves the cap slot it would have consumed for the rest of that session
(only if θ_rep² would really have appended it: cap not full, materializable, grounded, eligible,
applicable); a repeat of the same value in the same episode would be a θ_rep² no-op and
reserves nothing. Suppression therefore never frees cap for an otherwise excluded op."""

from collections.abc import Iterable, Mapping
from datetime import datetime

from evowiki.evidence.models import Event, EventType
from evowiki.rep.theta import (
    POLICY_VERSION_V3,
    POLICY_VERSION_V4,
    POLICY_VERSION_V5,
    POLICY_VERSION_V6,
)
from evowiki.view.roles import Role
from evowiki.view.state import ViewState

PERSIST_UNTIL_EXPOSURE = frozenset({Role.SUPPORT, Role.BRIDGE})


def exposure_bounded(policy_version: str) -> bool:
    """R1 (v3): protected SetRole candidates are removed before resolve."""
    return policy_version == POLICY_VERSION_V3


def application_bounded(policy_version: str) -> bool:
    """R1.1 (v4, v5 = R1.1 + R2, v6 = v5 + R2.1): protected stable SetRole winners are
    withheld at application only."""
    return policy_version in (POLICY_VERSION_V4, POLICY_VERSION_V5, POLICY_VERSION_V6)


def last_exposure(events: Iterable[Event]) -> dict[str, datetime]:
    """Latest page-view time per concept (hub navigation views carry no concept_id)."""
    out: dict[str, datetime] = {}
    for e in events:
        if e.type is EventType.PAGE_VIEW:
            c = e.payload.get("concept_id")
            if c is not None and (c not in out or e.ts > out[c]):
                out[c] = e.ts
    return out


def protected_concepts(view: ViewState, assigned_at: Mapping[str, datetime],
                       events: Iterable[Event]) -> frozenset[str]:
    seen = last_exposure(events)
    return frozenset(
        c for c, t in assigned_at.items()
        if view.concept(c).role in PERSIST_UNTIL_EXPOSURE and not (c in seen and seen[c] > t))


def role_assignment_times(entries: Iterable) -> dict[str, datetime]:
    """Time of the latest SetRole op per concept in a ViewLog's entries (SetRole is never a
    pending proposal, so every SetRole op in the log is in effect when appended)."""
    from evowiki.view.ops import SetRole, ViewOp
    out: dict[str, datetime] = {}
    for e in entries:
        if isinstance(e, ViewOp) and isinstance(e.args, SetRole):
            out[e.args.concept_id] = e.ts
    return out
