"""Page runs (spec 2026-10-14 §4, B1b): a page is written by a run the server owns, not by the HTTP
response that asked for it. A run is one `PageJob` executed by `page_flows.execute`; its SSE items are
a stream that any number of followers read from the start.

Two implementations of `PageRuns`:

- `DbosPageRuns` (the service): the job is the argument of the DBOS workflow `evowiki.page_run`,
  enqueued on the queue `pages` (partitioned by user, one at a time) under `SetWorkflowID(run_id)`;
  the items are the workflow's stream (`DBOS.write_stream`), `follow` is `DBOS.read_stream`. A
  restart resumes the workflow, the stream continues.
- `InlinePageRuns` (tests, scripts): the same body runs in a `durable.virtual_workflow(run_id)` with
  the caller's own deps, the items in an in-memory log, one run at a time per user in priority
  order like the queue. Nothing survives the process.

A user's page runs go through that queue one at a time (write-all, spec 2026-10-15 §4): what the user
asks for has priority `USER_PRIORITY`, the Bot's idle writing `IDLE_PRIORITY` (a lower number goes
first). Both go through `PageRunStore` (`page_run` rows): `open_run` decides new run or attach,
`consume` marks a done run read to the end. A follower that leaves does not cancel the run (the
old cancel-on-no-follower is gone by design); only `discard` does (the user's 「停止」, a path edit,
the assistant), and a running DBOS run then stops at its next step (model and tool calls are steps)."""

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from heapq import heappop, heappush
from threading import Condition, Lock
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from evowiki.agent import durable
from evowiki.wikiapp.page_run_store import (
    KEEP_FINISHED,
    MemoryPageRunStore,
    PageRunRow,
    PageRunStore,
    RunKind,
)
from evowiki.wikiapp.wiki_schemas import ORGANIZE_PAYLOADS, REWRITE_PAYLOADS, ErrorEvent

if TYPE_CHECKING:
    from evowiki.wikiapp.page_flows import PageDeps

log = logging.getLogger("evowiki.wikiapp.page_runs")

PAGE_QUEUE = "pages"
USER_PRIORITY = 1                       # DBOS: a lower number is dequeued first
IDLE_PRIORITY = 100


# a `writing` row whose DBOS workflow does not exist: younger than this it is a concurrent request
# between its row insert and its `start_workflow` (leave it / start it); older, nobody will ever
# start it (the process died in that gap and recovery has nothing to recover) -> `failed`
NO_WORKFLOW_GRACE = timedelta(seconds=60)
TERMINAL = frozenset({"SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"})


def spawn_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="page-write", daemon=True).start()


def spawn_inline(work: Callable[[], None]) -> None:
    """Runs the work at once (tests, scripts): the run is finished when `start` returns."""
    work()

Item = tuple[str, BaseModel]            # (sse event name, payload)
STREAM_KEY = "items"
END_EVENTS = frozenset({"done", "error"})


