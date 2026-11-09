"""The harness pack in Postgres (migration 0063, design 2026-10-21 §3.2). Plain functions over a
connection; the caller owns the transaction. Every change appends one harness_version row; the
user's pack version is the highest of them."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from evowiki.harness.pack import HarnessPack, Value, validate
from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.locks import xact_lock

Source = Literal["user", "improver", "rollback"]


@dataclass(frozen=True)
class VersionRow:
    version: int
    key: str
    before: Any
    after: Any
    source: Source
    why: str
    created_at: datetime


def lock_key(conn: psycopg.Connection, user_id: str) -> None:
    xact_lock(conn, f"harness:{user_id}")


def _version(conn: psycopg.Connection, user_id: str) -> int:
    r = fetch_one(conn, "select coalesce(max(version), 0) as v from harness_version "
                        "where user_id = %s", (user_id,))
    return int(r["v"]) if r else 0


def load_pack(conn: psycopg.Connection, user_id: str) -> HarnessPack:
    rows = fetch_all(conn, "select key, value from harness_value where user_id = %s", (user_id,))
    return HarnessPack(_version(conn, user_id), {r["key"]: r["value"] for r in rows})


def source_of(conn: psycopg.Connection, user_id: str) -> dict[str, str]:
    rows = fetch_all(conn, "select key, source from harness_value where user_id = %s",
                     (user_id,))
    return {r["key"]: r["source"] for r in rows}


def set_value(conn: psycopg.Connection, user_id: str, name: str, value: Value | None, *,
              source: Source, why: str, now: datetime) -> int | None:
    new = None if value is None else validate(name, value)
    lock_key(conn, user_id)        # one writer per user: versions stay dense and ordered
    r = fetch_one(conn, "select value from harness_value where user_id = %s and key = %s",
                  (user_id, name))
    before = None if r is None else r["value"]
    if before == new:
        return None
    version = _version(conn, user_id) + 1
    conn.execute(
        "insert into harness_version (user_id, version, key, before, after, source, why, "
        "created_at) values (%s, %s, %s, %s, %s, %s, %s, %s)",
        (user_id, version, name, None if before is None else Jsonb(before),
         None if new is None else Jsonb(new), source, why, now))
    if new is None:
        conn.execute("delete from harness_value where user_id = %s and key = %s",
                     (user_id, name))
    else:
        conn.execute(
            "insert into harness_value (user_id, key, value, source, version, updated_at) "
            "values (%s, %s, %s, %s, %s, %s) on conflict (user_id, key) do update set "
            "value = excluded.value, source = excluded.source, version = excluded.version, "
            "updated_at = excluded.updated_at",
            (user_id, name, Jsonb(new), source, version, now))
    return version


def pack_at(conn: psycopg.Connection, user_id: str, version: int) -> HarnessPack:
    """The user's pack as it was at `version` (what a run that recorded that pack version read):
    every harness_version row up to it, applied in order (version 0 = the empty pack). LookupError
    for a version the user's history does not reach."""
    if version < 0 or version > _version(conn, user_id):
        raise LookupError(f"没有这个手册版本：{version}")
    rows = fetch_all(conn, "select key, after from harness_version where user_id = %s "
                           "and version <= %s order by version", (user_id, version))
    values: dict[str, Value] = {}
    for r in rows:
        if r["after"] is None:
            values.pop(r["key"], None)
        else:
            values[r["key"]] = r["after"]
    return HarnessPack(version, values)


def history(conn: psycopg.Connection, user_id: str, *, limit: int = 50) -> list[VersionRow]:
    rows = fetch_all(conn, "select version, key, before, after, source, why, created_at "
                           "from harness_version where user_id = %s order by version desc "
                           "limit %s", (user_id, limit))
    return [VersionRow(**r) for r in rows]
