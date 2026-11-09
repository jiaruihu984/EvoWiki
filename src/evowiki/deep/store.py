"""Refine jobs and interactive assets (migration 0022), in memory and on Postgres.

Jobs are append-only snapshots: `save` appends the whole `RefineJob` at every stage change, the newest
snapshot wins. Assets are written only through `AssetStore.writer(...)`, a callback the wiki commit
runs inside its own transaction (`wikiapp.repo.Commit.extra`), so a page never points at an asset
that was not stored and a page that was not committed leaves no asset behind."""

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evowiki.deep.contracts import (
    AssetMeta,
    LibraryComponent,
    RefineDraft,
    RefineJob,
    RenderCheck,
)
from evowiki.store.db import fetch_all, fetch_one

Writer = Callable[[Any], None]      # gets the commit's connection (None for the memory repo)


def sha256(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredAsset:
    """`html`: the content — HTML for `meta.kind` html, the block JSON for evoui."""

    user_id: str
    meta: AssetMeta
    html: str
    job_id: str
    created_at: datetime


class AssetStore(Protocol):
    def writer(self, assets: Sequence[StoredAsset]) -> Writer: ...

    def put(self, assets: Sequence[StoredAsset]) -> None:
        """Stores `assets` on their own (not riding on a page commit): a draft's interactive that the page
        shows before it is confirmed (bot.practice); an asset no page refers to is harmless."""
        ...

    def get(self, user_id: str, asset_id: str) -> StoredAsset | None: ...

    def for_page(self, user_id: str, page_id: str) -> list[AssetMeta]: ...

    def by_job(self, user_id: str, job_id: str) -> StoredAsset | None:
        """The first asset stored under `job_id` (e.g. a page's exam quiz, `exam:<page>:<h>`)."""
        ...

    def generated(self, user_id: str, kind: str, content_sha256: str) -> bool:
        """Whether this user's EvoWiki stored an asset of this kind with exactly this content."""
        ...


class JobStore(Protocol):
    def save(self, user_id: str, job: RefineJob, draft: RefineDraft | None = None) -> None:
        """Appends a job snapshot; `draft` only on a failed job's final snapshot (last_draft)."""
        ...

    def last_draft(self, user_id: str, job_id: str) -> RefineDraft | None:
        """The draft stored with the job's newest snapshot, if any."""
        ...

    def latest(self, user_id: str, page_id: str) -> RefineJob | None: ...

    def get(self, user_id: str, job_id: str) -> RefineJob | None: ...


class MemoryAssetStore:
    def __init__(self) -> None:
        self.rows: dict[str, StoredAsset] = {}

    def writer(self, assets: Sequence[StoredAsset]) -> Writer:
        def write(_conn: Any) -> None:
            for a in assets:
                if a.meta.asset_id in self.rows:
                    raise ValueError(f"asset {a.meta.asset_id} already stored")
            for a in assets:
                self.rows[a.meta.asset_id] = a
        return write

    def put(self, assets: Sequence[StoredAsset]) -> None:
        self.writer(assets)(None)

    def get(self, user_id: str, asset_id: str) -> StoredAsset | None:
        a = self.rows.get(asset_id)
        return a if a is not None and a.user_id == user_id else None

    def for_page(self, user_id: str, page_id: str) -> list[AssetMeta]:
        return [a.meta for a in self.rows.values()
                if a.user_id == user_id and a.meta.page_id == page_id]

    def by_job(self, user_id: str, job_id: str) -> StoredAsset | None:
        return next((a for a in self.rows.values() if a.user_id == user_id and a.job_id == job_id),
                    None)


    def generated(self, user_id: str, kind: str, content_sha256: str) -> bool:
        return any(a.user_id == user_id and a.meta.kind == kind and a.meta.sha256 == content_sha256
                   for a in self.rows.values())


class MemoryJobStore:
    def __init__(self) -> None:
        self.rows: list[tuple[str, RefineJob]] = []
        self.drafts: dict[tuple[str, str], RefineDraft | None] = {}   # newest snapshot's draft

    def save(self, user_id: str, job: RefineJob, draft: RefineDraft | None = None) -> None:
        self.rows.append((user_id, job))
        self.drafts[(user_id, job.job_id)] = draft

    def last_draft(self, user_id: str, job_id: str) -> RefineDraft | None:
        return self.drafts.get((user_id, job_id))

    def latest(self, user_id: str, page_id: str) -> RefineJob | None:
        return next((j for u, j in reversed(self.rows) if u == user_id and j.page_id == page_id),
                    None)

    def get(self, user_id: str, job_id: str) -> RefineJob | None:
        return next((j for u, j in reversed(self.rows) if u == user_id and j.job_id == job_id),
                    None)


def _asset(r: dict) -> StoredAsset:
    meta = AssetMeta(asset_id=r["asset_id"], page_id=r["page_id"], section_id=r["section_id"],
                     interactive_id=r["interactive_id"], title=r["title"], height=r["height"],
                     sha256=r["sha256"], check=RenderCheck.model_validate(r["render_check"]),
                     kind=r.get("kind") or "html")
    return StoredAsset(user_id=r["user_id"], meta=meta, html=r["html"], job_id=r["job_id"],
                       created_at=r["created_at"])


class PgAssetStore:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def writer(self, assets: Sequence[StoredAsset]) -> Writer:
        def write(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into wiki_asset (asset_id, user_id, page_id, section_id, "
                    "interactive_id, title, height, sha256, html, render_check, job_id, "
                    "created_at, kind) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [(a.meta.asset_id, a.user_id, a.meta.page_id, a.meta.section_id,
                      a.meta.interactive_id, a.meta.title, a.meta.height, a.meta.sha256, a.html,
                      Jsonb(a.meta.check.model_dump(mode="json")), a.job_id, a.created_at,
                      a.meta.kind)
                     for a in assets])
        return write

    def put(self, assets: Sequence[StoredAsset]) -> None:
        with self._pool.connection() as conn:
            self.writer(assets)(conn)

    def get(self, user_id: str, asset_id: str) -> StoredAsset | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select * from wiki_asset where asset_id = %s and user_id = %s",
                          (asset_id, user_id))
        return None if r is None else _asset(r)

    def for_page(self, user_id: str, page_id: str) -> list[AssetMeta]:
        with self._pool.connection() as conn:
            rows = fetch_all(conn, "select * from wiki_asset where user_id = %s and page_id = %s "
                                   "order by created_at, asset_id", (user_id, page_id))
        return [_asset(r).meta for r in rows]

    def by_job(self, user_id: str, job_id: str) -> StoredAsset | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select * from wiki_asset where user_id = %s and job_id = %s "
                                "order by created_at, asset_id limit 1", (user_id, job_id))
        return None if r is None else _asset(r)

    def generated(self, user_id: str, kind: str, content_sha256: str) -> bool:
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT 1 FROM wiki_asset WHERE user_id = %s AND sha256 = %s AND kind = %s LIMIT 1",
                (user_id, content_sha256, kind)).fetchone() is not None


