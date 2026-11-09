"""Putting a material to a use (design §8): one place for `POST /wiki/sources/{id}/uses` and the
assistant's tool. `auto` lets the material decide (`use.guess_material_use`: exam, paper, else
learn); the use is recorded through `events.source_added`, and a learning use starts the next
import run, in mode full (summaries, every page read), when the material has no full import yet:
the first learning use of a chat attachment (ask only so far), or one after a full import that
could not start or ended with an error (`needs_full_run`). That run keeps the sections the source
has (`keeps_sections`): only the use changed, so the user's toc edits and links stay and the run
adds what the use needs (summaries, vectors, glossary, pages read). Also the run helpers the
routes share (`next_run`, `start_next`).

A goal is planned from a material's sections, so a text-first PDF is parsed before that, and the
plan waits (`await_layout`).

A run that cannot start is not left to the user: `start_next` hands it to the durable start
(`SourceRuns.start_later`, the DBOS workflow `evowiki.source_start`), which tries again with
waits in between and records the plain failure MSG_NOT_STARTED only after its last try. Only when
even that cannot be scheduled does the source fail at once (RunNotStarted)."""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from psycopg_pool import ConnectionPool

from evowiki.sources import events, store
from evowiki.sources.importer import MSG_LAYOUT_START, MSG_TOC, ImportJob, SourceProgress
from evowiki.sources.models import SourceRow, Use
from evowiki.sources.runs import SourceRuns
from evowiki.sources.titles import prompt_title
from evowiki.sources.use import guess_material_use
from evowiki.store.locks import user_writer

log = logging.getLogger(__name__)

MSG_STILL_READING = "资料还在读"
MSG_READ_FAILED = "资料没有读出来"
# a run that could not start even after the durable retries: a plain failure, no button named
MSG_NOT_STARTED = "资料没能开始整理"
# uses that need the whole import (sections summarized, every page read)
LEARNING = frozenset({"learn", "understand", "exam", "supplement"})
LAYOUT_WAIT_S = 3600.0        # a goal waits this long for its materials (a 1000-page book reads)
LAYOUT_POLL_S = 1.0


class StillReading(RuntimeError):
    """`auto` on a material that has no sections yet: there is nothing to judge (HTTP 409)."""

    detail = MSG_STILL_READING


class ReadFailed(RuntimeError):
    """`auto` on a material whose import failed: there is nothing to judge (HTTP 409)."""

    detail = MSG_READ_FAILED


class RunNotStarted(RuntimeError):
    """The next import run could not start nor be handed to the durable retries; the source
    says so (HTTP 503). `detail`: the text the source shows (MSG_NOT_STARTED)."""

    def __init__(self, msg: str, detail: str = MSG_NOT_STARTED):
        super().__init__(msg)
        self.detail = detail


@dataclass(frozen=True)
class UseAdded:
    use_id: str
    use: Use                          # the use recorded (the guessed one for `auto`)
    created_at: datetime


def ask_only(uses: list[dict]) -> bool:
    """Only ever attached in a chat (an upload without a use was imported in full)."""
    return bool(uses) and all(u["use"] == "ask" for u in uses)


def needs_full_run(row: SourceRow, before: list[dict], use: str) -> bool:
    """A learning use starts a full run when the material was only ever attached to ask (a run
    of its may be going: the full one replaces it), or when no run is going and the newest run
    did not finish a full import (it could not start, was interrupted or ended with an error),
    so 「再说一次」 starts it. The importer records a finished run's mode in its end; an end
    without one is from before modes were recorded, when every import was full."""
    if use not in LEARNING:
        return False
    if ask_only(before):
        return True
    end = row.progress.get("end")
    if end is None and row.status != "failed":
        return False                               # a run is going
    return not (end == "done" and row.progress.get("mode", "full") == "full")


def keeps_sections(row: SourceRow) -> bool:
    """A later run keeps the sections the source has (`ImportJob.keep_sections`: their ids, the
    user's toc edits and the links to them stay), unless the run before ended with an error at
    its toc: then the sections are the page-group fallback of a 402 or the spending cap (or
    there are none), and building the toc again is what the new run is for."""
    p = row.progress
    return not (p.get("end") == "error" and p.get("stage") == "toc")


