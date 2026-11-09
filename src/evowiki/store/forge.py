"""The forge archive and tool registry in Postgres (migration 0067, design 2026-10-22). Plain
functions over a connection; the caller owns the transaction. Candidates are never deleted: failed
builds stay in the archive with their parent (DGM). The registry is append-only versions."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.locks import xact_lock

Kind = Literal["tool", "patch"]
Status = Literal["building", "built", "build_failed", "promoted", "rolled_back", "withdrawn"]


@dataclass(frozen=True)
class CandidateRow:
    id: str
    user_id: str | None
    kind: Kind
    name: str | None
    parent_id: str | None
    spec: Any
    evidence: Any
    files: dict[str, str]
    build: Any
    status: Status
    run_id: str | None
    cost_usd: float | None
    created_at: datetime
    updated_at: datetime
    origin: str = "round"


@dataclass(frozen=True)
class RegistryRow:
    scope: str
    version: int
    entries: dict[str, str]
    change: dict[str, Any]
    created_at: datetime


def _cand(r: dict) -> CandidateRow:
    return CandidateRow(**r)


def insert_candidate(conn: psycopg.Connection, c: CandidateRow) -> None:
    conn.execute(
        "insert into forge_candidate (id, user_id, kind, name, parent_id, spec, evidence, files, "
        "build, status, run_id, cost_usd, created_at, updated_at, origin) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (c.id, c.user_id, c.kind, c.name, c.parent_id, Jsonb(c.spec), Jsonb(c.evidence),
         Jsonb(c.files), None if c.build is None else Jsonb(c.build), c.status, c.run_id,
         c.cost_usd, c.created_at, c.updated_at, c.origin))


def get_candidate(conn: psycopg.Connection, candidate_id: str) -> CandidateRow | None:
    r = fetch_one(conn, "select * from forge_candidate where id = %s", (candidate_id,))
    return None if r is None else _cand(r)


def set_build(conn: psycopg.Connection, candidate_id: str, *, build: Any,
              files: dict[str, str], status: Status, cost_usd: float | None,
              now: datetime) -> None:
    conn.execute(
        "update forge_candidate set build = %s, files = %s, status = %s, cost_usd = %s, "
        "updated_at = %s where id = %s",
        (Jsonb(build), Jsonb(files), status, cost_usd, now, candidate_id))


def set_status(conn: psycopg.Connection, candidate_id: str, status: Status,
               now: datetime) -> None:
    conn.execute("update forge_candidate set status = %s, updated_at = %s where id = %s",
                 (status, now, candidate_id))


def archive(conn: psycopg.Connection, user_id: str | None, name: str | None = None,
            limit: int = 50) -> list[CandidateRow]:
    """Candidates of one scope (None = global), newest first, failed ones included."""
    sql = "select * from forge_candidate where user_id is not distinct from %s"
    args: list[Any] = [user_id]
    if name is not None:
        sql += " and name = %s"
        args.append(name)
    sql += " order by created_at desc, id desc limit %s"
    args.append(limit)
    return [_cand(r) for r in fetch_all(conn, sql, tuple(args))]


def candidates_by_origin(conn: psycopg.Connection, user_id: str | None, origin: str,
                         statuses: tuple[Status, ...] = ("built", "promoted")
                         ) -> list[CandidateRow]:
    rows = fetch_all(
        conn, "select * from forge_candidate where user_id is not distinct from %s "
              "and origin = %s and status = any(%s) order by created_at, id",
        (user_id, origin, list(statuses)))
    return [_cand(r) for r in rows]


def put_vec(conn: psycopg.Connection, candidate_id: str, *, embedder: str, model_version: str,
            vec: list[float]) -> None:
    """Upsert (same candidate + model_version replaces: re-embedding is idempotent)."""
    conn.execute(
        "insert into forge_tool_vec (candidate_id, embedder, model_version, dim, vec) "
        "values (%s, %s, %s, %s, %s) on conflict (candidate_id, model_version) do update set "
        "embedder = excluded.embedder, dim = excluded.dim, vec = excluded.vec",
        (candidate_id, embedder, model_version, len(vec), vec))


def vecs(conn: psycopg.Connection, candidate_ids: list[str], *,
         model_version: str) -> dict[str, list[float]]:
    if not candidate_ids:
        return {}
    rows = fetch_all(
        conn, "select candidate_id, vec from forge_tool_vec "
              "where candidate_id = any(%s) and model_version = %s",
        (list(candidate_ids), model_version))
    return {r["candidate_id"]: [float(x) for x in r["vec"]] for r in rows}


def settings_with_prefix(conn: psycopg.Connection, scope: str, prefix: str) -> dict[str, Any]:
    """Settings of one scope whose key starts with prefix (literal match, no LIKE wildcards)."""
    rows = fetch_all(conn, "select key, value from forge_setting where scope = %s "
                           "and starts_with(key, %s) order by key", (scope, prefix))
    return {r["key"]: r["value"] for r in rows}


def lock_scope(conn: psycopg.Connection, scope: str) -> None:
    xact_lock(conn, f"forge:{scope}")


def registry_head(conn: psycopg.Connection, scope: str) -> tuple[int, dict[str, str]]:
    r = fetch_one(conn, "select version, entries from forge_registry where scope = %s "
                        "order by version desc limit 1", (scope,))
    return (0, {}) if r is None else (int(r["version"]), dict(r["entries"]))


def registry_at(conn: psycopg.Connection, scope: str, version: int) -> dict[str, str] | None:
    r = fetch_one(conn, "select entries from forge_registry where scope = %s and version = %s",
                  (scope, version))
    return None if r is None else dict(r["entries"])


def append_registry(conn: psycopg.Connection, scope: str, entries: dict[str, str],
                    change: dict[str, Any], now: datetime) -> int:
    lock_scope(conn, scope)
    version = registry_head(conn, scope)[0] + 1
    conn.execute(
        "insert into forge_registry (scope, version, entries, change, created_at) "
        "values (%s, %s, %s, %s, %s)", (scope, version, Jsonb(entries), Jsonb(change), now))
    return version


def registry_history(conn: psycopg.Connection, scope: str, limit: int = 50) -> list[RegistryRow]:
    rows = fetch_all(conn, "select * from forge_registry where scope = %s "
                           "order by version desc limit %s", (scope, limit))
    return [RegistryRow(**r) for r in rows]


def get_setting(conn: psycopg.Connection, scope: str, key: str) -> Any | None:
    r = fetch_one(conn, "select value from forge_setting where scope = %s and key = %s",
                  (scope, key))
    return None if r is None else r["value"]


def set_setting(conn: psycopg.Connection, scope: str, key: str, value: Any,
                now: datetime) -> None:
    conn.execute(
        "insert into forge_setting (scope, key, value, updated_at) values (%s, %s, %s, %s) "
        "on conflict (scope, key) do update set value = excluded.value, "
        "updated_at = excluded.updated_at", (scope, key, Jsonb(value), now))
