"""Harness RSI rounds, pack changes and the experience book in Postgres (migration 0065, design
2026-10-21 §4–§8). Plain functions over a connection; the caller owns the transaction.

A round starts `running` (with the finding it works on) and is finished once, by `finish_round`;
a change is recorded `asked` or `applied` and settled once, by `settle_change`. Triggers refuse
every other UPDATE and every DELETE. The experience book is append-only; `experience` only ranks."""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.usage import month_start
from evowiki.teach.memory import HALF_LIFE_DAYS

RoundStatus = Literal["running", "applied", "asked", "thin", "failed", "no_change", "skipped",
                      "error"]
ChangeStatus = Literal["applied", "asked", "rejected", "undone", "rolled_back"]
Verdict = Literal["effective", "ineffective", "harmful"]
Scope = Literal["user", "general"]


@dataclass(frozen=True)
class RoundRow:
    round_id: str
    user_id: str
    started_at: datetime
    finished_at: datetime | None
    status: RoundStatus
    reason: str | None
    finding: str | None
    signal_ids: Any
    cases: Any
    candidates: Any
    comparisons: Any
    decision: Any
    diff: Any
    cost: float | None
    improver_run_id: str | None


@dataclass(frozen=True)
class ChangeRow:
    change_id: str
    user_id: str
    round_id: str
    version: int | None
    diff: Any
    status: ChangeStatus
    cost_before: float | None
    cost_after: float | None
    reason: str | None
    created_at: datetime
    settled_at: datetime | None


@dataclass(frozen=True)
class ExpRow:
    exp_id: str
    user_id: str
    situation: Any
    change: Any
    evidence: Any
    verdict: Verdict
    scope: Scope
    created_at: datetime
    weight: float


def _id() -> str:
    return uuid.uuid4().hex


def _json(v: Any) -> Jsonb | None:
    return None if v is None else Jsonb(v)


def start_round(conn: psycopg.Connection, user_id: str, *, finding: str | None,
                now: datetime) -> str:
    """Opens a `running` round on `finding` (the short description used to spot two thin rounds
    in a row on the same finding) and returns its id."""
    rid = _id()
    conn.execute("insert into harness_round (round_id, user_id, started_at, status, finding) "
                 "values (%s, %s, %s, 'running', %s)", (rid, user_id, now, finding))
    return rid


def finish_round(conn: psycopg.Connection, round_id: str, status: RoundStatus, *, reason: str,
                 signal_ids: Any = None, cases: Any = None, candidates: Any = None,
                 comparisons: Any = None, decision: Any = None, diff: Any = None,
                 cost: float | None = None, improver_run_id: str | None = None,
                 now: datetime) -> None:
    """The one allowed update: running -> a final status, with the result columns."""
    if status in ("applied", "asked") and diff is not None:
        # Freeze the intended effect before any online observations. Approval keeps this
        # immutable round record; outcomes go to the existing append-only observations.
        choice = decision or {}
        decision = {**choice, "expectation": {
            "feedback": signal_ids or [], "cases": cases,
            "citations": choice.get("citations", []),
            "rationale": choice.get("rationale") or reason}}
    cur = conn.execute(
        "update harness_round set status = %s, reason = %s, finished_at = %s, signal_ids = %s, "
        "cases = %s, candidates = %s, comparisons = %s, decision = %s, diff = %s, cost = %s, "
        "improver_run_id = %s where round_id = %s",
        (status, reason, now, _json(signal_ids), _json(cases), _json(candidates),
         _json(comparisons), _json(decision), _json(diff), cost, improver_run_id, round_id))
    if cur.rowcount != 1:
        raise LookupError(f"harness round {round_id} not found")


def rounds(conn: psycopg.Connection, user_id: str, limit: int) -> list[RoundRow]:
    """Newest first."""
    return [RoundRow(**r) for r in fetch_all(
        conn, "select * from harness_round where user_id = %s "
              "order by started_at desc, round_id limit %s", (user_id, limit))]


def last_round(conn: psycopg.Connection, user_id: str) -> RoundRow | None:
    """The newest round of any status (what the due rule counts new signals from)."""
    got = rounds(conn, user_id, 1)
    return got[0] if got else None


# The statuses of a round that decided on its evidence (the improver ran, or the gate judged the
# composed diff): such a round consumes the signals it read. thin (not enough evidence), skipped
# (nothing ran), error (nothing was decided) and running do not: their signals stay for the next
# round, so the evidence accumulates (design §5.6).
CONSUMING: tuple[RoundStatus, ...] = ("applied", "asked", "failed", "no_change")