def next_run(conn, uid: str, row: SourceRow, *, mode: Literal["full", "ask"],
             layout_now: bool = False) -> ImportJob:
    """In the caller's transaction: the next run generation, a fresh progress record (no `end`:
    a second request is refused until this run ends) and, for a failed source, status reading.
    `row`: the source before this call (`keeps_sections` reads its last run's end).
    `layout_now`: `ImportJob.layout_now`."""
    keep = keeps_sections(row)
    gen = store.bump_run(conn, uid, row.source_id)
    store.set_progress(conn, uid, row.source_id, SourceProgress(
        stage="parse", done=0, total=1, message="正在解析").model_dump())
    if row.status == "failed":
        store.set_status(conn, uid, row.source_id, status="reading", error=None)
    return ImportJob(user_id=uid, source_id=row.source_id, gen=gen, mode=mode,
                     keep_sections=keep, layout_now=layout_now)


def unstarted(row: SourceRow | None, job: ImportJob) -> bool:
    """`job` is still the source's newest run and has neither ended nor failed: starting it
    is still wanted."""
    return (row is not None and row.deleted_at is None and row.run_gen == job.gen
            and "end" not in row.progress and row.status != "failed")


def record_not_started(pool: ConnectionPool, job: ImportJob) -> bool:
    """The run could not start for good: the source says so (an error end; a source that was
    reading fails), unless the run is no longer wanted (deleted, a newer run, ended). True when
    it was written."""
    end = SourceProgress(stage="parse", done=0, total=1, message=MSG_NOT_STARTED).model_dump()
    end |= {"end": "error", "detail": MSG_NOT_STARTED}
    with user_writer(pool, job.user_id) as tx:
        row = store.get_source(tx.conn, job.user_id, job.source_id)
        if not unstarted(row, job):
            return False
        store.set_progress(tx.conn, job.user_id, job.source_id, end)
        if row.status == "reading":
            store.set_status(tx.conn, job.user_id, job.source_id, status="failed",
                             error=MSG_NOT_STARTED)
    return True


def start_next(pool: ConnectionPool, runs: SourceRuns, job: ImportJob) -> None:
    """Starts the run `next_run` made (or a new source's first run). If it cannot start now it
    goes to the durable retries (`runs.start_later`); only when that fails too does the source
    say so at once (`record_not_started`) and RunNotStarted is raised."""
    try:
        runs.start(job)
        return
    except Exception:
        log.exception("could not start run %d of %s: retrying it durably", job.gen,
                      job.source_id)
    try:
        runs.start_later(job)
        return
    except Exception as e:
        log.exception("could not schedule the retries of run %d of %s", job.gen, job.source_id)
        record_not_started(pool, job)
        raise RunNotStarted(f"run {job.gen} of {job.source_id} did not start") from e


def apply_use(pool: ConnectionPool, runs: SourceRuns, user_id: str, source_id: str,
              use: Use | Literal["auto"], now: datetime, *, goal_id: str | None = None,
              page_id: str | None = None, session_id: str | None = None,
              effect: Callable[[object, str], None] | None = None) -> UseAdded:
    """Records the use (`auto`: guessed from the material's title and sections) and starts the
    full import when a learning use finds none done yet (`needs_full_run`). Unknown, foreign or
    deleted source → LookupError; `auto` on a failed import → ReadFailed, with no sections yet →
    StillReading; a run that cannot
    start → RunNotStarted (the use stays recorded, the source shows the error).
    `effect(conn, use_id)`: run in the same transaction as the use (the assistant's ledger row is
    marked executed, with the use as its undo data, together with it)."""
    with user_writer(pool, user_id) as tx:
        added, rerun = record_use(tx.conn, user_id, source_id, use, now, goal_id=goal_id,
                                  page_id=page_id, session_id=session_id, effect=effect)
    if rerun is not None:
        start_next(pool, runs, rerun)
    return added


def record_use(conn, user_id: str, source_id: str, use: Use | Literal["auto"], now: datetime,
               *, goal_id: str | None = None, page_id: str | None = None,
               session_id: str | None = None,
               effect: Callable[[object, str], None] | None = None,
               ) -> tuple[UseAdded, ImportJob | None]:
    """`apply_use`'s writes, in the caller's transaction (which holds the user's writer lock): the
    use and, when it needs the full import, the next run generation. Returns the use and that run
    (None: none needed); the caller starts it with `start_next` AFTER its commit. Errors as
    `apply_use`."""
    row = store.get_source(conn, user_id, source_id)
    if row is None or row.deleted_at is not None:
        raise LookupError(f"source {source_id} not found for user {user_id}")
    if use == "auto":
        if row.status == "failed":
            raise ReadFailed(f"source {source_id} could not be read")
        sections = store.sections(conn, user_id, source_id)
        if not sections:
            raise StillReading(f"source {source_id} has no sections yet")
        use = guess_material_use(row, sections)
        log.info("source %s/%s: auto use is %s", user_id, source_id, use)
    before = store.uses_of(conn, user_id, source_id)
    use_id = events.source_added(conn, user_id, source_id, use, now, goal_id=goal_id,
                                 page_id=page_id, session_id=session_id)
    if effect is not None:
        effect(conn, use_id)
    rerun = next_run(conn, user_id, row, mode="full") if needs_full_run(row, before, use) else None
    return UseAdded(use_id=use_id, use=use, created_at=now), rerun


