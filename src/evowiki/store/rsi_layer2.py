"""Postgres `Layer2Store` (migrations 0041, 0042): the append-only outcome series of each RSI change
and the method registry (versions + changes). Same semantics as
`evowiki.teach.layer2.store.MemoryLayer2Store`: points and method versions are only inserted (the
tables' triggers refuse update / delete / truncate), points read back oldest first (at, then id),
versions by version, changes by (created_at, change_id); timestamps in UTC (the one inside the JSON
body is re-anchored to its column). A change's kind and created_at are fixed by its first write.
`transaction()` makes several writes atomic, together with the other stores on the same pool."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evowiki.store import pinned
from evowiki.store.bot import mirror_action, sync_rsi_undone
from evowiki.store.db import fetch_all, fetch_one
from evowiki.teach.approval import method_key
from evowiki.teach.contracts import MethodChange, MethodKind, MethodVersion, OutcomeOut

# gate_failed / awaiting_user / rejected: no mirror row (a change the user is asked about,
# `teach.approval`, has its `proposed` row under the same key; the mirror then moves it).
# Rollback only updates an existing mirror, never creates a historical action.
MIRROR_STATUS = {"applied": "executed"}


class PgLayer2Store:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One connection and one transaction for every call on this thread inside the block, shared
        with every other store on the same pool (`store.pinned`; e.g. the cycle's `PgRsiStore`
        transaction); nested blocks become savepoints."""
        with pinned.transaction(self._pool):
            yield

    def outcome_points(self, user_id: str, change_id: str | None = None) -> list[OutcomeOut]:
        sql = "select at, body from rsi_outcome_point where user_id = %s"
        args: tuple = (user_id,)
        if change_id is not None:
            sql += " and change_id = %s"
            args = (user_id, change_id)
        with pinned.connection(self._pool) as conn:
            rows = fetch_all(conn, sql + " order by at, id", args)
        return [OutcomeOut.model_validate(r["body"]).model_copy(update={"at": r["at"]}) for r in rows]

    def add_outcome_point(self, user_id: str, o: OutcomeOut) -> None:
        with pinned.connection(self._pool) as conn:
            conn.execute("insert into rsi_outcome_point (user_id, change_id, at, verdict, body) "
                         "values (%s, %s, %s, %s, %s)",
                         (user_id, o.change_id, o.at, o.verdict, Jsonb(o.model_dump(mode="json"))))

    # ---- method registry (0042) ----

    def method_versions(self, user_id: str, kind: MethodKind) -> list[MethodVersion]:
        with pinned.connection(self._pool) as conn:
            rows = fetch_all(conn, "select at, body from rsi_method_version "
                                   "where user_id = %s and kind = %s order by version", (user_id, kind))
        return [MethodVersion.model_validate(_version_body(r["body"])).model_copy(update={"at": r["at"]})
                for r in rows]

    def add_method_version(self, user_id: str, v: MethodVersion) -> None:
        try:
            with pinned.connection(self._pool) as conn, conn.transaction():
                row = fetch_one(conn, "select coalesce(max(version), 0) as last from rsi_method_version "
                                      "where user_id = %s and kind = %s", (user_id, v.kind))
                assert row is not None
                v.require_next(user_id, row["last"])
                conn.execute("insert into rsi_method_version (user_id, kind, version, at, change_id, body) "
                             "values (%s, %s, %s, %s, %s, %s)",
                             (user_id, v.kind, v.version, v.at, v.change_id,
                              Jsonb(v.model_dump(mode="json"))))
        except psycopg.errors.UniqueViolation as e:      # a concurrent writer took this number
            raise ValueError(f"{user_id}/{v.kind}: version {v.version} is not the next (taken)") from e

    def method_changes(self, user_id: str, kind: MethodKind | None = None) -> list[MethodChange]:
        sql = "select created_at, updated_at, body from rsi_method_change where user_id = %s"
        args: tuple = (user_id,)
        if kind is not None:
            sql += " and kind = %s"
            args = (user_id, kind)
        with pinned.connection(self._pool) as conn:
            rows = fetch_all(conn, sql + " order by created_at, change_id", args)
        return [MethodChange.model_validate(r["body"]).model_copy(
                    update={"created_at": r["created_at"], "updated_at": r["updated_at"]})
                for r in rows]

    def put_method_change(self, user_id: str, c: MethodChange) -> None:
        with pinned.connection(self._pool) as conn:
            row = fetch_one(conn, """
                insert into rsi_method_change (change_id, user_id, kind, status, created_at, updated_at, body)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (change_id) do update set
                  status = excluded.status, updated_at = excluded.updated_at,
                  body = excluded.body
                  where rsi_method_change.user_id = excluded.user_id
                returning change_id""",
                (c.change_id, user_id, c.kind, c.status, c.created_at, c.updated_at,
                 Jsonb(c.model_dump(mode="json"))))
            status = MIRROR_STATUS.get(c.status)
            if row is not None and c.status == "rolled_back":
                sync_rsi_undone(conn, user_id)
            elif row is not None and status is not None:     # the action ledger's copy (spec §4)
                mirror_action(
                    conn, user_id=user_id, kind="rsi_method", scope="rsi", source="rsi",
                    key=method_key(user_id, c.change_id), status=status,
                    summary=c.summary, why=c.why,
                    content={"change_id": c.change_id, "kind": c.kind},
                    created_at=c.created_at, decided_at=c.updated_at)
        if row is None:
            raise ValueError(f"method change {c.change_id} belongs to another user")


def _version_body(body: dict) -> dict:
    """A stored method version without the `b_method` key A2 rows carried (always null) while
    B2 existed (retired 2026-10-29)."""
    return {k: v for k, v in body.items() if k != "b_method"}