def last_consuming_round(conn: psycopg.Connection, user_id: str) -> RoundRow | None:
    """The newest round whose status is in CONSUMING: a round reads the signals since its start."""
    row = fetch_one(
        conn, "select * from harness_round where user_id = %s and status = any(%s) "
              "order by started_at desc, round_id limit 1", (user_id, list(CONSUMING)))
    return RoundRow(**row) if row else None


def record_change(conn: psycopg.Connection, user_id: str, round_id: str, diff: Any,
                  status: Literal["applied", "asked"], *, version: int | None = None,
                  cost_before: float | None = None, cost_after: float | None = None,
                  reason: str | None = None, now: datetime) -> str:
    """Records a proposed (`asked`) or already applied change; `version` is the harness_version
    it produced (applied only). Returns the change id."""
    cid = _id()
    conn.execute(
        "insert into harness_change (change_id, user_id, round_id, version, diff, status, "
        "cost_before, cost_after, reason, created_at, settled_at) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (cid, user_id, round_id, version, Jsonb(diff), status, cost_before, cost_after, reason,
         now, now if status == "applied" else None))
    return cid


def settle_change(conn: psycopg.Connection, change_id: str, status: ChangeStatus, *,
                  version: int | None = None, now: datetime) -> None:
    """asked -> applied | rejected, applied -> undone | rolled_back; `version` only on apply."""
    cur = conn.execute("update harness_change set status = %s, version = coalesce(%s, version), "
                       "settled_at = %s where change_id = %s", (status, version, now, change_id))
    if cur.rowcount != 1:
        raise LookupError(f"harness change {change_id} not found")


def get_round(conn: psycopg.Connection, user_id: str, round_id: str) -> RoundRow | None:
    r = fetch_one(conn, "select * from harness_round where round_id = %s and user_id = %s",
                  (round_id, user_id))
    return None if r is None else RoundRow(**r)


def get_change(conn: psycopg.Connection, user_id: str, change_id: str) -> ChangeRow | None:
    r = fetch_one(conn, "select * from harness_change where change_id = %s and user_id = %s",
                  (change_id, user_id))
    return None if r is None else ChangeRow(**r)


def change_log(conn: psycopg.Connection, user_id: str, *, limit: int) -> list[dict]:
    """Read intentions and every observed result, including pending/no_effect.

    No new table: harness_round.decision holds the frozen expectation, harness_change the
    approval/rollback status, harness_observation the measured signals and refusal reasons.
    Never infers a result from missing observations or changes any setting.
    """
    rows = fetch_all(conn, "select * from harness_change where user_id = %s "
                     "and status in ('applied', 'undone', 'rolled_back') "
                     "order by created_at desc, change_id limit %s", (user_id, limit))
    out = []
    for row in rows:
        change = ChangeRow(**row)
        rnd = get_round(conn, user_id, change.round_id)
        expected = (rnd.decision or {}).get("expectation") if rnd else None
        points = observations(conn, user_id, change.change_id)
        out.append({"change_id": change.change_id, "expectation": expected,
                    "status": change.status, "rolled_back": change.status == "rolled_back",
                    "results": [{"verdict": p.verdict, "deltas": p.deltas,
                                 "reason": p.reason, "note": p.note,
                                 "dropped": p.dropped, "at": p.at.isoformat()} for p in points]})
    return out


def asked_changes(conn: psycopg.Connection, user_id: str) -> list[ChangeRow]:
    """Changes waiting for the user's approval (status asked), newest first."""
    return [ChangeRow(**r) for r in fetch_all(
        conn, "select * from harness_change where user_id = %s and status = 'asked' "
              "order by created_at desc, change_id", (user_id,))]


def applied_changes(conn: psycopg.Connection, user_id: str) -> list[ChangeRow]:
    """Changes currently in force (status applied), newest first."""
    return [ChangeRow(**r) for r in fetch_all(
        conn, "select * from harness_change where user_id = %s and status = 'applied' "
              "order by created_at desc, change_id", (user_id,))]


# the changes the improver may not propose again (`harness.diff.refused_keys`): the user said no
# to it, undid it, or it was rolled back as harmful. A `rejected` change counts only when the
# user rejected its ledger row (`round.ledger_key`): an approval closed because the pack / spec
# moved meanwhile or the rule guard blocked it is settled `rejected` too, but nobody said no to it
REFUSED: tuple[ChangeStatus, ...] = ("rejected", "undone", "rolled_back")


