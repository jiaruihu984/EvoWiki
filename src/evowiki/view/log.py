"""The ViewOp event stream (spec §4.4, §6.3). V is always the fold of this log."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import TypeAdapter

from evowiki.view.ops import (
    ALWAYS_STRUCTURAL,
    PRIMARY_SENSITIVE,
    Confirmation,
    Decision,
    LogEntry,
    OpArgs,
    ProposedBy,
    SetMode,
    ViewOp,
)
from evowiki.view.state import InapplicableOp, ViewState

Checker = Callable[[OpArgs, ViewState], list[str]]


def permissive(args: OpArgs, state: ViewState) -> list[str]:
    """A checker that allows everything (tests only; production uses grounding_checker)."""
    return []


PERMISSIVE: Checker = permissive


class GroundingError(ValueError):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations))
        self.violations = list(violations)


_ENTRY = TypeAdapter(LogEntry)


@dataclass(frozen=True)
class FoldResult:
    state: ViewState
    skipped: tuple[str, ...]


def fold(entries: Sequence[ViewOp | Decision]) -> FoldResult:
    """Replay V. An auto op takes effect at its seq unless a decision undid it. A pending op
    takes effect at the seq of its accepting decision. Ops that no longer apply are skipped."""
    decisions = {e.op_id: e for e in entries if isinstance(e, Decision)}
    effects: list[tuple[int, ViewOp]] = []
    for e in entries:
        if not isinstance(e, ViewOp):
            continue
        d = decisions.get(e.id)
        if e.confirmation is Confirmation.AUTO and (d is None or d.accepted):
            effects.append((e.seq, e))
        elif e.confirmation is Confirmation.PENDING and d is not None and d.accepted:
            effects.append((d.seq, e))
    state, skipped = ViewState(), []
    for _, op in sorted(effects, key=lambda t: t[0]):
        try:
            state = state.apply(op.args)
        except InapplicableOp:
            skipped.append(op.id)
    return FoldResult(state, tuple(skipped))


class ViewLog:
    """Append-only ViewOp + Decision log for one user. Grounding is checked before every
    append and again when a pending op is accepted (fail-closed). Loading a log (constructor
    entries, `from_jsonl`) re-validates entry shape, ordering and sovereignty (a structural op
    can never be auto), but replay trusts the original writer's grounding; whether to re-ground
    on load is a Plan 5 persistence decision."""

    def __init__(self, user_id: str, checker: Checker, domain_of: Mapping[str, str],
                 entries: Iterable[ViewOp | Decision] = ()):
        self.user_id = user_id
        self._checker = checker
        self._domain_of = dict(domain_of)
        self._entries: list[ViewOp | Decision] = []
        self._ops: dict[str, ViewOp] = {}
        self._decisions: dict[str, Decision] = {}
        for e in entries:
            self._admit(e)
        result = fold(self._entries)
        self._state, self._skipped = result.state, result.skipped

    def _admit(self, e: ViewOp | Decision) -> None:
        if e.user_id != self.user_id:
            raise ValueError(f"entry {e.seq} belongs to {e.user_id}, not {self.user_id}")
        if e.seq != len(self._entries):
            raise ValueError(f"entry seq {e.seq} out of order (expected {len(self._entries)})")
        if isinstance(e, ViewOp):
            if e.id in self._ops:
                raise ValueError(f"duplicate op id {e.id}")
            self._ops[e.id] = e
        else:
            op = self._ops.get(e.op_id)
            if op is None:
                raise ValueError(f"decision on unknown op {e.op_id}")
            if e.op_id in self._decisions:
                raise ValueError(f"op {e.op_id} already decided")
            if op.confirmation is Confirmation.AUTO and e.accepted:
                raise ValueError(f"auto op {e.op_id} needs no acceptance")
            self._decisions[e.op_id] = e
        self._entries.append(e)

    # --- read side ---------------------------------------------------------------------------
    @property
    def entries(self) -> tuple[ViewOp | Decision, ...]:
        return tuple(self._entries)

    @property
    def state(self) -> ViewState:
        return self._state

    @property
    def skipped(self) -> tuple[str, ...]:
        return self._skipped

    @property
    def next_seq(self) -> int:
        return len(self._entries)

    def op(self, op_id: str) -> ViewOp:
        return self._ops[op_id]

    def decision(self, op_id: str) -> Decision | None:
        return self._decisions.get(op_id)

    def confirmation(self, op_id: str) -> Confirmation:
        op, d = self._ops[op_id], self._decisions.get(op_id)
        if d is None:
            return op.confirmation
        return Confirmation.ACCEPTED if d.accepted else Confirmation.REJECTED

    def pending(self) -> list[ViewOp]:
        return [op for op in self._ops.values() if self.confirmation(op.id) is Confirmation.PENDING]

    def ops_in_session(self, session_id: str) -> int:
        return sum(1 for op in self._ops.values() if op.session_id == session_id)

    def needs_confirmation(self, args: OpArgs, active_domain: str | None) -> bool:
        """Spec §6.3, broad reading: CREATE_HUB/MERGE_HUB/RETIRE_HUB always need confirmation;
        ATTACH/DETACH need it iff they change the primary hub of ANY concept at
        `active_domain`. Relevance is the active-domain share over all members of a hub, so a
        membership change of `args.hub_id` can flip the primary hub of every concept attached
        to it (before or after), not just `args.concept_id`. Raises InapplicableOp."""
        if args.kind in ALWAYS_STRUCTURAL:
            return True
        if args.kind in PRIMARY_SENSITIVE:
            after = self._state.apply(args)
            affected = (self._state.members(args.hub_id) | after.members(args.hub_id)
                        | {args.concept_id})
            return any(
                self._state.primary_hub(c, active_domain, self._domain_of)
                != after.primary_hub(c, active_domain, self._domain_of)
                for c in sorted(affected)
            )
        return False

    # --- write side --------------------------------------------------------------------------
    def admission_violations(self, args: OpArgs,
                             eligibility: Callable[[OpArgs], list[str]] | None = None) -> list[str]:
        """Read-only: the violations `append` would raise for `args` in the current state (empty
        iff `append` would admit it). Mirrors `append`'s checks in its order; changes nothing.
        Used by R1.1 to decide whether a withheld op would have been appended (cap
        reservation)."""
        if isinstance(args, SetMode):
            return ["legacy: SET_MODE is load-only"]
        violations = self._checker(args, self._state)
        if eligibility is not None:
            violations += eligibility(args)
        if violations:
            return violations
        try:
            self._state.apply(args)
        except InapplicableOp as exc:
            return [f"inapplicable: {exc}"]
        return []

    def append(self, args: OpArgs, *, session_id: str, ts: datetime, policy_version: str,
               active_domain: str | None, rule_id: str | None = None,
               proposed_by: ProposedBy = ProposedBy.RULE, propose: bool = False,
               llm_call_ids: Iterable[str] = (), proposal_fingerprint: str | None = None,
               evidence_snapshot: Iterable[tuple[str, int]] = (),
               eligibility: Callable[[OpArgs], list[str]] | None = None) -> ViewOp:
        """Ground, check `eligibility` (e.g. persistent-role invariant), then append. Legacy
        SET_MODE is load-only and is always refused here."""
        if isinstance(args, SetMode):
            raise GroundingError(["legacy: SET_MODE is load-only"])
        violations = self._checker(args, self._state)
        if eligibility is not None:
            violations += eligibility(args)
        if violations:
            raise GroundingError(violations)
        try:
            after = self._state.apply(args)
            required = propose or self.needs_confirmation(args, active_domain)
        except InapplicableOp as exc:
            raise GroundingError([f"inapplicable: {exc}"]) from exc
        seq = self.next_seq
        op = ViewOp(
            id=f"{self.user_id}:vop:{seq:06d}", user_id=self.user_id, session_id=session_id,
            seq=seq, ts=ts, args=args, triggered_by_rule_id=rule_id, proposed_by=proposed_by,
            requires_confirmation=required,
            confirmation=Confirmation.PENDING if required else Confirmation.AUTO,
            policy_version=policy_version, llm_call_ids=tuple(llm_call_ids),
            proposal_fingerprint=proposal_fingerprint, evidence_snapshot=tuple(evidence_snapshot),
        )
        self._admit(op)
        if not required:
            self._state = after
        return op

    def decide(self, op_id: str, accepted: bool, *, session_id: str, ts: datetime,
               reason: str = "user") -> Decision:
        op = self._ops[op_id]
        current = self.confirmation(op_id)
        if current in (Confirmation.ACCEPTED, Confirmation.REJECTED):
            raise ValueError(f"op {op_id} already decided ({current.value})")
        if current is Confirmation.AUTO and accepted:
            raise ValueError(f"auto op {op_id} needs no acceptance")
        after = None
        if accepted:
            violations = self._checker(op.args, self._state)
            if violations:
                accepted, reason = False, "grounding: " + "; ".join(violations)
            else:
                try:
                    after = self._state.apply(op.args)
                except InapplicableOp as exc:
                    accepted, reason = False, f"inapplicable: {exc}"
        d = Decision(op_id=op_id, user_id=self.user_id, session_id=session_id,
                     seq=self.next_seq, ts=ts, accepted=accepted, reason=reason)
        self._admit(d)
        if after is not None:
            self._state = after
        elif current is Confirmation.AUTO:  # undo: recompute V without the op
            result = fold(self._entries)
            self._state, self._skipped = result.state, result.skipped
        return d

    # --- serialization -------------------------------------------------------------------------
    def to_jsonl(self) -> str:
        return "".join(_ENTRY.dump_json(e).decode() + "\n" for e in self._entries)

    @classmethod
    def from_jsonl(cls, text: str, *, user_id: str, checker: Checker,
                   domain_of: Mapping[str, str]) -> "ViewLog":
        entries = [_ENTRY.validate_json(line) for line in text.splitlines() if line.strip()]
        return cls(user_id, checker, domain_of, entries)
