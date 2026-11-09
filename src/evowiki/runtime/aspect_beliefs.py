"""Aspect K as the replay of the aspect_event log (plan rev 3.1 Phase 1). New events are
validated against the replayed state before anything is appended, so the log only ever holds
events that apply cleanly and replay == the in-memory run. `trusted`: the (prereq, concept) pairs
K inference may follow (`AspectK`, relation layers); None = every stored prereq."""

from collections.abc import Iterable, Sequence
from datetime import datetime

import psycopg

from evowiki.belief.aspects import (
    AspectConfig,
    AspectEvent,
    AspectK,
    AspectKView,
    TrustedPrereqs,
)
from evowiki.store.aspects import append_aspect_event, aspect_events
from evowiki.store.db import fetch_one
from evowiki.store.locks import UserTx


def replay_aspects(config: AspectConfig, events: Iterable[AspectEvent], *,
                   trusted: TrustedPrereqs | None = None) -> AspectK:
    k = AspectK(config, trusted=trusted)
    for ev in events:
        k.apply(ev)
    return k


def load_aspects(conn: psycopg.Connection, user_id: str, config: AspectConfig, *,
                 upto_seq: int | None = None, trusted: TrustedPrereqs | None = None) -> AspectK:
    return replay_aspects(config, (s.event for s in aspect_events(conn, user_id,
                                                                    upto_seq=upto_seq)),
                          trusted=trusted)


def _stored(tx: UserTx, event_id: str) -> bool:
    return fetch_one(tx.conn, "select 1 as x from aspect_event where event_id = %s",
                     (event_id,)) is not None


def record_aspect_events(tx: UserTx, config: AspectConfig, events: Sequence[AspectEvent], *,
                         trusted: TrustedPrereqs | None = None) -> AspectK:
    """All-or-nothing: every new event must apply, else AspectRejected and nothing is stored.
    An event already in the log is handed to append (which checks it is an identical retry)
    and not re-applied. This is the only sanctioned writer of the aspect_event log.

    Raises (in every case nothing from the batch is appended):
    - AspectRejected: an event does not apply to the replayed state (unknown cell, nothing to
      revoke, no live note, ...). An identical duplicate of a not-yet-stored event_id inside
      ONE batch is also rejected: the first copy was applied, the second is "already applied".
    - AspectEventConflict: an event_id already in the log is reused with different content
      (or belongs to another user).
    A retry ACROSS batches (event_id already stored with identical content) is idempotent: it is
    skipped, not re-applied and not re-appended."""
    k = load_aspects(tx.conn, tx.user_id, config, trusted=trusted)
    fresh = []
    for ev in events:
        if _stored(tx, ev.event_id):
            append_aspect_event(tx, ev)          # idempotent, or AspectEventConflict
            continue
        k.apply(ev)                              # raises before any append below
        fresh.append(ev)
    for ev in fresh:
        append_aspect_event(tx, ev)
    return k


def read_kview(conn: psycopg.Connection, user_id: str, config: AspectConfig,
               as_of: datetime) -> AspectKView:
    return AspectKView(load_aspects(conn, user_id, config), as_of)
