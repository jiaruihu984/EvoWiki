"""θ_rep¹ slow loop (Stage I-B spec §3; Plan 4b-1b Task 5).

features (usage + K + content, never V) → fire → candidate ViewOps → coalesce → drop
history-blocked candidates (pending / rejected in cooldown / hub fingerprint) → priority →
specificity → incompatible tie = no-op → a winner equal to V is a no-op (not fed to the
hysteresis) → engine-owned N=2 hysteresis (R1) → per-session cap → materialize → grounded
append. Every step
is recorded in a `SlowLoopTrace` for the later manipulation check (Plan 4b-1c)."""

import hashlib
import json
from collections import Counter
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from evowiki.belief.beta import Belief
from evowiki.content.graph import ContentGraph
from evowiki.content.relations import RelationCatalog
from evowiki.content.segments import SegmentIndex
from evowiki.evidence.models import Event, EventType
from evowiki.llm.gateway import (
    LLMParseError,
    MissingRecordingError,
    ProviderOutputError,
    ProviderUnavailableError,
)
from evowiki.rep.evaluate import (
    Candidate,
    Resolution,
    Subject,
    candidates,
    coalesce,
    evaluate,
    resolve,
)
from evowiki.rep.theta import ThetaRep
from evowiki.view.constraints import HYSTERESIS_N, VIEWOP_CAP_PER_SESSION
from evowiki.view.eligibility import EligibilityPolicy, eligibility_for
from evowiki.view.engine_state import EngineState
from evowiki.view.features import (
    FeatureContext,
    bridge_colocation,
    bridge_partner_blocked,
    concept_features,
)
from evowiki.view.grounding import ConnectionRequest, hub_support
from evowiki.view.lease import (
    ConceptLease,
    LeaseLedger,
    fold_leases,
    lease_bounded,
    lease_snapshot,
)
from evowiki.view.log import GroundingError, ViewLog
from evowiki.view.naming import HubNamer
from evowiki.view.ops import (
    Confirmation,
    CreateHub,
    Decision,
    OpArgs,
    ProposedBy,
    SetPerspective,
    SetRole,
    ViewOp,
)
from evowiki.view.persistence import (
    application_bounded,
    exposure_bounded,
    protected_concepts,
    role_assignment_times,
)
from evowiki.view.perspective import PerspectiveChoice, select_default
from evowiki.view.roles import Role
from evowiki.view.state import ViewState
from evowiki.view.usage import UsageIndex

Slot = tuple[str, ...]

# Spec §4.7 system constants: configurable, never on the RSI-modifiable surface.
SLOW_LOOP_EVERY: Final = 20
# Re-proposal after a rejection, for every slot (design decision 9): also system constants, not θ.
REPROPOSE_COOLDOWN_SESSIONS: Final = 5
REPROPOSE_MIN_NEW_EVIDENCE: Final = 3

# Errors the gateway-backed namer may raise; each drops only the affected candidate.
_LLM_ERRORS: Final = (LLMParseError, MissingRecordingError, ProviderUnavailableError,
                      ProviderOutputError)


