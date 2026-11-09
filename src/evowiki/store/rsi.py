"""Postgres `RsiStore` (migration 0024): Policy Registry rows, patches, Patch Memory, the cycle log and
per-user RSI settings. Same semantics as `evowiki.teach.rsi_store.MemoryRsiStore`: versions are
append-only and gap-free per (user, track) — only last + 1 is accepted (the first must be 1), anything
else raises ValueError; patches / memory rows / settings are upserted; a user without settings gets
`RsiSettings()`. Timestamps come back UTC (including the ones inside the JSON bodies, which are
re-anchored to their columns).

Extra over the protocol: `PgRsiStore.transaction()` pins one connection for the calls inside it, so a
caller can make several writes atomic — e.g. `registry.commit`, which writes the implicit v1 and the
new version back to back. The pin is per pool (`store.pinned`): every store on the same pool joins it."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evowiki.store import pinned
from evowiki.store.bot import mirror_action, sync_rsi_undone
from evowiki.store.db import fetch_all, fetch_one
from evowiki.teach.approval import patch_key
from evowiki.teach.contracts import (
    InferParams,
    Patch,
    PatchMemoryEntry,
    PolicyVersion,
    RsiSettings,
    Track,
)
from evowiki.teach.rsi_store import CycleRecord, MemoryRecord, PatchRecord

# other statuses: no mirror row. A change the user is asked about (`teach.approval`) has its
# `proposed` row under the same key; acceptance moves it to executed. Rollback only updates
# an existing mirror, never creates a historical action.
MIRROR_STATUS = {"personal_accepted": "executed"}


def _json(model: Any) -> Jsonb:
    return Jsonb(model.model_dump(mode="json"))


def _version(row: dict) -> PolicyVersion:
    return PolicyVersion.model_validate(row["body"]).model_copy(update={"at": row["at"]})


def infer_at(conn: psycopg.Connection, user_id: str, version: int) -> InferParams:
    """θ_infer,u of track-A version `version` (1 with nothing stored = the implicit v1)."""
    row = fetch_one(conn, "select at, body from rsi_policy_version where user_id = %s "
                          "and track = 'A' and version = %s", (user_id, version))
    if row is None:
        if version == 1:
            return InferParams()
        raise LookupError(f"no track-A version {version} for {user_id}")
    infer = _version(row).infer
    if infer is None:
        raise ValueError("track A version without infer")
    return infer


def current_infer(conn: psycopg.Connection, user_id: str) -> InferParams:
    """θ_infer,u in force: the latest track-A version's infer, else `InferParams()` (the implicit v1).
    Takes a connection so it can run inside the caller's (e.g. repeatable-read) transaction."""
    row = fetch_one(conn, "select at, body from rsi_policy_version where user_id = %s and track = 'A' "
                          "order by version desc limit 1", (user_id,))
    if row is None:
        return InferParams()
    infer = _version(row).infer
    if infer is None:                            # PolicyVersion's validator makes this unreachable
        raise ValueError("track A version without infer")
    return infer


def current_infer_version(conn: psycopg.Connection, user_id: str) -> int:
    """The track-A version in force (what a SectionLog records as `infer_version`); 1 = the implicit
    v1 when nothing is stored."""
    row = fetch_one(conn, "select max(version) as v from rsi_policy_version "
                          "where user_id = %s and track = 'A'", (user_id,))
    return int(row["v"]) if row and row["v"] is not None else 1


def _short(why: str, limit: int = 60) -> str:
    """The mirror row's one-line summary: the whole `why` when it fits, else cut after the last 「，」/「。」 before the limit."""
    if len(why) <= limit:
        return why
    cut = max(why.rfind("，", 0, limit), why.rfind("。", 0, limit))
    return why[:cut] if cut > 0 else why[:limit]


class PgRsiStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Every store call on this thread inside the block uses one connection and one transaction;
        an exception rolls all of them back. Nested blocks become savepoints. The connection is
        pinned per pool (`store.pinned`), so every store on the same pool — another `PgRsiStore`,
        a `PgLayer2Store` — joins the same transaction."""
        with pinned.transaction(self._pool):
            yield

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        with pinned.connection(self._pool) as conn:   # commits on clean exit, rolls back on error
            yield conn

    # ---- Policy Registry ----

    def versions(self, user_id: str, track: Track) -> list[PolicyVersion]:
        with self._conn() as conn:
            rows = fetch_all(conn, "select at, body from rsi_policy_version where user_id = %s and track = %s "
                                   "order by version", (user_id, track))
        return [_version(r) for r in rows]

    def add_version(self, user_id: str, v: PolicyVersion) -> None:
        with self._conn() as conn:
            try:
                with conn.transaction():
                    row = fetch_one(conn, "select coalesce(max(version), 0) as last from rsi_policy_version "
                                          "where user_id = %s and track = %s", (user_id, v.track))
                    assert row is not None
                    expected = row["last"] + 1
                    if v.version != expected:
                        raise ValueError(f"{user_id}/{v.track}: version {v.version} is not the next ({expected})")
                    conn.execute("insert into rsi_policy_version (user_id, track, version, at, patch_id, body) "
                                 "values (%s, %s, %s, %s, %s, %s)",
                                 (user_id, v.track, v.version, v.at, v.patch_id, _json(v)))
            except psycopg.errors.UniqueViolation as e:   # a concurrent writer took this number
                raise ValueError(f"{user_id}/{v.track}: version {v.version} is not the next (taken)") from e

    # ---- patches ----

    def patches(self, user_id: str) -> list[PatchRecord]:
        with self._conn() as conn:
            rows = fetch_all(conn, "select * from rsi_patch where user_id = %s order by created_at, patch_id",
                             (user_id,))
        return [PatchRecord(user_id=r["user_id"],
                            patch=Patch.model_validate(r["body"]).model_copy(update={"created_at": r["created_at"]}),
                            cutoff=r["cutoff"], gate_runs=r["gate_runs"],
                            last_gate_outcomes=r["last_gate_outcomes"], updated_at=r["updated_at"])
                for r in rows]

    def put_patch(self, rec: PatchRecord) -> None:
        p = rec.patch
        with self._conn() as conn:
            row = fetch_one(conn, """
                insert into rsi_patch (patch_id, user_id, track, status, created_at, body, cutoff, gate_runs,
                                       last_gate_outcomes, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (patch_id) do update set
                  track = excluded.track, status = excluded.status, created_at = excluded.created_at,
                  body = excluded.body, cutoff = excluded.cutoff, gate_runs = excluded.gate_runs,
                  last_gate_outcomes = excluded.last_gate_outcomes, updated_at = excluded.updated_at
                  where rsi_patch.user_id = excluded.user_id
                returning patch_id""",
                (p.patch_id, rec.user_id, p.track, p.status, p.created_at, _json(p), rec.cutoff,
                 rec.gate_runs, rec.last_gate_outcomes, rec.updated_at))
            if row is not None:
                status = MIRROR_STATUS.get(p.status)
                if p.status == "rolled_back":
                    sync_rsi_undone(conn, rec.user_id)
                elif status is not None:                 # the action ledger's copy (spec §4)
                    mirror_action(
                        conn, user_id=rec.user_id, kind="rsi_patch",
                        scope="rsi" if p.track == "B" else "teaching_params", source="rsi",
                        key=patch_key(rec.user_id, p.patch_id), status=status,
                        summary=_short(p.why), why=p.why,
                        content={"patch_id": p.patch_id, "track": p.track},
                        created_at=p.created_at, decided_at=rec.updated_at)
        if row is None:
            raise ValueError(f"patch {p.patch_id} belongs to another user")

    # ---- Patch Memory ----

    def memory(self, user_id: str) -> list[MemoryRecord]:
        with self._conn() as conn:
            rows = fetch_all(conn, "select * from rsi_memory where user_id = %s order by at, patch_id", (user_id,))
        return [MemoryRecord(user_id=r["user_id"],
                             entry=PatchMemoryEntry.model_validate(r["entry"]).model_copy(update={"at": r["at"]}),
                             direction=r["direction"], from_value=r["from_value"], to_value=r["to_value"],
                             counts_at=r["counts_at"], repeat_of_rejected=r["repeat_of_rejected"],
                             outcomes_at_close=r["outcomes_at_close"], complexity=r["complexity"])
                for r in rows]

    def put_memory(self, rec: MemoryRecord) -> None:
        e = rec.entry
        with self._conn() as conn:
            row = fetch_one(conn, """
                insert into rsi_memory (patch_id, user_id, track, signature, path, direction, entry, from_value,
                                        to_value, counts_at, repeat_of_rejected, outcomes_at_close, complexity, at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (patch_id) do update set
                  track = excluded.track, signature = excluded.signature, path = excluded.path,
                  direction = excluded.direction, entry = excluded.entry, from_value = excluded.from_value,
                  to_value = excluded.to_value, counts_at = excluded.counts_at,
                  repeat_of_rejected = excluded.repeat_of_rejected,
                  outcomes_at_close = excluded.outcomes_at_close, complexity = excluded.complexity,
                  at = excluded.at
                  where rsi_memory.user_id = excluded.user_id
                returning patch_id""",
                (e.patch_id, rec.user_id, e.track, e.signature, e.path, rec.direction, _json(e), rec.from_value,
                 rec.to_value, rec.counts_at, rec.repeat_of_rejected, rec.outcomes_at_close, rec.complexity,
                 e.at))
        if row is None:
            raise ValueError(f"memory row {e.patch_id} belongs to another user")

    # ---- cycle log ----

    def cycles(self, user_id: str) -> list[CycleRecord]:
        with self._conn() as conn:
            rows = fetch_all(conn, "select * from rsi_cycle where user_id = %s order by id", (user_id,))
        return [CycleRecord(user_id=r["user_id"], at=r["at"], trigger=r["trigger"], result=r["result"],
                            cost_usd=r["cost_usd"]) for r in rows]

    def last_cycle_at(self, user_id: str) -> datetime | None:
        """The latest cycle's time (None: no cycle yet), without loading the cycle log."""
        with self._conn() as conn:
            row = fetch_one(conn, "select max(at) as at from rsi_cycle where user_id = %s", (user_id,))
        return None if row is None else row["at"]

    def add_cycle(self, rec: CycleRecord) -> None:
        with self._conn() as conn:
            conn.execute("insert into rsi_cycle (user_id, at, trigger, result, cost_usd) values (%s, %s, %s, %s, %s)",
                         (rec.user_id, rec.at, rec.trigger, Jsonb(rec.result), rec.cost_usd))

    # ---- settings ----

    def settings(self, user_id: str) -> RsiSettings:
        with self._conn() as conn:
            row = fetch_one(conn, "select settings from rsi_settings where user_id = %s", (user_id,))
        if row is None:
            return RsiSettings()
        return RsiSettings.model_validate(_current_keys(row["settings"]))

    def put_settings(self, user_id: str, s: RsiSettings) -> None:
        with self._conn() as conn:
            conn.execute("insert into rsi_settings (user_id, settings, updated_at) values (%s, %s, now()) "
                         "on conflict (user_id) do update set settings = excluded.settings, "
                         "updated_at = excluded.updated_at", (user_id, _json(s)))


# Keys of track B (RSI-B / B2) that retired on 2026-10-29. Migration 0099 removes them from stored
# rows; until a database ran it, they are dropped on read so the settings still load.
_RETIRED_KEYS = frozenset({"gate_limit_usd", "strict_gate", "layer2"})


def _current_keys(stored: dict) -> dict:
    return {k: v for k, v in stored.items() if k not in _RETIRED_KEYS}