class _Job(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FeedbackArgs(_Job):
    section_id: str
    kind: Literal["deeper", "other"]
    shown: str | None = None           # the section version the user was shown (artifact id, 0091)


class RefineArgs(_Job):
    job: dict
    scope: dict | None = None
    spec_version: int | None = None


class ReviseArgs(_Job):
    """A revision from materials (`source_revise`): the `page_revise` ledger row the run fills."""

    action_id: str
    source_ids: tuple[str, ...] = ()          # () = the run picks the materials itself


class PageJob(_Job):
    """The workflow's only argument (JSON): everything a run needs besides the service's deps."""

    kind: RunKind
    user_id: str
    page_id: str
    now: datetime
    origin: Literal["open", "idle", "background"] = "open"   # idle: the Bot, nobody asked for it
    before: tuple[str | None, ...] | None = None    # the pages before this one on its path, as planned
    path_id: str | None = None          # the path `before` is from
    feedback: FeedbackArgs | None = None
    refine: RefineArgs | None = None
    revise: ReviseArgs | None = None


# ---- stream items -----------------------------------------------------------------------------

def _registry() -> dict[str, type[BaseModel]]:
    classes = [*ORGANIZE_PAYLOADS.values(), *REWRITE_PAYLOADS.values()]
    return {c.__name__: c for c in classes}


def encode_item(name: str, payload: BaseModel) -> dict:
    return {"event": name, "type": type(payload).__name__, "data": payload.model_dump(mode="json")}


def decode_item(d: dict) -> Item:
    cls = _registry().get(d.get("type", ""))
    if cls is None:
        raise ValueError(f"unknown stream item type {d.get('type')!r}")
    return d["event"], cls.model_validate(d["data"])


def _ended_early() -> Item:
    return "error", ErrorEvent(code="internal", retryable=True,
                               detail="write run ended without done")


@dataclass(frozen=True)
class PageRun:
    """What the body sees of its run."""

    run_id: str
    user_id: str
    page_id: str
    origin: str
    emit: Callable[[str, BaseModel], None]
    # what the run has spent (USD) when it ends without `done`: [0] is set by the body
    spent: list[float] = field(default_factory=lambda: [0.0])
    # `(cost so far, organizer-side calls, section-writing calls)`: what the row shows while it writes
    on_spend: Callable[[float, int, int], None] | None = None

    def report_spend(self, cost: float, outline_calls: int, section_calls: int) -> None:
        if self.on_spend is not None:
            self.on_spend(cost, outline_calls, section_calls)


class PageRuns(Protocol):
    store: PageRunStore

    def start(self, job: PageJob, deps: "PageDeps", *, attach: bool) -> tuple[str, bool]:
        """(run_id, started): the page's live run when `attach` and there is one (started False),
        else a new run."""
        ...

    def follow(self, run_id: str, *, consume: bool = True) -> Iterator[Item]:
        """Replays the run's items from the first, then follows them to `done` / `error`. A `done`
        read to the end marks the run consumed unless `consume` is False (a watcher: it only looks)."""
        ...

    def discard(self, run_id: str, at: datetime, *, queued_only: bool = False,
                failure: str = "cancelled") -> bool:
        """Marks the run discarded in the store (False: it was claimed or discarded already, nothing
        changed) and stops it if it is still running. `queued_only`: only if it has not begun.
        `failure`: the code a stopped run's row ends with ("preempted": `_preempt_idle`)."""
        ...


def _row_args(job: PageJob) -> dict:
    return {"user_id": job.user_id, "page_id": job.page_id, "kind": job.kind,
            "origin": job.origin, "job": job.model_dump(mode="json"), "now": job.now}


def priority_of(job: PageJob) -> int:
    """The queue priority: what the user asks for goes before the Bot's idle writing."""
    return IDLE_PRIORITY if job.origin == "idle" else USER_PRIORITY


PREEMPTED = "preempted"     # an idle run stopped for the user's own write (`_preempt_idle`)


def _preempt_idle(runs: "PageRuns", job: PageJob) -> None:
    """The user asks for a page (origin `open`: he waits for it) while the Bot writes another page
    ahead (an idle run that has begun): the queue runs one write per user and never reorders a
    begun run, so the Bot's run is stopped (DBOS cancel; failure `preempted`) and the user's starts
    at once. What it committed stays under the writing mark; the Bot writes that page again later
    (`WriteQueue._stopped` does not count a preempted run as the learner's 「停止」). A run of the
    same page is joined, not stopped; a write the user waits for is never stopped."""
    if job.origin != "open":
        return
    for row in runs.store.live_of(job.user_id):
        if (row.origin == "idle" and row.began_at is not None and row.page_id != job.page_id
                and runs.discard(row.run_id, job.now, failure=PREEMPTED)):
            log.info("page run %s (idle) preempted by the user's write of %s", row.run_id,
                     job.page_id)


def _demote_queued_idle(runs: "PageRuns", job: PageJob, attach: bool) -> None:
    """A user asks for a page the idle Bot has queued but not begun: that run has the idle priority,
    so it is dropped and the user's run takes the page (a begun run is attached to, it cannot be
    reordered)."""
    if job.origin == "idle" or not attach:
        return
    for row in runs.store.live_of(job.user_id):
        if row.page_id == job.page_id and row.origin == "idle" and row.began_at is None:
            runs.discard(row.run_id, job.now)


# ---- inline -----------------------------------------------------------------------------------

class _Log:
    """The items of an inline run and whether it ended; followers wait on it."""

    def __init__(self) -> None:
        self.items: list[Item] = []
        self.closed = False
        self._cond = Condition()

    def emit(self, name: str, payload: BaseModel) -> None:
        with self._cond:
            if self.closed:
                return
            self.items.append((name, payload))
            if name in END_EVENTS:       # nothing follows an end event: followers are released
                self.closed = True
            self._cond.notify_all()

    def close(self) -> None:
        """Ends the log; a run whose work never said done / error gets an `internal` error."""
        with self._cond:
            if self.closed:
                return
            self.items.append(_ended_early())
            self.closed = True
            self._cond.notify_all()

    def read(self) -> Iterator[Item]:
        i = 0
        while True:
            with self._cond:
                while i >= len(self.items) and not self.closed:
                    self._cond.wait()
                batch, closed = self.items[i:], self.closed
                i += len(batch)
            yield from batch
            if closed and i >= len(self.items):
                return


class InlinePageRuns:
    """Runs `page_flows.execute` with the caller's deps inside a virtual workflow named by the run
    id (step keys are `<run_id>:<n>`, unique per run). A user's runs go one at a time, the lower
    priority number first (the DBOS queue's rule). `spawn` runs the work (inline by default: the
    run is finished when `start` returns; `spawn_thread` for a real worker). `deps.after` (the
    write queue's kick) is called once a run is over, outside its workflow."""

    def __init__(self, spawn: Callable[[Callable[[], None]], None] = spawn_inline,
                 store: PageRunStore | None = None):
        # a memory store for unit tests of the runner; a service must pass the Postgres store (run
        # ids repeat across a memory store's lives, and effect keys are `<run_id>:<step>`)
        self._spawn = spawn
        self.store: PageRunStore = store if store is not None else MemoryPageRunStore()
        self._logs: dict[str, _Log] = {}
        self._lock = Lock()
        self._pending: dict[str, list[tuple[int, int, Callable[[], None]]]] = {}
        self._running: set[str] = set()
        self._seq = 0

    def start(self, job: PageJob, deps: "PageDeps", *, attach: bool) -> tuple[str, bool]:
        from evowiki.wikiapp import page_flows
        _demote_queued_idle(self, job, attach)
        _preempt_idle(self, job)
        with self._lock:
            row, created = self.store.open_run(attach=attach, **_row_args(job))
            if not created:
                return row.run_id, False
            self._prune()
            items = self._logs[row.run_id] = _Log()
        run_id = row.run_id

        def work() -> None:
            try:
                current = self.store.get(run_id)
                if current is not None and current.discarded_at is not None:
                    # dropped while it was queued: it never runs
                    self.store.finish(run_id, "failed", datetime.now(UTC), failure="cancelled")
                    items.emit("error", ErrorEvent(code="internal", retryable=True,
                                                   detail="write run cancelled"))
                    return
                with durable.virtual_workflow(run_id):
                    page_flows.execute(job, run_id, items.emit, deps)
            except Exception as exc:    # execute fails closed itself; this is a bug in it
                items.emit("error", ErrorEvent(code="internal", retryable=True,
                                               detail=f"{type(exc).__name__}: {exc}"[:500]))
                log.exception("page run %s crashed", run_id)
            finally:
                items.close()
                if deps.after is not None:
                    try:
                        deps.after(job.user_id)
                    except Exception:
                        log.exception("after page run %s failed", run_id)
        self._submit(job.user_id, priority_of(job), work)
        return run_id, True

    def _submit(self, user_id: str, priority: int, work: Callable[[], None]) -> None:
        with self._lock:
            self._seq += 1
            heappush(self._pending.setdefault(user_id, []), (priority, self._seq, work))
        self._pump(user_id)

    def _pump(self, user_id: str) -> None:
        with self._lock:
            queue = self._pending.get(user_id)
            if user_id in self._running or not queue:
                return
            _, _, work = heappop(queue)
            self._running.add(user_id)

        def run() -> None:
            try:
                work()
            finally:
                with self._lock:
                    self._running.discard(user_id)
                self._pump(user_id)
        self._spawn(run)

    def _prune(self) -> None:
        """Drops the logs of ended runs nobody can attach to any more (failed, discarded, consumed);
        called under the lock when a run starts (a log is kept while another follower may still replay it, so
        it goes at the next start, not when the first follower has read it)."""
        for rid in [rid for rid, lg in self._logs.items() if lg.closed]:
            row = self.store.get(rid)
            if (row is None or row.status == "failed" or row.discarded_at or row.consumed_at
                    or row.kind != "organize"                          # never attached to
                    or (row.finished_at is not None
                        and datetime.now(UTC) - row.finished_at > KEEP_FINISHED
                        and row.origin != "idle")):
                del self._logs[rid]

    def follow(self, run_id: str, *, consume: bool = True) -> Iterator[Item]:
        items = self._logs.get(run_id)
        if items is None:                  # a row from another process / store: nothing to replay
            yield _ended_early()
            return
        last = None
        for name, payload in items.read():
            last = name
            yield name, payload
            if name in END_EVENTS:
                break
        if last == "done" and consume:
            self.store.consume(run_id, datetime.now(UTC))

    def discard(self, run_id: str, at: datetime, *, queued_only: bool = False,
                failure: str = "cancelled") -> bool:
        return self.store.discard(run_id, at, queued_only=queued_only)


# ---- DBOS -------------------------------------------------------------------------------------

_queue = None                   # the DBOS queue `pages` of the launch it was registered on
_queue_owner: object = None


def page_queue():
    """The queue every page run goes through: partitioned by user, one run at a time per user.
    Registered right after the DBOS launch (`DurableRuntime.start`); a launch that did not
    register it (tests) gets it here, once."""
    global _queue, _queue_owner
    from dbos import DBOS
    from dbos._dbos import _dbos_global_instance
    if _queue is None or _queue_owner is not _dbos_global_instance:
        _queue = DBOS.register_queue(PAGE_QUEUE, partition_concurrency=1)
        _queue_owner = _dbos_global_instance
    return _queue


def register_queue() -> None:
    """Call right after the one DBOS launch: a restart's still-queued runs need their queue to be
    dispatched again."""
    page_queue()


class DbosPageRuns:
    """The service's runs: workflows in DBOS, items in the workflow's stream. The deps of a run come
    from `page_flows.install` (recovery has no caller), not from `start`'s argument."""

    def __init__(self, store: PageRunStore):
        self.store = store

    def start(self, job: PageJob, deps: "PageDeps", *, attach: bool) -> tuple[str, bool]:
        from dbos import SetEnqueueOptions, SetWorkflowID

        from evowiki.agent import dbos_app
        from evowiki.wikiapp.page_flows import page_workflow
        if not dbos_app.launched():     # fail closed: never run a page inline in the service
            raise RuntimeError("DBOS is not launched: page runs need the service's durable runtime")
        queue = page_queue()
        _demote_queued_idle(self, job, attach)
        _preempt_idle(self, job)
        row, created = self.store.open_run(attach=attach, **_row_args(job))
        if not created and row.status == "writing" and self._unfinishable(row):
            # a `writing` row that can never finish would block its page forever: close it, open anew
            self.store.finish(row.run_id, "failed", datetime.now(UTC), failure="unfinishable")
            row, created = self.store.open_run(attach=attach, **_row_args(job))
        if created or row.status == "writing":
            # idempotent by id: a crash between the insert and the enqueue is repaired here. One
            # run at a time per user (the queue's partition), the user's own requests first
            with SetWorkflowID(row.run_id), SetEnqueueOptions(
                    queue_partition_key=row.user_id, priority=priority_of(job)):
                queue.enqueue(page_workflow, row.job)
        return row.run_id, created

    def _unfinishable(self, row: PageRunRow) -> bool:
        """A `writing` row only leaves that state through the workflow's own `run.finish` step, which
        workflow-level failures skip (bad install / job, a failing finish, max recovery attempts).
        Unfinishable = the workflow ended (terminal status: a restart of the same id replays its
        outcome and never runs), or belongs to another application version (recovery only picks
        up its own version's workflows: it stays pending forever; this is what an `APP_VERSION`
        bump does to the runs that were in flight, FINDINGS B1b), or it does not exist and the row
        is older than `NO_WORKFLOW_GRACE` (see there). Fail closed: those rows become `failed`;
        a younger row without a workflow is a concurrent request that is about to start it."""
        from dbos import DBOS
        from dbos._utils import GlobalParams
        st = DBOS.get_workflow_status(row.run_id)
        if st is None:
            return datetime.now(UTC) - row.started_at > NO_WORKFLOW_GRACE
        return st.status in TERMINAL or st.app_version != GlobalParams.app_version

    def sweep(self) -> int:
        """After launch (and its recovery): closes every `writing` row that can never finish.
        Returns how many."""
        n = 0
        for row in self.store.writing_rows():
            if self._unfinishable(row):
                self.store.finish(row.run_id, "failed", datetime.now(UTC), failure="unfinishable")
                log.warning("page run %s can never finish: marked failed", row.run_id)
                n += 1
        return n

    def follow(self, run_id: str, *, consume: bool = True) -> Iterator[Item]:
        from dbos import DBOS
        last = None
        for raw in DBOS.read_stream(run_id, STREAM_KEY, polling_interval_sec=0.1):
            try:
                name, payload = decode_item(raw)
            except ValueError:        # an item this build cannot read (incl. pydantic errors)
                log.exception("page run %s: unreadable stream item", run_id)
                yield _ended_early()
                return
            last = name
            yield name, payload
            if name in END_EVENTS:
                break
        if last not in END_EVENTS:
            yield _ended_early()
            return
        if last == "done" and consume:
            self.store.consume(run_id, datetime.now(UTC))

    def discard(self, run_id: str, at: datetime, *, queued_only: bool = False,
                failure: str = "cancelled") -> bool:
        """A discarded run stops spending: the workflow is cancelled while it is still pending /
        enqueued (it stops at its next step; the row then stays `writing`, so it is closed as
        `failed` with `failure` here)."""
        if not self.store.discard(run_id, at, queued_only=queued_only):
            return False
        from dbos import DBOS
        try:
            st = DBOS.get_workflow_status(run_id)
            if st is not None and st.status in ("PENDING", "ENQUEUED"):
                DBOS.cancel_workflow(run_id)
                self.store.finish(run_id, "failed", at, failure=failure)
        except Exception:
            log.warning("could not cancel workflow %s", run_id, exc_info=True)
        return True


# ---- no runner ------------------------------------------------------------------------------

class NoPageRuns:
    """`Services.runs` until a real runner is set: every use fails closed (a service that builds
    `Services` without runs must not write pages on a memory store whose run ids would repeat)."""

    @property
    def store(self) -> PageRunStore:
        raise RuntimeError("no page runner configured: set Services.runs")

    def start(self, job: PageJob, deps: "PageDeps", *, attach: bool) -> tuple[str, bool]:
        raise RuntimeError("no page runner configured: set Services.runs")

    def follow(self, run_id: str, *, consume: bool = True) -> Iterator[Item]:
        raise RuntimeError("no page runner configured: set Services.runs")

    def discard(self, run_id: str, at: datetime, *, queued_only: bool = False,
                failure: str = "cancelled") -> bool:
        raise RuntimeError("no page runner configured: set Services.runs")