def proposal_fingerprint(members: Iterable[str], support_paths: Iterable[Iterable[str]],
                         rule_ref: str, policy_version: str) -> str:
    """Identity of a hub proposal: members + support paths + framing (rule and policy)."""
    blob = json.dumps({"members": sorted(members),
                       "support_paths": [list(p) for p in support_paths],
                       "rule": rule_ref, "policy": policy_version}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Hysteresis:
    """A key is stable once it was observed in `sessions_required` consecutive sessions.
    Several observations within one session count once; a missed session resets the streak."""

    def __init__(self, sessions_required: int):
        if sessions_required < 1:
            raise ValueError("sessions_required must be >= 1")
        self._n = sessions_required
        self._streak: dict[Hashable, tuple[int, int]] = {}

    def observe(self, session_index: int, keys: Iterable[Hashable]) -> set[Hashable]:
        stable: set[Hashable] = set()
        for k in keys:
            last, streak = self._streak.get(k, (None, 0))
            if last == session_index:
                pass
            elif last == session_index - 1:
                streak += 1
            else:
                streak = 1
            self._streak[k] = (session_index, streak)
            if streak >= self._n:
                stable.add(k)
        return stable

    def items(self) -> list[tuple[Hashable, int, int]]:
        """(key, last session index, streak) of every tracked key (Phase 0 persistence)."""
        return [(k, last, n) for k, (last, n) in self._streak.items()]

    def load(self, items: Iterable[tuple[Hashable, int, int]]) -> None:
        if self._streak:
            raise ValueError("Hysteresis.load needs a fresh hysteresis")
        self._streak = {k: (last, n) for k, last, n in items}


# Ordered application outcomes of the stable winners of a pass (SlowLoopTrace.application)
APP_APPENDED = "appended"
APP_SUPPRESSED_RESERVED = "persistence_suppressed_reserved"
APP_SUPPRESSED_REPEAT = "persistence_suppressed_repeat"
APP_CAP_DROPPED = "cap_dropped"
APP_MATERIALIZATION_DROPPED = "materialization_dropped"
APP_ADMISSION_DROPPED = "admission_dropped"
APP_CONNECTION_REQUESTS = "connection_requests"
APP_REFRACTORY_BLOCKED = "refractory_blocked"     # R2.1 (θ_rep@v6)


@dataclass(frozen=True)
class PersistenceSuppressed:
    """R1.1: a stable SetRole winner of a protected concept, withheld at application.
    `cap_reserved`: it reserved the cap slot θ_rep² would have spent on it (first withheld
    occurrence of this value in the protection episode that began at `protected_since`).
    Counterfactual cap state at the decision (evidence for re-deriving `cap_reserved`):
    `session_ops` appended ops of the session, `session_reserved` slots already reserved in it;
    `admission` the `ViewLog.admission_violations` result (() for a reservation, None for a
    repeat, which is not checked)."""
    slot: Slot
    value: Hashable
    rule_ids: tuple[str, ...]
    protected_since: datetime
    cap_reserved: bool
    session_ops: int = 0
    session_reserved: int = 0
    admission: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SlowLoopTrace:
    """Everything one slow-loop pass saw and decided. `firings` are (rule id, subject) with a
    group subject written as "a|b|c"; `blocked`/`noop`/`held` are slots; `appended` are ViewOp
    ids. `blocked` lists candidate slots removed by history before resolve (pending proposal,
    rejected in cooldown, hub fingerprint); `noop` lists resolve winners equal to V only."""
    session_index: int
    active_domain: str | None
    features: dict[str, dict]
    perspective_choices: dict[str, PerspectiveChoice]
    firings: tuple[tuple[str, str], ...]
    coalesced: tuple[Candidate, ...]
    resolutions: tuple[Resolution, ...]
    noop: tuple[Slot, ...]
    blocked: tuple[Slot, ...]
    held: tuple[Slot, ...]
    dropped: tuple[str, ...]
    appended: tuple[str, ...]
    protected: tuple[str, ...] = ()     # R1 (θ_rep@v3): concepts whose role persists until exposure
    persistence_suppressed: tuple[PersistenceSuppressed, ...] = ()   # R1.1 (θ_rep@v4)
    # every stable winner's outcome in ready order: (slot, value, APP_* outcome)
    application: tuple[tuple[Slot, Hashable, str], ...] = ()
    # R2 (θ_rep@v5) instrumentation: featured concepts whose best cross partner is unreviewed
    # while a reviewed co-located partner exists (`features.bridge_partner_blocked`)
    bridge_partner_blocked: tuple[str, ...] = ()
    # R2.1 (θ_rep@v6) instrumentation (ruling Q6): lease state per tracked concept at the start
    # of the pass; refractory concepts for which B2 fired; stable bridge winners withheld
    # because their concept is refractory; concepts whose consumed lease may be renewed (not
    # active, not refractory); concepts whose bridge was re-applied after a consumed lease
    lease: tuple[tuple[str, ConceptLease], ...] = ()
    refractory_b2: tuple[str, ...] = ()
    refractory_blocked: tuple[str, ...] = ()
    renewal_eligible: tuple[str, ...] = ()
    renewals: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlowLoopResult:
    appended: tuple[ViewOp, ...]
    connection_requests: tuple[ConnectionRequest, ...]
    trace: SlowLoopTrace


def _lease_trace(lstate, firings, appended, refractory_blocked) -> dict:
    """R2.1 instrumentation fields of one θ_rep@v6 pass (read-only)."""
    b2 = {f.subject for f in firings if f.rule.id == "B2" and isinstance(f.subject, str)}
    return {
        "lease": lease_snapshot(lstate),
        "refractory_b2": tuple(sorted(c for c in b2 if lstate.refractory(c))),
        "refractory_blocked": tuple(refractory_blocked),
        "renewal_eligible": tuple(sorted(
            c for c, cl in lstate.concepts.items()
            if cl.bridge_exposures and not cl.active and not cl.refractory)),
        "renewals": tuple(op.args.concept_id for op in appended
                          if isinstance(op.args, SetRole) and op.args.role is Role.BRIDGE
                          and lstate.get(op.args.concept_id).bridge_exposures),
    }


def _subject_text(s: Subject) -> str:
    return "|".join(sorted(s)) if isinstance(s, frozenset) else s


def subject_concepts(s: Subject) -> tuple[str, ...]:
    return tuple(sorted(s)) if isinstance(s, frozenset) else (s,)


def op_slot(args: OpArgs) -> tuple[Slot, Hashable] | None:
    """The (slot, value) of a logged op, in the conventions of `rep.evaluate.candidates`."""
    match args:
        case SetRole():
            return ("SET_ROLE", args.concept_id), (args.role, args.anchor)
        case SetPerspective():
            return ("SET_PERSPECTIVE", args.concept_id), args.segment_id
        case CreateHub():
            return ("CREATE_HUB", *args.members), args.members
    return None


def blocked_by_history(slot: Slot, value: Hashable, is_hub: bool, fingerprint: str | None,
                       subjects: Iterable[str], log: ViewLog, beliefs: Mapping[str, Belief],
                       usage: UsageIndex, session_index: int) -> bool:
    """Re-proposal rule (design decision 9; legacy semantics), for every slot kind. A proposal
    matching a PENDING op never repeats. One matching a REJECTED op (a rejected proposal or an
    undone auto op) returns only after the cooldown and with enough new evidence on its subject
    concepts. Hubs match by fingerprint, and an accepted hub (later retired) is held to the same
    rule; other slots match by (slot, value) and an accepted/auto op is left to the V check."""
    now_evidence = sum(beliefs[m].evidence_count for m in subjects)
    for op in log.entries:
        if not isinstance(op, ViewOp):
            continue
        if is_hub:
            if op.proposal_fingerprint != fingerprint:
                continue
        elif op_slot(op.args) != (slot, value):
            continue
        state = log.confirmation(op.id)
        if state is Confirmation.PENDING:
            return True
        if state is Confirmation.REJECTED or (is_hub and state is Confirmation.ACCEPTED):
            d = log.decision(op.id)
            assert d is not None
            cooled = (session_index - usage.session_index(d.session_id)
                      >= REPROPOSE_COOLDOWN_SESSIONS)
            grown = (now_evidence - sum(n for _, n in op.evidence_snapshot)
                     >= REPROPOSE_MIN_NEW_EVIDENCE)
            if not (cooled and grown):
                return True
    return False


@dataclass(frozen=True)
class _Ready:
    args: OpArgs
    proposed_by: ProposedBy
    llm_call_ids: tuple[str, ...]
    fingerprint: str | None = None


class _StateShim:
    """`ViewEngine._in_view` reads `log.state` only; θ_rep@v6 compares with the effective V."""

    def __init__(self, state: ViewState):
        self.state = state


class ViewEngine:
    """The θ_rep¹ slow loop. Layer-A constants (HYSTERESIS_N, VIEWOP_CAP_PER_SESSION) come from
    `view.constraints` and are not configurable here.

    Mutable state: `self._hysteresis` (the N=2 streaks, decision R1) and
    `self._last_session_index`. Both are rebuilt by replaying sessions in order; persistence is
    owed with V persistence (Plan 5 follow-up)."""

    def __init__(self, graph: ContentGraph, segments: SegmentIndex, relations: RelationCatalog,
                 theta: ThetaRep, *, namer: HubNamer, policy_version: str,
                 slow_loop_every: int = SLOW_LOOP_EVERY):
        self._graph, self._segments, self._relations = graph, segments, relations
        self._theta = theta
        self._namer = namer
        self._policy_version = policy_version
        # Plan 4b-1d: persistent-role eligibility and usage semantics follow the policy version
        # (θ_rep¹ → layer-A v1 + usage-v1; θ_rep² → amended layer A + usage-v2).
        self._eligibility = eligibility_for(policy_version)
        self._every = slow_loop_every
        self._hysteresis = Hysteresis(HYSTERESIS_N)      # R1: once per engine, never per run
        self._last_session_index: int | None = None
        # R1.1 counterfactual cap reservation: slots reserved per session, and the withheld
        # (concept, protected_since, value) triples that already reserved (θ_rep² would have
        # applied them once; a repeat would be a θ_rep² no-op)
        self._reserved: Counter[str] = Counter()
        self._withheld: set[tuple[str, datetime, Hashable]] = set()

    @property
    def eligibility(self) -> EligibilityPolicy:
        return self._eligibility

    # --- persistence (Phase 0, live V) -----------------------------------------------------
    _restored: bool = False      # set by restore(): any second restore is refused

    def snapshot(self) -> EngineState:
        """The engine's mutable state as a value (hysteresis, last session, R1.1 reservations)."""
        return EngineState.of(
            [(k[0], k[1], last, n) for k, last, n in self._hysteresis.items()],
            self._last_session_index, self._reserved.items(), self._withheld)

    def restore(self, state: EngineState) -> None:
        """Load a snapshot into a fresh engine (never one that has already run or restored)."""
        if self._restored or self._hysteresis.items() or self._last_session_index is not None \
                or self._reserved or self._withheld:
            raise ValueError("ViewEngine.restore needs a fresh engine")
        self._hysteresis.load(((s, v), last, n) for s, v, last, n in state.streaks)
        self._last_session_index = state.last_session_index
        self._reserved = Counter(dict(state.reserved))
        self._withheld = set(state.withheld)
        self._restored = True

    def usage(self, events: Sequence[Event]) -> UsageIndex:
        """The engine's usage index over an event stream (the policy's usage semantics)."""
        return UsageIndex.from_events(events, self._graph,
                                      semantics=self._eligibility.usage_semantics)

    def should_run(self, interactions_since_last_run: int, session_ended: bool) -> bool:
        return session_ended or interactions_since_last_run >= self._every

    def ingest(self, log: ViewLog, events: Sequence[Event]) -> list[Decision]:
        """Apply the user's answers (view_suggestion_accepted/rejected) to pending ops."""
        out: list[Decision] = []
        for ev in sorted(events, key=lambda e: e.seq):
            if ev.type not in (EventType.VIEW_SUGGESTION_ACCEPTED,
                               EventType.VIEW_SUGGESTION_REJECTED):
                continue
            op_id = ev.payload.get("suggestion_id")
            try:
                pending = log.confirmation(op_id) is Confirmation.PENDING
            except KeyError:
                continue
            if pending:
                out.append(log.decide(op_id, ev.type is EventType.VIEW_SUGGESTION_ACCEPTED,
                                      session_id=ev.session_id, ts=ev.ts))
        return out

    # --- hubs ----------------------------------------------------------------------------------
    def _fingerprint(self, c: Candidate) -> tuple[str, tuple[tuple[str, ...], ...]]:
        members = tuple(c.value)  # type: ignore[arg-type]
        support = hub_support(members, self._graph, self._segments)
        paths = support.paths if support is not None else ()
        rule_ref = ",".join(f"{r}@v{self._theta.rule(r).version}" for r in c.rule_ids)
        return proposal_fingerprint(members, paths, rule_ref, self._policy_version), paths

    # --- no-op filter --------------------------------------------------------------------------
    @staticmethod
    def _in_view(c: Candidate, log: ViewLog) -> bool:
        """The candidate's value is already what V holds (a winner like this is a no-op)."""
        st = log.state
        match c.slot[0]:
            case "SET_ROLE":
                v = st.concept(c.slot[1])
                return c.value == (v.role or Role.LEARN, v.role_anchor)
            case "SET_PERSPECTIVE":
                return st.concept(c.slot[1]).perspective_segment_id == c.value
            case "CREATE_HUB":
                members = set(c.value)  # type: ignore[arg-type]
                return any(members <= set(m) for m in st.active_hubs().values())
        return False

    def _blocked(self, c: Candidate, log: ViewLog, beliefs: Mapping[str, Belief],
                 usage: UsageIndex, session_index: int) -> bool:
        hub = c.slot[0] == "CREATE_HUB"
        fp = self._fingerprint(c)[0] if hub else None
        return blocked_by_history(c.slot, c.value, hub, fp, subject_concepts(c.subject), log,
                                  beliefs, usage, session_index)

    # --- materialization -----------------------------------------------------------------------
    def _materialize(self, c: Candidate, log: ViewLog) -> _Ready | list[ConnectionRequest] | str:
        kind = c.slot[0]
        if kind == "SET_ROLE":
            role, anchor = c.value  # type: ignore[misc]
            if c.propose:
                # A pending SetRole would be accepted later without re-checking eligibility
                # (ViewLog.decide), so a persistent role is never a pending proposal.
                return f"propose: SET_ROLE {c.slot[1]} is never a pending proposal"
            return _Ready(SetRole(concept_id=c.slot[1], role=role, anchor=anchor),
                          ProposedBy.RULE, ())
        if kind == "SET_PERSPECTIVE":
            return _Ready(SetPerspective(concept_id=c.slot[1], segment_id=c.value),  # type: ignore[arg-type]
                          ProposedBy.RULE, ())
        if kind == "CREATE_HUB":
            members = tuple(c.value)  # type: ignore[arg-type]
            support = hub_support(members, self._graph, self._segments)
            if support is None:
                return f"grounding: {members} not path-supported"
            if not support.complete:
                key = "|".join(members)
                return [ConnectionRequest(path=h, requested_for=key) for h in support.missing_hops]
            name, call = self._namer.name(members)
            fp, _ = self._fingerprint(c)
            return _Ready(CreateHub(hub_id=f"hub-{log.next_seq:06d}", label_en=name.label_en,
                                    label_zh=name.label_zh, members=members,
                                    support_paths=support.paths),
                          ProposedBy.RULE, (call,) if call else (), fp)
        return f"unknown slot {c.slot}"

    # --- the slow loop -------------------------------------------------------------------------
    def run(self, log: ViewLog, beliefs: Mapping[str, Belief], events: Sequence[Event],
            now: datetime, session_id: str, *,
            leases: LeaseLedger | None = None) -> SlowLoopResult:
        """One slow-loop pass. `events` must be the user's full observable event stream (not just
        the current session): session indices, the usage window, hysteresis and re-proposal
        cooldowns all derive from it. Sessions must be fed in order; repeating the latest
        session is allowed (hysteresis counts a session once)."""
        usage = self.usage(events)
        # Validate before any mutation (ingest, hysteresis, _last_session_index): fail closed.
        if session_id not in usage.sessions:
            raise ValueError(f"session {session_id} is not an observed session of the stream")
        session_index = usage.session_index(session_id)
        if self._last_session_index is not None and session_index < self._last_session_index:
            raise ValueError(f"session {session_id} (index {session_index}) is older than the "
                             f"last slow-loop session (index {self._last_session_index})")
        if lease_bounded(self._policy_version) and leases is None:
            raise ValueError("θ_rep@v6 needs the user's lease ledger")
        self.ingest(log, events)
        ctx = FeatureContext(self._graph, self._relations, dict(beliefs), usage, now,
                               self._theta.params,
                               bridge_colocation=bridge_colocation(self._policy_version))
        # R2.1 (θ_rep@v6): every read of V below uses the lease-aware effective view
        lstate = (fold_leases(log.entries, leases.records)
                  if lease_bounded(self._policy_version) and leases is not None else None)
        view = lstate.effective(log.state) if lstate is not None else log.state
        featured = sorted(usage.recent()
                          | {c for c in view.concept_ids() if view.concept(c).role is not None})
        feats = {c: concept_features(c, ctx) for c in featured}
        # R2 instrumentation (ruling D2): featured concepts whose unreviewed best cross partner
        # blocks a reviewed one (ranking unchanged; only counted)
        partner_blocked = (tuple(c for c in featured if bridge_partner_blocked(c, ctx))
                           if ctx.bridge_colocation else ())
        choices = {c: select_default(c, ctx, view.concept(c).perspective_segment_id)
                   for c in featured}
        firings = evaluate(self._theta, feats, ctx)
        cands = coalesce(candidates(firings, feats, ctx, view))

        # Step order (controller ruling, Task 5 fix 1): history-blocked candidates are removed
        # BEFORE resolve; then resolve; a winner equal to V is a no-op that is NOT fed to the
        # hysteresis. Filtering V-equal candidates before resolve would let a lower-priority
        # rule win against an absent incumbent and make roles flap (BRIDGE <-> REFERENCE).
        noop: list[Slot] = []
        remaining = [c for c in cands if not self._blocked(c, log, beliefs, usage, session_index)]
        blocked = tuple(c.slot for c in cands if c not in remaining)
        protected: frozenset[str] = frozenset()
        assigned = role_assignment_times(log.entries)
        if exposure_bounded(self._policy_version):
            # R1: a support/bridge role persists until the target's next exposure
            protected = protected_concepts(view, assigned, events)
            remaining = [c for c in remaining
                         if not (c.slot[0] == "SET_ROLE" and c.slot[1] in protected)]
        elif application_bounded(self._policy_version):
            # R1.1: same protection, applied only after the hysteresis (below)
            protected = protected_concepts(view, assigned, events)
        resolved, resolutions = resolve(remaining)
        won: list[Candidate] = []
        for c in resolved:
            if self._in_view(c, log if lstate is None else _StateShim(view)):  # type: ignore[arg-type]
                noop.append(c.slot)
            else:
                won.append(c)
        stable = self._hysteresis.observe(session_index, {(c.slot, c.value) for c in won})
        ready = sorted((c for c in won if (c.slot, c.value) in stable),
                       key=lambda c: (-c.priority, -c.specificity, c.rule_ids, c.slot))
        held = tuple(c.slot for c in won if (c.slot, c.value) not in stable)

        appended: list[ViewOp] = []
        requests: list[ConnectionRequest] = []
        dropped: list[str] = []
        suppressed: list[PersistenceSuppressed] = []
        # instrumentation (R1.1 evidence, every policy version): each stable winner's outcome
        # in the frozen ready order, so cap accounting can be re-derived without the engine
        application: list[tuple[Slot, Hashable, str]] = []
        eligibility = self._eligibility.checker(beliefs, self._graph, self._relations)
        refractory_blocked: list[str] = []
        for c in ready:
            if (lstate is not None and c.slot[0] == "SET_ROLE" and c.value[0] is Role.BRIDGE
                    and lstate.refractory(c.slot[1])):
                # R2.1: no bridge application before one non-bridge exposure (K = 1); the
                # winner keeps its hysteresis evidence; no cap, no reservation
                refractory_blocked.append(c.slot[1])
                application.append((c.slot, c.value, APP_REFRACTORY_BLOCKED))
                continue
            if application_bounded(self._policy_version) and c.slot[0] == "SET_ROLE" \
                    and c.slot[1] in protected:
                # R1.1: withhold; reserve the cap only where θ_rep² would have appended it
                key = (c.slot[1], assigned[c.slot[1]], c.value)
                state = (log.ops_in_session(session_id), self._reserved[session_id])
                if key in self._withheld:
                    suppressed.append(PersistenceSuppressed(c.slot, c.value, c.rule_ids,
                                                            key[1], False, *state, None))
                    application.append((c.slot, c.value, APP_SUPPRESSED_REPEAT))
                    continue
                if log.ops_in_session(session_id) + self._reserved[session_id] \
                        >= VIEWOP_CAP_PER_SESSION:
                    dropped.append(f"cap: {c.slot}")
                    application.append((c.slot, c.value, APP_CAP_DROPPED))
                    continue
                out = self._materialize(c, log)
                if isinstance(out, str):
                    dropped.append(out)
                    application.append((c.slot, c.value, APP_MATERIALIZATION_DROPPED))
                    continue
                violations = log.admission_violations(out.args, eligibility)  # type: ignore[union-attr]
                if violations:
                    dropped.append(f"grounding: {GroundingError(violations)}")
                    application.append((c.slot, c.value, APP_ADMISSION_DROPPED))
                    continue
                self._withheld.add(key)
                self._reserved[session_id] += 1
                suppressed.append(PersistenceSuppressed(c.slot, c.value, c.rule_ids, key[1],
                                                        True, *state, ()))
                application.append((c.slot, c.value, APP_SUPPRESSED_RESERVED))
                continue
            if log.ops_in_session(session_id) + self._reserved[session_id] \
                    >= VIEWOP_CAP_PER_SESSION:
                dropped.append(f"cap: {c.slot}")
                application.append((c.slot, c.value, APP_CAP_DROPPED))
                continue
            try:
                out = self._materialize(c, log)
            except _LLM_ERRORS as exc:
                # Fail-closed: a failed naming call drops the candidate, never crashes.
                dropped.append(f"llm: {c.slot}: {type(exc).__name__}: {exc}")
                application.append((c.slot, c.value, APP_MATERIALIZATION_DROPPED))
                continue
            if isinstance(out, str):
                dropped.append(out)
                application.append((c.slot, c.value, APP_MATERIALIZATION_DROPPED))
                continue
            if isinstance(out, list):
                requests += out
                application.append((c.slot, c.value, APP_CONNECTION_REQUESTS))
                continue
            try:
                appended.append(log.append(
                    out.args, session_id=session_id, ts=now, policy_version=self._policy_version,
                    active_domain=usage.active_domain, rule_id=",".join(c.rule_ids),
                    proposed_by=out.proposed_by, propose=c.propose, llm_call_ids=out.llm_call_ids,
                    proposal_fingerprint=out.fingerprint,
                    evidence_snapshot=tuple((m, beliefs[m].evidence_count)
                                            for m in subject_concepts(c.subject)),
                    eligibility=eligibility,
                ))
                application.append((c.slot, c.value, APP_APPENDED))
            except GroundingError as exc:
                dropped.append(f"grounding: {exc}")
                application.append((c.slot, c.value, APP_ADMISSION_DROPPED))

        trace = SlowLoopTrace(
            session_index=session_index, active_domain=usage.active_domain,
            features={c: f.as_dict() for c, f in feats.items()},
            perspective_choices=choices,
            firings=tuple((f.rule.id, _subject_text(f.subject)) for f in firings),
            coalesced=tuple(cands), resolutions=tuple(resolutions), noop=tuple(noop),
            blocked=blocked, held=held, dropped=tuple(dropped), appended=tuple(op.id for op in appended),
            protected=tuple(sorted(protected)), persistence_suppressed=tuple(suppressed),
            application=tuple(application), bridge_partner_blocked=partner_blocked,
            **(_lease_trace(lstate, firings, appended, refractory_blocked)
               if lstate is not None else {}))
        self._last_session_index = session_index      # committed only after a successful pass
        return SlowLoopResult(tuple(appended), tuple(requests), trace)
