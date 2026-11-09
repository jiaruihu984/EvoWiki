"""Page write runs (spec 2026-10-14 §4, B1b; write-all 2026-10-15 §4): one `page_run` row per DBOS
page workflow.

The row is product data the engine does not know: which page, which kind and origin, whether a
follower may attach, when the workflow began (before that the run is queued behind the user's other
page run), the live progress the status API shows, and the cost / failure it ended with. A page
written ahead (origin `idle`) is committed by its own run like any other; there is nothing to hold
or claim."""

import threading
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict

# (prefetch: old rows)
RunKind = Literal["organize", "prefetch", "feedback", "refine", "rsi_rewrite", "reorder_rewrite",
                  "source_revise", "harness_rewrite", "patch_rewrite"]
RunStatus = Literal["writing", "done", "failed"]
Origin = Literal["open", "prefetch", "background", "idle"]                       # prefetch: old rows
KEEP_FINISHED = timedelta(minutes=30)
OPEN_TRIES = 5                          # (user, page, n) races before `open_run` gives up
_ATTACHABLE = ("organize",)


class PageRunRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str                         # = DBOS workflow id "page:<user>:<page>:<n>"
    user_id: str
    page_id: str
    n: int
    kind: RunKind
    origin: Origin
    job: dict[str, Any]                 # the PageJob JSON (restart / ensure)
    status: RunStatus
    started_at: datetime                # the row was opened (queued)
    finished_at: datetime | None = None
    consumed_at: datetime | None = None
    discarded_at: datetime | None = None
    began_at: datetime | None = None    # the workflow began (None: still queued)
    progress: dict[str, Any] | None = None   # {"section": int, "of": int | None, "pass": int | None}
    cost_usd: float | None = None
    llm_calls: int | None = None
    failure: str | None = None          # the error code the run ended with


class PageRunStore(Protocol):
    def open_run(self, *, user_id: str, page_id: str, kind: RunKind, origin: str,
                 job: dict[str, Any], now: datetime, attach: bool) -> tuple[PageRunRow, bool]: ...
    def get(self, run_id: str) -> PageRunRow | None: ...
    def writing_rows(self) -> list[PageRunRow]:
        """Every row still `writing` (the startup sweep looks for the ones that can never finish)."""
        ...
    def live_of(self, user_id: str) -> list[PageRunRow]:
        """The user's rows still `writing` (queued or running) and not discarded, oldest first."""
        ...
    def latest_of(self, user_id: str, page_ids: list[str]) -> dict[str, PageRunRow]:
        """The newest run row of each of these pages (a page without one is absent)."""
        ...
    def finished_since(self, user_id: str, since: datetime, *, origin: str) -> list[PageRunRow]:
        """The user's finished rows of this origin that ended at or after `since`, newest first."""
        ...
    def begin(self, run_id: str, at: datetime) -> None: ...
    def set_progress(self, run_id: str, progress: dict[str, Any]) -> None: ...
    def finish(self, run_id: str, status: RunStatus, at: datetime, *, cost_usd: float | None = None,
               llm_calls: int | None = None, failure: str | None = None) -> None: ...
    def consume(self, run_id: str, at: datetime) -> None: ...
    def discard(self, run_id: str, at: datetime, *, queued_only: bool = False) -> bool:
        """Marks the run discarded unless it was discarded already; True = it took effect.
        `queued_only`: only a run that has not begun (False if it began meanwhile)."""
        ...
    def has_begun_before(self, user_id: str, page_id: str, n: int) -> bool:
        """Whether an earlier run (number below `n`) of this page ever began. A run dropped while
        it was still queued (re-queued after a path edit) did not."""
        ...


# ---- shared rules -----------------------------------------------------------------------------

def _attachable(r: PageRunRow, now: datetime) -> bool:
    """A follower may attach to this run (a discarded run is never used). A finished run is
    replayed to its first reader: for 30 minutes; a page written ahead (`idle`) until it is read,
    however old (opening it must never write it again)."""
    if r.kind not in _ATTACHABLE or r.discarded_at is not None:
        return False
    if r.status == "writing":
        return True
    return (r.status == "done" and r.consumed_at is None and r.finished_at is not None
            and (r.origin == "idle" or now - r.finished_at <= KEEP_FINISHED))


