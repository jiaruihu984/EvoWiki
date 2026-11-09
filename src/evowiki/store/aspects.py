"""Append-only aspect-K event log (migration 0012). The log is the source of truth; aspect K is
its replay (runtime/aspect_beliefs.py)."""

import json
from datetime import UTC, datetime
from typing import NamedTuple

import psycopg
from psycopg.types.json import Jsonb

from evowiki.belief.aspects import AspectEvent, AspectEventAdapter
from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.locks import UserTx

_ENVELOPE = {"event_id", "ts", "type"}


class AspectEventConflict(ValueError):
    pass


class StoredAspectEvent(NamedTuple):
    seq: int
    event: AspectEvent


def _payload(ev: AspectEvent) -> dict:
    return ev.model_dump(mode="json", exclude=_ENVELOPE)


def _event(r: dict) -> StoredAspectEvent:
    ev = AspectEventAdapter.validate_python({
        "event_id": r["event_id"], "ts": r["occurred_at"].astimezone(UTC), "type": r["type"],
        **r["payload"]})
    return StoredAspectEvent(r["seq"], ev)


def _canon(ev: AspectEvent) -> str:
    return json.dumps(ev.model_dump(mode="json", exclude={"ts"}), sort_keys=True)


def last_aspect_seq(conn: psycopg.Connection, user_id: str) -> int:
    r = fetch_one(conn, "select coalesce(max(seq), 0) as s from aspect_event where user_id = %s",
                  (user_id,))
    return r["s"]


def observe_count_since(conn: psycopg.Connection, user_id: str, since: datetime | None) -> int:
    """Observe events of the user with occurred_at > `since` (None = all): the RSI tick's cheap
    signal count, the same count as `wikiapp.rsi_data.user_data(...).signals_since`, without
    loading or replaying the log."""
    r = fetch_one(conn,
                  "select count(*) as n from aspect_event where user_id = %s and type = 'observe' "
                  "and (%s::timestamptz is null or occurred_at > %s)",
                  (user_id, since, since))
    return r["n"]


def append_aspect_event(tx: UserTx, ev: AspectEvent) -> tuple[StoredAspectEvent, bool]:
    """INTERNAL: appends without checking that the event applies to the replayed state. Callers
    must go through `runtime.aspect_beliefs.record_aspect_events`, which validates first: the log
    is append-only, so an event that does not apply could never be removed and would break every
    later replay.

    Append under the user's lock. Same event_id + same content -> idempotent no-op; the same
    event_id with other content, or owned by another user, raises AspectEventConflict."""
    r = fetch_one(tx.conn, "select * from aspect_event where event_id = %s", (ev.event_id,))
    if r is not None:
        old = _event(r)
        if r["user_id"] != tx.user_id or old.event.ts != ev.ts or _canon(old.event) != _canon(ev):
            raise AspectEventConflict(f"aspect event {ev.event_id} reused with different content")
        return old, False
    seq = last_aspect_seq(tx.conn, tx.user_id) + 1
    row = fetch_one(
        tx.conn,
        "insert into aspect_event (user_id, seq, event_id, occurred_at, type, payload) "
        "values (%s, %s, %s, %s, %s, %s) returning *",
        (tx.user_id, seq, ev.event_id, ev.ts, ev.type, Jsonb(_payload(ev))))
    return _event(row), True


def aspect_events(conn: psycopg.Connection, user_id: str, *,
                  upto_seq: int | None = None) -> list[StoredAspectEvent]:
    rows = fetch_all(
        conn,
        "select * from aspect_event where user_id = %s and (%s::bigint is null or seq <= %s) "
        "order by seq",
        (user_id, upto_seq, upto_seq))
    return [_event(r) for r in rows]


def aspect_events_after(conn: psycopg.Connection, user_id: str,
                        after_seq: int) -> list[StoredAspectEvent]:
    """The user's aspect events with seq > `after_seq`, in order."""
    rows = fetch_all(conn, "select * from aspect_event where user_id = %s and seq > %s "
                           "order by seq", (user_id, after_seq))
    return [_event(r) for r in rows]
