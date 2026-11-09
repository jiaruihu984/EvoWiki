"""R2.1 — concept-scoped bridge lease with K = 1 refractory (policy version `theta_rep@v6`;
rulings 2026-10-06, the product specification §7).

The ViewLog keeps every SetRole as appended (raw V). The *effective* view is a deterministic,
replayable fold of two append-only inputs:

- the ViewLog's SetRole ops (seq order), and
- the lease ledger: render outcomes recorded when a page of the concept is shown —
  `bridge_exposure` (the page rendered with bridge as the effective, default-sourced role while
  the concept's lease was active) and `refractory_exposure` (a page of a refractory concept that
  did not render as bridge). Each record carries the ViewLog length at record time
  (`after_view_seq`), which fixes its place among the ops.

Per concept (one lease, never per (concept, anchor) pair):
- a bridge SetRole while no lease is active starts a new lease epoch and freezes the rollback
  target = the effective (role, anchor) just before it; a bridge SetRole while the lease is
  active only updates the anchor (same epoch, same rollback target, no new exposure budget);
- a non-bridge SetRole replaces the effective role and ends an active lease unconsumed;
- a bridge exposure consumes the lease: the effective (role, anchor) is restored to the
  rollback target immediately (not a rule winner: no N = 2, no cap, no op) and the concept
  becomes refractory;
- a refractory exposure (one non-bridge exposure, K = 1) ends the refractory period.

The engine, the S3 renderer integration and the feature-level predictor all read the effective
view through `LeaseState.effective`, and all use `begin`/`on_set_role`/`on_exposure` below, so
render, the V-equal no-op rule and the prediction share one lease semantics. While a concept
is refractory a stable bridge winner is withheld at application (B2 and N = 2 evidence keep
accumulating); it can apply at a later regular pass once the refractory period has ended."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType

from evowiki.rep.theta import POLICY_VERSION_V6
from evowiki.view.ops import Confirmation, Decision, SetRole, ViewOp
from evowiki.view.roles import Role
from evowiki.view.state import ViewState

BRIDGE_EXPOSURE = "bridge_exposure"
REFRACTORY_EXPOSURE = "refractory_exposure"
REFRACTORY_K = 1


def lease_bounded(policy_version: str) -> bool:
    """R2.1 is active under θ_rep@v6 only."""
    return policy_version == POLICY_VERSION_V6


@dataclass(frozen=True)
class LeaseRecord:
    kind: str                    # BRIDGE_EXPOSURE | REFRACTORY_EXPOSURE
    concept_id: str
    plan_id: str | None
    ts: datetime
    after_view_seq: int          # ViewLog length when recorded


@dataclass(frozen=True)
class ConceptLease:
    """Lease state of one concept. `role`/`anchor` are the effective (role, anchor)."""
    role: Role | None = None
    anchor: str | None = None
    epoch: int = 0                           # number of leases begun
    active: bool = False
    refractory: bool = False
    acquired_ts: datetime | None = None      # start of the current/last lease epoch
    pre_role: Role | None = None             # rollback target of the current/last epoch
    pre_anchor: str | None = None
    bridge_exposures: int = 0                # leases consumed
    refractory_exposures: int = 0
    last_lapse_ts: datetime | None = None


_EMPTY = ConceptLease()


def on_set_role(cl: ConceptLease, role: Role, anchor: str | None, ts: datetime) -> ConceptLease:
    if role is Role.BRIDGE:
        if cl.active:                         # anchor update inside the same lease
            return replace(cl, anchor=anchor)
        if cl.refractory:
            raise ValueError("a bridge cannot be applied to a refractory concept")
        return replace(cl, role=Role.BRIDGE, anchor=anchor, epoch=cl.epoch + 1, active=True,
                       acquired_ts=ts, pre_role=cl.role, pre_anchor=cl.anchor)
    return replace(cl, role=role, anchor=anchor, active=False)


def on_exposure(cl: ConceptLease, kind: str, ts: datetime) -> ConceptLease:
    if kind == BRIDGE_EXPOSURE:
        if not cl.active:
            raise ValueError("bridge exposure without an active lease")
        return replace(cl, role=cl.pre_role, anchor=cl.pre_anchor, active=False,
                       refractory=True, bridge_exposures=cl.bridge_exposures + 1,
                       last_lapse_ts=ts)
    if kind == REFRACTORY_EXPOSURE:
        if not cl.refractory:
            raise ValueError("refractory exposure of a concept that is not refractory")
        return replace(cl, refractory=False, refractory_exposures=cl.refractory_exposures + 1)
    raise ValueError(f"unknown lease record kind {kind!r}")


@dataclass(frozen=True)
class LeaseState:
    concepts: Mapping[str, ConceptLease]

    def get(self, c: str) -> ConceptLease:
        return self.concepts.get(c, _EMPTY)

    def active(self, c: str) -> bool:
        return self.get(c).active

    def refractory(self, c: str) -> bool:
        return self.get(c).refractory

    def effective(self, raw: ViewState) -> ViewState:
        """The raw V with every lease-tracked concept's (role, anchor) set to its effective
        value (they differ only for a lapsed bridge)."""
        changed = {c: cl for c, cl in self.concepts.items()
                   if (raw.concept(c).role, raw.concept(c).role_anchor) != (cl.role, cl.anchor)}
        if not changed:
            return raw
        concepts = {c: raw.concept(c) for c in raw.concept_ids()}
        for c, cl in changed.items():
            concepts[c] = replace(raw.concept(c), role=cl.role, role_anchor=cl.anchor)
        return ViewState(raw.hubs, concepts, raw.applied)


def fold_leases(entries: Sequence[ViewOp | Decision],
                records: Iterable[LeaseRecord]) -> LeaseState:
    """Replay the lease state from the ViewLog entries and the lease ledger (deterministic)."""
    decided = {e.op_id for e in entries if isinstance(e, Decision)}
    # sort key: an op at seq n -> (n, 1, 0); a record made when the log had n entries ->
    # (n, 0, i): after every op with seq < n, before the op with seq n, in ledger order
    stream: list[tuple[tuple[int, int, int], ViewOp | LeaseRecord]] = []
    for e in entries:
        if isinstance(e, ViewOp) and isinstance(e.args, SetRole):
            if e.confirmation is not Confirmation.AUTO or e.id in decided:
                raise ValueError("θ_rep@v6 leases assume auto SetRole ops without decisions")
            stream.append(((e.seq, 1, 0), e))
    stream += [((r.after_view_seq, 0, i), r) for i, r in enumerate(records)]
    state: dict[str, ConceptLease] = {}
    for _, item in sorted(stream, key=lambda t: t[0]):
        if isinstance(item, ViewOp):
            a = item.args
            assert isinstance(a, SetRole)
            state[a.concept_id] = on_set_role(state.get(a.concept_id, _EMPTY), a.role,
                                              a.anchor, item.ts)
        else:
            state[item.concept_id] = on_exposure(state.get(item.concept_id, _EMPTY), item.kind,
                                                 item.ts)
    return LeaseState(MappingProxyType(state))


class LeaseLedger:
    """Append-only lease ledger of one user (render outcomes; see module docstring)."""

    def __init__(self, records: Iterable[LeaseRecord] = ()):
        self._records: list[LeaseRecord] = list(records)

    @property
    def records(self) -> tuple[LeaseRecord, ...]:
        return tuple(self._records)

    def record(self, kind: str, concept_id: str, plan_id: str | None, ts: datetime,
               after_view_seq: int) -> LeaseRecord:
        r = LeaseRecord(kind, concept_id, plan_id, ts, after_view_seq)
        self._records.append(r)
        return r


def exposure_kind(state: LeaseState, concept_id: str, *, v_bridge: bool,
                  any_bridge: bool) -> str | None:
    """The lease record a shown page of `concept_id` produces: a bridge exposure if its lease
    is active and the page rendered with bridge as the effective, default-sourced (V) role
    (ruling Q5: otherwise the learner did not get the bridge and nothing is consumed); a
    refractory exposure if the concept is refractory and the page did not render as any bridge
    (a renderer context bridge does not count as the non-bridge exposure)."""
    if state.active(concept_id):
        return BRIDGE_EXPOSURE if v_bridge else None
    if state.refractory(concept_id) and not any_bridge:
        return REFRACTORY_EXPOSURE
    return None


def lease_snapshot(state: LeaseState) -> tuple[tuple[str, ConceptLease], ...]:
    return tuple(sorted(state.concepts.items()))