def _new_row(*, user_id: str, page_id: str, n: int, kind: str, origin: str, job: dict[str, Any],
             now: datetime) -> PageRunRow:
    return PageRunRow(
        run_id=f"page:{user_id}:{page_id}:{n}", user_id=user_id, page_id=page_id, n=n, kind=kind,
        origin=origin, job=job, status="writing", started_at=now)


# ---- memory -----------------------------------------------------------------------------------

class MemoryPageRunStore:
    def __init__(self) -> None:
        self._rows: dict[str, PageRunRow] = {}
        self._lock = threading.Lock()

    def _update(self, run_id: str, **fields: Any) -> None:
        self._rows[run_id] = self._rows[run_id].model_copy(update=fields)

    def open_run(self, *, user_id, page_id, kind, origin, job, now, attach):
        with self._lock:
            mine = [r for r in self._rows.values() if r.user_id == user_id and r.page_id == page_id]
            if attach and kind in _ATTACHABLE:
                live = [r for r in mine if _attachable(r, now)]
                if live:
                    return max(live, key=lambda r: r.n), False
            row = _new_row(user_id=user_id, page_id=page_id,
                           n=1 + max((r.n for r in mine), default=0), kind=kind, origin=origin,
                           job=job, now=now)
            self._rows[row.run_id] = row
            return row, True

    def get(self, run_id):
        with self._lock:
            return self._rows.get(run_id)

    def writing_rows(self):
        with self._lock:
            return [r for r in self._rows.values() if r.status == "writing"]

    def live_of(self, user_id):
        with self._lock:
            return sorted((r for r in self._rows.values()
                           if r.user_id == user_id and r.status == "writing"
                           and r.discarded_at is None), key=lambda r: (r.started_at, r.n))

    def latest_of(self, user_id, page_ids):
        with self._lock:
            out: dict[str, PageRunRow] = {}
            for r in self._rows.values():
                if r.user_id == user_id and r.page_id in page_ids and (
                        r.page_id not in out or r.n > out[r.page_id].n):
                    out[r.page_id] = r
            return out

    def finished_since(self, user_id, since, *, origin):
        with self._lock:
            return sorted((r for r in self._rows.values()
                           if r.user_id == user_id and r.origin == origin and r.status != "writing"
                           and r.finished_at is not None and r.finished_at >= since),
                          key=lambda r: r.finished_at, reverse=True)

    def begin(self, run_id, at):
        with self._lock:
            if self._rows[run_id].began_at is None:
                self._update(run_id, began_at=at)

    def set_progress(self, run_id, progress):
        with self._lock:
            self._update(run_id, progress=dict(progress))

    def finish(self, run_id, status, at, *, cost_usd=None, llm_calls=None, failure=None):
        with self._lock:
            if self._rows[run_id].status == "writing":
                self._update(run_id, status=status, finished_at=at, cost_usd=cost_usd,
                             llm_calls=llm_calls, failure=failure)

    def consume(self, run_id, at):
        with self._lock:
            self._update(run_id, consumed_at=at)

    def has_begun_before(self, user_id, page_id, n):
        with self._lock:
            return any(r.user_id == user_id and r.page_id == page_id and r.n < n
                       and r.began_at is not None for r in self._rows.values())

    def discard(self, run_id, at, *, queued_only=False):
        with self._lock:
            if self._rows[run_id].discarded_at is not None or (
                    queued_only and self._rows[run_id].began_at is not None):
                return False
            self._update(run_id, discarded_at=at)
            return True


# ---- postgres ---------------------------------------------------------------------------------

_COLS = ("run_id, user_id, page_id, n, kind, origin, job, status, started_at, finished_at, "
         "consumed_at, discarded_at, began_at, progress, cost_usd, llm_calls, failure")


class PgPageRunStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def _rows(self, sql: str, params: tuple) -> list[PageRunRow]:
        with self._pool.connection() as conn:
            return [PageRunRow.model_validate(d) for d in conn.execute(sql, params).fetchall()]

    def get(self, run_id):
        rows = self._rows(f"select {_COLS} from page_run where run_id = %s", (run_id,))
        return rows[0] if rows else None

    def writing_rows(self):
        return self._rows(f"select {_COLS} from page_run where status = 'writing' "
                          "order by started_at", ())

    def live_of(self, user_id):
        return self._rows(f"select {_COLS} from page_run where user_id = %s and status = 'writing' "
                          "and discarded_at is null order by started_at, n", (user_id,))

    def latest_of(self, user_id, page_ids):
        if not page_ids:
            return {}
        rows = self._rows(
            f"select distinct on (page_id) {_COLS} from page_run where user_id = %s "
            "and page_id = any(%s) order by page_id, n desc", (user_id, list(page_ids)))
        return {r.page_id: r for r in rows}

    def finished_since(self, user_id, since, *, origin):
        return self._rows(f"select {_COLS} from page_run where user_id = %s and origin = %s "
                          "and status <> 'writing' and finished_at >= %s order by finished_at desc",
                          (user_id, origin, since))

    def open_run(self, *, user_id, page_id, kind, origin, job, now, attach):
        for attempt in range(1, OPEN_TRIES + 1):
            try:
                return self._open_once(user_id, page_id, kind, origin, job, now, attach)
            except psycopg.errors.UniqueViolation:
                if attempt == OPEN_TRIES:        # fail closed, not a spin
                    raise
                # (user, page, n) raced with another opener: recompute n and try again
        raise AssertionError("unreachable")

    def _open_once(self, user_id, page_id, kind, origin, job, now, attach):
        with self._pool.connection() as conn, conn.transaction():
            rows = conn.execute(
                f"select {_COLS} from page_run where user_id = %s and page_id = %s order by n desc",
                (user_id, page_id)).fetchall()
            mine = [PageRunRow.model_validate(d) for d in rows]
            if attach and kind in _ATTACHABLE:
                for r in mine:
                    if _attachable(r, now):
                        return r, False
            row = _new_row(user_id=user_id, page_id=page_id,
                           n=1 + max((r.n for r in mine), default=0), kind=kind, origin=origin,
                           job=job, now=now)
            conn.execute(
                "insert into page_run (run_id, user_id, page_id, n, kind, origin, job, status, "
                "started_at) values (%s, %s, %s, %s, %s, %s, %s, 'writing', %s)",
                (row.run_id, user_id, page_id, row.n, kind, origin, Jsonb(job), now))
            return row, True

    def _set(self, sql: str, params: tuple) -> None:
        with self._pool.connection() as conn:
            conn.execute(sql, params)

    def begin(self, run_id, at):
        self._set("update page_run set began_at = %s where run_id = %s and began_at is null",
                  (at, run_id))

    def set_progress(self, run_id, progress):
        self._set("update page_run set progress = %s where run_id = %s", (Jsonb(progress), run_id))

    def finish(self, run_id, status, at, *, cost_usd=None, llm_calls=None, failure=None):
        self._set("update page_run set status = %s, finished_at = %s, cost_usd = %s, "
                  "llm_calls = %s, failure = %s where run_id = %s and status = 'writing'",
                  (status, at, cost_usd, llm_calls, failure, run_id))

    def consume(self, run_id, at):
        self._set("update page_run set consumed_at = %s where run_id = %s", (at, run_id))

    def has_begun_before(self, user_id, page_id, n):
        with self._pool.connection() as conn:
            row = conn.execute("select 1 from page_run where user_id = %s and page_id = %s "
                               "and n < %s and began_at is not null limit 1",
                               (user_id, page_id, n)).fetchone()
            return row is not None

    def discard(self, run_id, at, *, queued_only=False):
        with self._pool.connection() as conn:
            cur = conn.execute("update page_run set discarded_at = %s where run_id = %s "
                               "and discarded_at is null" + (" and began_at is null"
                                                              if queued_only else ""),
                               (at, run_id))
            return cur.rowcount == 1
