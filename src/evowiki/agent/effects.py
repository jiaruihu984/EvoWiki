"""Exactly-once database side effects on top of at-least-once DBOS steps (spec 2026-10-14 §4).

`apply_once` runs inside the caller's transaction: it looks the key up with `select … for update`.
A key that is already there with the same payload hash returns the recorded result and never calls
`fn`; the same key with a different payload is a conflict (fail closed — never a silent success).
`fn`'s writes and the key row commit or roll back together. Two transactions racing on a brand-new
key: the second insert raises UniqueViolation and rolls back; the DBOS step retries and sees the row."""

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class EffectConflict(RuntimeError):
    """The idempotency key was already used for a different payload."""


def payload_hash(payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_once(conn: psycopg.Connection, key: str, payload: Mapping[str, Any],
               fn: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
    h = payload_hash(payload)
    with conn.cursor(row_factory=dict_row) as cur:     # the pool's connections use dict rows too
        row = cur.execute("select payload_hash, result from effect_applied where key = %s "
                          "for update", (key,)).fetchone()
    if row is not None:
        if row["payload_hash"] != h:
            raise EffectConflict(f"idempotency key {key!r} was used for a different payload")
        return dict(row["result"])
    result = dict(fn())
    conn.execute("insert into effect_applied (key, payload_hash, result) values (%s, %s, %s)",
                 (key, h, Jsonb(result)))
    return result