class MaterialsNotReady(RuntimeError):
    """A material was still being read when the wait ended."""


def await_layout(pool: ConnectionPool, runs: SourceRuns, user_id: str, source_ids: Sequence[str],
                 *, parse: bool, say: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 timeout_s: float = LAYOUT_WAIT_S) -> None:
    """Until these materials can be planned from; `say(line)`: the state while it waits. A
    material still being read is waited for. `parse`: also a text-first PDF's layout (`next_run`
    with `layout_now`, the sections rebuilt from it) unless a run going parses it anyway; when
    the parse fails the run ends with the text layer, and that is what there is to plan from. A
    material deleted or failed meanwhile is left for the caller's checks. Over `timeout_s`:
    MaterialsNotReady. Holds no connection while waiting; a replay finds the parse done."""
    left = timeout_s
    started: set[str] = set()
    shown = ""
    for sid in dict.fromkeys(source_ids):
        while True:
            with pool.connection() as conn:
                row = store.get_source(conn, user_id, sid)
            if row is None or row.deleted_at is not None or row.status == "failed":
                break
            pending = parse and row.text_first
            if row.status != "reading" and not pending:
                break
            if pending and "end" in row.progress:
                if sid in started:
                    break                  # the parse failed: the text layer stays
                _start_layout(pool, runs, user_id, sid)
                started.add(sid)
                continue
            line = _waiting_line(row)
            if say is not None and line != shown:
                shown = line
                say(line)
            if left <= 0:
                raise MaterialsNotReady(f"source {sid} is still being read")
            sleep(LAYOUT_POLL_S)
            left -= LAYOUT_POLL_S


def _start_layout(pool: ConnectionPool, runs: SourceRuns, user_id: str, source_id: str) -> None:
    with user_writer(pool, user_id) as tx:
        row = store.get_source(tx.conn, user_id, source_id)
        if row is None or row.deleted_at is not None or "end" not in row.progress:
            return                         # deleted, or a run started meanwhile: wait for that
        job = next_run(tx.conn, user_id, row, mode="ask", layout_now=True)
    start_next(pool, runs, job)


def _waiting_line(row: SourceRow) -> str:
    """The material's own progress, as the turn's status line."""
    total = row.progress.get("total")
    title = prompt_title(row.title)
    message = row.progress.get("message")
    if message == MSG_LAYOUT_START:
        return f"正在启动版面解析，要读《{title}》"
    if message == MSG_TOC:
        return f"正在为《{title}》排目录"
    if row.progress.get("stage") == "parse" and total and total > 1:
        return f"正在读《{title}》的版面，{row.progress.get('done', 0)}/{total} 页"
    return f"正在读《{title}》"


def start_unstarted(pool: ConnectionPool, runs: SourceRuns) -> int:
    """At start-up (after DBOS launched): a live source whose newest run has no end and no
    workflow (the process died between recording the run and starting it) has that run started
    now, through `start_next` (the user asked for it; nobody has to ask again): mode ask for a
    chat attachment only, else full, as `/retry` would. Returns how many were handed on."""
    with pool.connection() as conn:
        rows = store.unfinished_runs(conn)
    handed = 0
    for row in rows:
        if runs.exists(row.user_id, row.source_id, row.run_gen):
            continue
        with pool.connection() as conn:
            mode = "ask" if ask_only(store.uses_of(conn, row.user_id, row.source_id)) else "full"
        # the run before's end was overwritten when this one was recorded: keep the sections
        # there are (losing the user's toc edits is worse than keeping a page-group toc)
        job = ImportJob(user_id=row.user_id, source_id=row.source_id, gen=row.run_gen, mode=mode,
                        keep_sections=True)
        log.warning("import run %d of %s/%s was never started: starting it", row.run_gen,
                    row.user_id, row.source_id)
        try:
            start_next(pool, runs, job)
        except RunNotStarted:
            continue                           # the source says so; logged by start_next
        handed += 1
    return handed
