"""Append-only personal-wiki event log (migration 0013). Wiki state
(pages, drafts, curator ledger, settings, guidelines, usage) is the replay of wiki_event; K
evidence stays in aspect_event."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import NamedTuple

import psycopg
from psycopg.types.json import Jsonb

from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.locks import UserTx


class WikiEventConflict(ValueError):
    pass


class StoredWikiEvent(NamedTuple):
    seq: int
    event_id: str
    occurred_at: datetime
    type: str
    payload: dict


def _event(r: dict) -> StoredWikiEvent:
    return StoredWikiEvent(r["seq"], r["event_id"], r["occurred_at"].astimezone(UTC), r["type"],
                           r["payload"])


def last_wiki_seq(conn: psycopg.Connection, user_id: str) -> int:
    r = fetch_one(conn, "select coalesce(max(seq), 0) as s from wiki_event where user_id = %s",
                  (user_id,))
    return r["s"]


def append_wiki_events(tx: UserTx,
                       rows: Sequence[tuple[str, datetime, str, dict]]) -> list[int]:
    """Append (event_id, occurred_at, type, payload) rows under the user's lock, numbered from
    last_wiki_seq + 1. Any event_id already stored (for any user) or repeated within the batch
    raises WikiEventConflict: wiki events are snapshots, never retried with the same id."""
    if not rows:
        return []
    ids = [r[0] for r in rows]
    if len(set(ids)) != len(ids):
        raise WikiEventConflict("wiki event ids repeated within one batch")
    taken = fetch_all(tx.conn, "select event_id from wiki_event where event_id = any(%s)", (ids,))
    if taken:
        raise WikiEventConflict(
            f"wiki event ids already stored: {sorted(r['event_id'] for r in taken)}")
    first = last_wiki_seq(tx.conn, tx.user_id) + 1
    seqs = list(range(first, first + len(rows)))
    with tx.conn.cursor() as cur:
        cur.executemany(
            "insert into wiki_event (user_id, seq, event_id, occurred_at, type, payload) "
            "values (%s, %s, %s, %s, %s, %s)",
            [(tx.user_id, seq, eid, at, typ, Jsonb(payload))
             for seq, (eid, at, typ, payload) in zip(seqs, rows, strict=True)])
    return seqs


def wiki_events(conn: psycopg.Connection, user_id: str, *,
                upto_seq: int | None = None) -> list[StoredWikiEvent]:
    rows = fetch_all(
        conn,
        "select * from wiki_event where user_id = %s and (%s::bigint is null or seq <= %s) "
        "order by seq",
        (user_id, upto_seq, upto_seq))
    return [_event(r) for r in rows]


def wiki_events_after(conn: psycopg.Connection, user_id: str,
                      after_seq: int) -> list[StoredWikiEvent]:
    """The user's wiki events with seq > `after_seq`, in order."""
    rows = fetch_all(conn, "select * from wiki_event where user_id = %s and seq > %s "
                           "order by seq", (user_id, after_seq))
    return [_event(r) for r in rows]


def wiki_events_of_type(conn: psycopg.Connection, user_id: str, types: Sequence[str], *,
                        after_seq: int = 0) -> list[StoredWikiEvent]:
    """The user's wiki events whose type is one of `types` and seq > `after_seq`, in seq order
    (e.g. every `teaching_spec_saved`, to read back an old teaching-spec version)."""
    rows = fetch_all(conn, "select * from wiki_event where user_id = %s and type = any(%s) "
                           "and seq > %s order by seq", (user_id, list(types), after_seq))
    return [_event(r) for r in rows]


# ---- node graph migration backup (migration 0031) -------------------------------------------

def latest_graph_action(conn: psycopg.Connection, user_id: str) -> str | None:
    """The action of the user's latest `graph_saved` event (None: never saved). A cheap check
    that spares a full replay when the graph is already there."""
    r = fetch_one(conn, "select payload->>'action' as a from wiki_event where user_id = %s "
                        "and type = 'graph_saved' order by seq desc limit 1", (user_id,))
    return None if r is None else r["a"]




class StoredSnapshot(NamedTuple):
    wiki_seq: int
    reducer: str
    state: dict


def wiki_snapshot(conn: psycopg.Connection, user_id: str) -> StoredSnapshot | None:
    """The user's cached wiki state (migration 0078), or None."""
    r = fetch_one(conn, "select wiki_seq, reducer, state from wiki_snapshot where user_id = %s",
                  (user_id,))
    return None if r is None else StoredSnapshot(r["wiki_seq"], r["reducer"], r["state"])


def put_wiki_snapshot(conn: psycopg.Connection, user_id: str, wiki_seq: int, reducer: str,
                      state: dict) -> None:
    """Stores the state after `wiki_seq`, unless a newer one is stored already."""
    conn.execute("insert into wiki_snapshot (user_id, wiki_seq, reducer, state) "
                 "values (%s, %s, %s, %s) on conflict (user_id) do update set "
                 "wiki_seq = excluded.wiki_seq, reducer = excluded.reducer, "
                 "state = excluded.state, updated_at = now() "
                 "where wiki_snapshot.wiki_seq <= excluded.wiki_seq "
                 "or wiki_snapshot.reducer <> excluded.reducer",
                 (user_id, wiki_seq, reducer, Jsonb(state)))
