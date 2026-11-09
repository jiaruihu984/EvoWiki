"""L2 code patches in Postgres (migration 0069, design 2026-10-22 §5.3). The only writer of
forge_patch / forge_live_event / forge_live_request. Plain functions over a connection; the caller
owns the transaction. forge_live_event is append-only (trigger)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from evowiki.store.db import fetch_all, fetch_one

PatchState = Literal["built", "awaiting_approval", "queued", "landed", "canary_failed",
                     "revert_queued", "reverted", "withdrawn"]


@dataclass(frozen=True)
class PatchRow:
    candidate_id: str
    user_id: str | None
    title: str
    base_commit: str
    commit: str                      # the candidate commit on branch forge/<candidate_id>
    branch: str
    state: PatchState
    live_commit: str | None          # its cherry-pick on rsi/live
    revert_commit: str | None
    reason: str                      # last plain reason (canary failure, conflict, ...)
    gate_ref: str | None
    cost_usd: float | None           # build + replay
    created_at: datetime
    updated_at: datetime


def insert_patch(conn: psycopg.Connection, row: PatchRow) -> None:
    conn.execute(
        "insert into forge_patch (candidate_id, user_id, title, base_commit, commit, branch, "
        "state, live_commit, revert_commit, reason, gate_ref, cost_usd, created_at, updated_at) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (row.candidate_id, row.user_id, row.title, row.base_commit, row.commit, row.branch,
         row.state, row.live_commit, row.revert_commit, row.reason, row.gate_ref, row.cost_usd,
         row.created_at, row.updated_at))


def get_patch(conn: psycopg.Connection, candidate_id: str) -> PatchRow | None:
    r = fetch_one(conn, "select * from forge_patch where candidate_id = %s", (candidate_id,))
    return None if r is None else PatchRow(**r)


def set_state(conn: psycopg.Connection, candidate_id: str, state: PatchState, *,
              now: datetime, reason: str = "", live_commit: str | None = None,
              revert_commit: str | None = None, gate_ref: str | None = None) -> None:
    """Move to `state`. reason is replaced each time; the commit / gate_ref columns keep their old
    value unless a new one is given."""
    conn.execute(
        "update forge_patch set state = %s, reason = %s, updated_at = %s, "
        "live_commit = coalesce(%s, live_commit), revert_commit = coalesce(%s, revert_commit), "
        "gate_ref = coalesce(%s, gate_ref) where candidate_id = %s",
        (state, reason, now, live_commit, revert_commit, gate_ref, candidate_id))


def patches(conn: psycopg.Connection, *, states: Sequence[PatchState] | None = None,
            user_id: str | None = None, limit: int = 200) -> list[PatchRow]:
    """Oldest first."""
    where, params = [], []
    if states is not None:
        where.append("state = any(%s)")
        params.append(list(states))
    if user_id is not None:
        where.append("user_id = %s")
        params.append(user_id)
    sql = "select * from forge_patch"
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by created_at, candidate_id limit %s"
    params.append(limit)
    return [PatchRow(**r) for r in fetch_all(conn, sql, params)]


def add_cost(conn: psycopg.Connection, candidate_id: str, usd: float | None) -> None:
    """Add to the patch's cost. None makes the cost null (unknown), and it stays unknown."""
    conn.execute(
        "update forge_patch set cost_usd = case when %s::float8 is null then null "
        "else cost_usd + %s::float8 end where candidate_id = %s",
        (usd, usd, candidate_id))


def log_event(conn: psycopg.Connection, kind: str, *, candidate_id: str | None,
              detail: dict[str, Any], now: datetime) -> None:
    conn.execute(
        "insert into forge_live_event (kind, candidate_id, detail, created_at) "
        "values (%s, %s, %s, %s)", (kind, candidate_id, Jsonb(detail), now))


def events(conn: psycopg.Connection, *, limit: int = 100) -> list[dict]:
    """Newest first."""
    return fetch_all(conn, "select * from forge_live_event order by id desc limit %s", (limit,))


def set_live_request(conn: psycopg.Connection, op: Literal["revert_all", "rebase"], *,
                     by: str, now: datetime) -> None:
    conn.execute("insert into forge_live_request (op, by_user, created_at) values (%s, %s, %s)",
                 (op, by, now))


def take_live_requests(conn: psycopg.Connection) -> list[dict]:
    """Mark every open request taken and return them, oldest first."""
    rows = fetch_all(
        conn, "update forge_live_request set taken = true where not taken "
        "returning id, op, by_user, created_at")
    return sorted(rows, key=lambda r: r["id"])