class PgJobStore:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def save(self, user_id: str, job: RefineJob, draft: RefineDraft | None = None) -> None:
        with self._pool.connection() as conn:
            conn.execute("insert into refine_job (job_id, user_id, page_id, job, draft) "
                         "values (%s, %s, %s, %s, %s)",
                         (job.job_id, user_id, job.page_id, Jsonb(job.model_dump(mode="json")),
                          None if draft is None else Jsonb(draft.model_dump(mode="json"))))

    def last_draft(self, user_id: str, job_id: str) -> RefineDraft | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select draft from refine_job where user_id = %s and job_id = %s "
                                "order by id desc limit 1", (user_id, job_id))
        return None if r is None or r["draft"] is None else RefineDraft.model_validate(r["draft"])

    def latest(self, user_id: str, page_id: str) -> RefineJob | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select job from refine_job where user_id = %s and page_id = %s "
                                "order by id desc limit 1", (user_id, page_id))
        return None if r is None else RefineJob.model_validate(r["job"])

    def get(self, user_id: str, job_id: str) -> RefineJob | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select job from refine_job where user_id = %s and job_id = %s "
                                "order by id desc limit 1", (user_id, job_id))
        return None if r is None else RefineJob.model_validate(r["job"])


class PgLibrary:
    """The component library on Postgres (migration 0027): global, not per user."""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def list(self) -> list[LibraryComponent]:
        with self._pool.connection() as conn:
            rows = fetch_all(conn, "select body from component_library order by component_id")
        return [LibraryComponent.model_validate(r["body"]) for r in rows]

    def get(self, component_id: str) -> LibraryComponent | None:
        with self._pool.connection() as conn:
            r = fetch_one(conn, "select body from component_library where component_id = %s",
                          (component_id,))
        return None if r is None else LibraryComponent.model_validate(r["body"])

    def put(self, component: LibraryComponent) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "insert into component_library (component_id, body) values (%s, %s) "
                "on conflict (component_id) do update set body = excluded.body, "
                "registered_at = clock_timestamp()",
                (component.component_id, Jsonb(component.model_dump(mode="json"))))