def refused_changes(conn: psycopg.Connection, user_id: str) -> list[ChangeRow]:
    """Changes in REFUSED, newest first."""
    return [ChangeRow(**r) for r in fetch_all(
        conn, "select c.* from harness_change c where c.user_id = %s and c.status = any(%s) "
              "and (c.status <> 'rejected' or exists ("
              "  select 1 from agent_action a where a.idempotency_key = "
              "  'harness:' || c.user_id || ':' || c.change_id and a.status = 'rejected')) "
              "order by c.created_at desc, c.change_id", (user_id, list(REFUSED)))]


def change_of_round(conn: psycopg.Connection, user_id: str, round_id: str) -> ChangeRow | None:
    """The change a round recorded (a round records at most one), None without one."""
    r = fetch_one(conn, "select * from harness_change where user_id = %s and round_id = %s "
                        "order by created_at desc, change_id limit 1", (user_id, round_id))
    return None if r is None else ChangeRow(**r)


def add_experience(conn: psycopg.Connection, user_id: str, situation: Mapping[str, Any],
                   change: Any, evidence: Any, verdict: Verdict, scope: Scope, *,
                   weight: float, now: datetime) -> str:
    eid = _id()
    conn.execute(
        "insert into harness_experience (exp_id, user_id, situation, change, evidence, verdict, "
        "scope, created_at, weight) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (eid, user_id, Jsonb(dict(situation)), Jsonb(change), Jsonb(evidence), verdict, scope,
         now, weight))
    return eid


def experience(conn: psycopg.Connection, user_id: str, *, query: Mapping[str, Any] | None,
               limit: int, now: datetime) -> list[ExpRow]:
    """Lessons whose situation contains `query` (all when None), ranked by
    weight * 0.5 ** (age_days / HALF_LIFE_DAYS), best first. Ranking only; nothing is deleted."""
    rows = fetch_all(
        conn,
        "select * from harness_experience where user_id = %s and situation @> %s "
        "order by weight * power(0.5, greatest(extract(epoch from (%s::timestamptz - created_at)), 0) "
        "/ 86400.0 / %s) desc, created_at desc, exp_id limit %s",
        (user_id, Jsonb(dict(query or {})), now, HALF_LIFE_DAYS, limit))
    return [ExpRow(**r) for r in rows]


def month_cost(conn: psycopg.Connection, user_id: str, now: datetime, tz: tzinfo) -> float:
    """Sum of harness_round.cost for rounds started this month (in `tz`)."""
    r = fetch_one(conn, "select coalesce(sum(cost), 0) as c from harness_round "
                        "where user_id = %s and started_at >= %s",
                  (user_id, month_start(now, tz)))
    return float(r["c"]) if r else 0.0


@dataclass(frozen=True)
class ObsRow:
    obs_id: str
    seq: int
    user_id: str
    change_id: str
    at: datetime
    verdict: Literal["pending", "helpful", "harmful", "no_effect"]
    deltas: Any                       # list of AnchorDelta dumps
    note: str
    reason: str | None
    dropped: Any                      # {"before": {...}, "after": {...}} counts
    lesson_id: str | None


def add_observation(conn: psycopg.Connection, user_id: str, change_id: str, verdict: str,
                    deltas: Any, *, note: str = "", reason: str | None = None,
                    dropped: Any = None, lesson_id: str | None = None, now: datetime) -> str:
    """Appends one observation point of a change (append-only)."""
    oid = _id()
    conn.execute(
        "insert into harness_observation (obs_id, user_id, change_id, at, verdict, deltas, note, "
        "reason, dropped, lesson_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (oid, user_id, change_id, now, verdict, Jsonb(deltas), note, reason, Jsonb(dropped or {}),
         lesson_id))
    return oid


def observations(conn: psycopg.Connection, user_id: str, change_id: str) -> list[ObsRow]:
    """The change's observation points, oldest first (by time, then insertion)."""
    return [ObsRow(**r) for r in fetch_all(
        conn, "select * from harness_observation where user_id = %s and change_id = %s "
              "order by at, seq", (user_id, change_id))]


@dataclass(frozen=True)
class FastCheckRow:
    check_id: str
    seq: int
    user_id: str
    change_id: str
    at: datetime
    outcome: Literal["rolled_back", "refused"]
    reason: str | None
    evidence: Any
    lesson_id: str | None


