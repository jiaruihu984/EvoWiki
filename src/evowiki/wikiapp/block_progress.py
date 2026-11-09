"""A reader's progress in an EvoUI block and the result events already counted (migration 0030),
in memory and on Postgres.

Progress (`{revision, data}`, data = evoui.js `progress()`) is replaced only by a greater revision
(nothing stored reads as revision 0): a late or resent save never rolls a reader back. Event keys (`block_results.event_key`) are written
through `ProgressStore.events_writer(...)`, a callback the wiki commit runs in its own transaction
(`wikiapp.repo.Commit.extra`), so an event counts exactly when its evidence and signal landed. A
key that is already there when the writer runs (two posts of the same event racing) raises
`WikiConflict` (409; the retry reports the event `duplicate`)."""

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evowiki.store.db import fetch_all, fetch_one
from evowiki.wikiapp.block_visibility import guard_asset_write
from evowiki.wikiapp.repo import WikiConflict

Writer = Any        # Callable[[conn | None], None] (wikiapp.repo.Commit.extra)


@dataclass(frozen=True)
class Progress:
    revision: int
    data: dict[str, Any]


class ProgressStore(Protocol):
    def get(self, user_id: str, asset_id: str) -> Progress | None: ...

    def put(self, user_id: str, asset_id: str, progress: Progress, *,
            at: datetime) -> tuple[int, bool]:
        """Stores `progress` when its revision is greater than the stored one. Returns (the
        revision stored afterwards, whether `progress` was stored)."""
        ...

    def seen(self, user_id: str, asset_id: str, keys: Collection[str]) -> set[str]:
        """The `keys` already recorded for this user and block."""
        ...

    def events_writer(self, user_id: str, asset_id: str, keys: Sequence[str], *,
                      at: datetime) -> Writer: ...


class MemoryProgressStore:
    def __init__(self) -> None:
        self.progress: dict[tuple[str, str], tuple[Progress, datetime]] = {}
        self.events: dict[tuple[str, str], dict[str, datetime]] = {}

    def get(self, user_id: str, asset_id: str) -> Progress | None:
        hit = self.progress.get((user_id, asset_id))
        return None if hit is None else Progress(hit[0].revision, json.loads(json.dumps(hit[0].data)))

    def put(self, user_id: str, asset_id: str, progress: Progress, *,
            at: datetime) -> tuple[int, bool]:
        old = self.progress.get((user_id, asset_id))
        if (old[0].revision if old is not None else 0) >= progress.revision:
            return (old[0].revision if old is not None else 0), False
        self.progress[(user_id, asset_id)] = (
            Progress(progress.revision, json.loads(json.dumps(progress.data))), at)
        return progress.revision, True

    def seen(self, user_id: str, asset_id: str, keys: Collection[str]) -> set[str]:
        have = self.events.get((user_id, asset_id), {})
        return {k for k in keys if k in have}

    def events_writer(self, user_id: str, asset_id: str, keys: Sequence[str], *,
                      at: datetime) -> Writer:
        def write(_conn: Any) -> None:
            have = self.events.setdefault((user_id, asset_id), {})
            if any(k in have for k in keys) or len(set(keys)) != len(keys):
                raise WikiConflict("这些结果刚刚已经记过了")
            for k in keys:
                have[k] = at
        return write


class PgProgressStore:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def get(self, user_id: str, asset_id: str) -> Progress | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select revision, data from block_progress "
                                "where user_id = %s and asset_id = %s", (user_id, asset_id))
        return None if r is None else Progress(r["revision"], r["data"])

    def put(self, user_id: str, asset_id: str, progress: Progress, *,
            at: datetime) -> tuple[int, bool]:
        if progress.revision <= 0:                     # nothing stored reads as revision 0
            cur = self.get(user_id, asset_id)
            return (0 if cur is None else cur.revision), False
        with self._pool.connection() as conn, conn.transaction():
            guard_asset_write(conn, user_id, asset_id)
            r = fetch_one(
                conn,
                "insert into block_progress (user_id, asset_id, revision, data, updated_at) "
                "values (%s, %s, %s, %s, %s) on conflict (user_id, asset_id) do update set "
                "revision = excluded.revision, data = excluded.data, "
                "updated_at = excluded.updated_at where block_progress.revision < excluded.revision "
                "returning revision",
                (user_id, asset_id, progress.revision, Jsonb(progress.data), at))
            if r is not None:
                return r["revision"], True
            cur = fetch_one(conn, "select revision from block_progress "
                                  "where user_id = %s and asset_id = %s", (user_id, asset_id))
        assert cur is not None
        return cur["revision"], False

    def seen(self, user_id: str, asset_id: str, keys: Collection[str]) -> set[str]:
        if not keys:
            return set()
        with self._pool.connection() as conn:
            rows = fetch_all(conn, "select event_key from block_event where user_id = %s and "
                                   "asset_id = %s and event_key = any(%s)",
                             (user_id, asset_id, list(keys)))
        return {r["event_key"] for r in rows}

    def events_writer(self, user_id: str, asset_id: str, keys: Sequence[str], *,
                      at: datetime) -> Writer:
        def write(conn: Any) -> None:
            if not keys:
                return
            n = 0
            with conn.cursor() as cur:
                for k in keys:
                    cur.execute("insert into block_event (user_id, asset_id, event_key, at) "
                                "values (%s, %s, %s, %s) on conflict do nothing",
                                (user_id, asset_id, k, at))
                    n += cur.rowcount
            if n != len(keys):
                raise WikiConflict("这些结果刚刚已经记过了")
        return write