def add_fast_check(conn: psycopg.Connection, user_id: str, change_id: str,
                   outcome: Literal["rolled_back", "refused"], evidence: Any, *,
                   reason: str | None = None, lesson_id: str | None = None,
                   now: datetime) -> str:
    """Appends one action of the fast rollback channel on a change (migration 0084)."""
    fid = "hf_" + uuid.uuid4().hex
    conn.execute(
        "insert into harness_fast_check (check_id, user_id, change_id, at, outcome, reason, "
        "evidence, lesson_id) values (%s, %s, %s, %s, %s, %s, %s, %s)",
        (fid, user_id, change_id, now, outcome, reason, Jsonb(evidence), lesson_id))
    return fid


def fast_checks(conn: psycopg.Connection, user_id: str, change_id: str) -> list[FastCheckRow]:
    """The change's fast-channel actions, oldest first."""
    return [FastCheckRow(**r) for r in fetch_all(
        conn, "select * from harness_fast_check where user_id = %s and change_id = %s "
              "order by at, seq", (user_id, change_id))]


@dataclass(frozen=True)
class GateRow:
    gate_id: str
    user_id: str
    round_id: str
    subject: str
    verdict: str
    reasons: Any
    detail: Any
    at: datetime


def record_gate(conn: psycopg.Connection, user_id: str, round_id: str, subject: str,
                verdict: Literal["pass", "thin", "fail"], reasons: Sequence[str],
                detail: Mapping[str, Any], *, now: datetime) -> str:
    """Appends one gate decision (append-only) and returns its ref `hg_<hex>`.
    `subject`: 'diff', 'tool:<cid>' or 'patch:<cid>'."""
    gid = "hg_" + uuid.uuid4().hex
    conn.execute(
        "insert into harness_gate (gate_id, user_id, round_id, subject, verdict, reasons, detail, "
        "at) values (%s, %s, %s, %s, %s, %s, %s, %s)",
        (gid, user_id, round_id, subject, verdict, Jsonb(list(reasons)), Jsonb(dict(detail)), now))
    return gid


def prior_aa(conn: psycopg.Connection, user_id: str, limit: int = 20) -> list[str]:
    """The A/A winners of this user's last gate rows that ran one (`harness.evidence`), oldest first."""
    rows = fetch_all(
        conn, "select detail->'evidence'->'aa'->'round' as winners from harness_gate "
        "where user_id = %s and detail->'evidence'->'aa' is not null order by at desc limit %s",
        (user_id, limit))
    return [w for r in reversed(rows) for w in (r["winners"] or [])]


def get_gate(conn: psycopg.Connection, user_id: str, gate_ref: str) -> GateRow | None:
    r = fetch_one(conn, "select * from harness_gate where gate_id = %s and user_id = %s",
                  (gate_ref, user_id))
    return None if r is None else GateRow(**r)


# --- landed code patches a harness round evaluated (harness 3b, Task 10) ----------------------

def add_patch_observation(conn: psycopg.Connection, user_id: str, candidate_id: str,
                          verdict: str, deltas: Any, *, note: str = "",
                          reason: str | None = None, dropped: Any = None,
                          lesson_id: str | None = None, now: datetime) -> str:
    """Appends one observation point of a landed code patch (append-only)."""
    oid = _id()
    conn.execute(
        "insert into harness_patch_observation (obs_id, user_id, candidate_id, at, verdict, "
        "deltas, note, reason, dropped, lesson_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (oid, user_id, candidate_id, now, verdict, Jsonb(deltas), note, reason,
         Jsonb(dropped or {}), lesson_id))
    return oid


def patch_observations(conn: psycopg.Connection, user_id: str, candidate_id: str
                       ) -> list[ObsRow]:
    """The patch's observation points, oldest first, as ObsRow (`change_id` = the candidate id)."""
    return [ObsRow(**r) for r in fetch_all(
        conn, "select obs_id, seq, user_id, candidate_id as change_id, at, verdict, deltas, note, "
              "reason, dropped, lesson_id from harness_patch_observation "
              "where user_id = %s and candidate_id = %s order by at, seq", (user_id, candidate_id))]


def patch_landed_at(conn: psycopg.Connection, candidate_id: str) -> datetime | None:
    """When the forge supervisor last landed the patch (its newest `land` event), or None."""
    r = fetch_one(conn, "select max(created_at) as at from forge_live_event "
                        "where candidate_id = %s and kind = 'land'", (candidate_id,))
    return None if r is None else r["at"]
