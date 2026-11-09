"""Import runs (design §5): one workflow per source and run generation, id
`source:<user>:<source>` for generation 0 and `source:<user>:<source>:<gen>` after it (a retry
or a learning use without a full import bumps the source's `run_gen` and starts the next one);
its items a stream that any number of followers read from the start (the same scheme as
page runs).
`follow` and `cancel` act on the source's newest generation.

- `DbosSourceRuns` (the service): `evowiki.source_import` enqueued on the queue `SOURCE_QUEUE`
  (one import at a time, across all users and processes) under `SetWorkflowID`; a second start of
  the same id returns the running / finished workflow, it never runs twice. `follow` is
  `DBOS.read_stream`; while the run still waits in the queue it first says so (`MSG_QUEUED`, a
  `progress` item the card shows as its message).
- `InlineSourceRuns` (tests, scripts): the same body in a `durable.virtual_workflow`, the items
  in an in-memory log.

A follower always gets an end item: when the stream has none (the run stopped because the
source was deleted, the workflow is unknown, the log is gone after a restart) the end is told
from the source's stored state (`settled`)."""

import contextvars
import logging
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Condition, Lock
from typing import Any, Protocol

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from evowiki.agent import durable
from evowiki.sources import store
from evowiki.sources.importer import (
    ImportDeps,
    ImportJob,
    SourceDone,
    SourceFailed,
    SourceProgress,
    execute,
)

log = logging.getLogger("evowiki.sources.runs")

Item = tuple[str, BaseModel]
END_EVENTS = frozenset({"done", "error"})
_REGISTRY: dict[str, type[BaseModel]] = {
    c.__name__: c for c in (SourceProgress, SourceDone, SourceFailed)}

MSG_NOT_FOUND = "找不到这份资料"
MSG_DELETED = "这份资料已经删除了"
MSG_FAILED = "导入失败了，可以删掉重新上传"
MSG_INTERRUPTED = "导入中断了，可以删掉重新上传"
MSG_QUEUED = "排队中，轮到这份就开始"
SOURCE_QUEUE = "source-imports"
ASK_QUEUE = "source-asks"
TEXT_QUEUE = "source-texts"
START_WAIT_S = 5.0     # how long a follower waits for a run that was just asked for


def workflow_id(user_id: str, source_id: str, gen: int = 0) -> str:
    return f"source:{user_id}:{source_id}" + (f":{gen}" if gen else "")


def start_workflow_id(job: ImportJob) -> str:
    """The durable start of a run that could not start at once (`DbosSourceRuns.start_later`)."""
    return "start:" + workflow_id(job.user_id, job.source_id, job.gen)


def current_gen(pool: ConnectionPool, user_id: str, source_id: str) -> int:
    """The source's newest run generation (0 when it is unknown)."""
    with pool.connection() as conn:
        row = store.get_source(conn, user_id, source_id)
    return 0 if row is None else row.run_gen


def encode_item(name: str, payload: BaseModel) -> dict:
    return {"event": name, "type": type(payload).__name__, "data": payload.model_dump(mode="json")}


def decode_item(d: dict) -> Item:
    cls = _REGISTRY.get(d.get("type", ""))
    if cls is None:
        raise ValueError(f"unknown source stream item type {d.get('type')!r}")
    return d["event"], cls.model_validate(d["data"])


def settled(pool: ConnectionPool, user_id: str, source_id: str, *,
            cancelled: bool = False) -> Item:
    """The end item of a run whose stream has none, from the source's stored state. A
    `cancelled` run without a recorded end is being deleted (the delete route cancels before it
    soft-deletes), so it says deleted even if the row is not marked yet."""
    with pool.connection() as conn:
        row = store.get_source(conn, user_id, source_id)
        n = (len(store.sections(conn, user_id, source_id))
             if row is not None and row.deleted_at is None else 0)
    from evowiki.sources.importer import failure_detail
    if row is None:
        return "error", SourceFailed(detail=failure_detail(MSG_NOT_FOUND, "source_not_found", {}), code="source_not_found")
    if row.deleted_at is not None:
        return "error", SourceFailed(detail=failure_detail(MSG_DELETED, "source_deleted", {}), code="source_deleted")
    code = row.progress.get("error_code")
    code = code if isinstance(code, str) else None
    params = row.progress.get("error_params", {})
    if row.status == "failed":
        return "error", SourceFailed(detail=failure_detail(row.error or MSG_FAILED, code, params), code=code)
    end = row.progress.get("end")
    if end == "error":
        return "error", SourceFailed(detail=failure_detail(str(row.progress.get("detail") or MSG_FAILED), code, params), code=code)
    if cancelled and end != "done":
        return "error", SourceFailed(detail=failure_detail(MSG_DELETED, "source_deleted", {}), code="source_deleted")
    if end == "done":
        return "done", SourceDone(status=row.status, title=row.title, pages=row.pages, sections=n,
                                  unreadable_pages=list(row.unreadable_pages),
                                  note=row.progress.get("note"))
    return "error", SourceFailed(detail=failure_detail(MSG_INTERRUPTED, "source_interrupted", {}), code="source_interrupted")


def starting(pool: ConnectionPool, user_id: str, source_id: str) -> bool:
    """A live source whose newest run has no end yet (it may not have started yet)."""
    with pool.connection() as conn:
        row = store.get_source(conn, user_id, source_id)
    return (row is not None and row.deleted_at is None and "end" not in row.progress
            and row.status != "failed")


class SourceRuns(Protocol):
    def start(self, job: ImportJob) -> str:
        """The run's workflow id; a run with that id already started is not started again."""
        ...

    def follow(self, user_id: str, source_id: str) -> Iterator[Item]:
        """Replays the run's items from the first, then follows them to `done` / `error`."""
        ...

    def cancel(self, user_id: str, source_id: str) -> None:
        """Stops a run still in progress (the delete route calls it before `soft_delete`)."""
        ...

    def exists(self, user_id: str, source_id: str, gen: int) -> bool:
        """A run of that generation was started (in any state)."""
        ...

    def start_later(self, job: ImportJob) -> None:
        """`start` failed: hand the run to durable retries (`flows.source_start_workflow`),
        which record the failure only after the last try. Raises when that cannot be done."""
        ...


# ---- inline -----------------------------------------------------------------------------------

def spawn_inline(work: Callable[[], None]) -> None:
    work()


class _Log:
    def __init__(self) -> None:
        self.items: list[Item] = []
        self.closed = False
        self._cond = Condition()

    def emit(self, name: str, payload: BaseModel) -> None:
        with self._cond:
            if self.closed:
                return
            self.items.append((name, payload))
            if name in END_EVENTS:
                self.closed = True
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
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


class InlineSourceRuns:
    """`importer.execute` with the given deps in a virtual workflow named by the run id. `spawn`
    runs the work (inline by default: the run has finished when `start` returns)."""

    def __init__(self, deps: ImportDeps,
                 spawn: Callable[[Callable[[], None]], None] = spawn_inline):
        self.deps = deps
        self._spawn = spawn
        self._logs: dict[str, _Log] = {}
        self._lock = Lock()
        self.started: list[str] = []          # ids of the runs this runner started (tests)
        self._cancelled: set[str] = set()

    def start(self, job: ImportJob) -> str:
        wid = workflow_id(job.user_id, job.source_id, job.gen)
        with self._lock:
            if wid in self._logs:
                return wid
            items = self._logs[wid] = _Log()
            self.started.append(wid)

        def work() -> None:
            try:
                with durable.virtual_workflow(wid):
                    execute(job, items.emit, self.deps)
            except Exception:       # execute fails closed itself; this is a bug in it
                log.exception("source run %s crashed", wid)
            finally:
                items.close()
        self._spawn(work)
        return wid

    def follow(self, user_id: str, source_id: str) -> Iterator[Item]:
        wid = workflow_id(user_id, source_id, current_gen(self.deps.pool, user_id, source_id))
        items = self._logs.get(wid)
        last = None
        if items is not None:
            for name, payload in items.read():
                last = name
                yield name, payload
                if name in END_EVENTS:
                    return
        if last not in END_EVENTS:
            yield settled(self.deps.pool, user_id, source_id, cancelled=wid in self._cancelled)

    def exists(self, user_id: str, source_id: str, gen: int) -> bool:
        return workflow_id(user_id, source_id, gen) in self._logs

    def start_later(self, job: ImportJob) -> None:
        raise RuntimeError("inline runs have no durable retries")

    def cancel(self, user_id: str, source_id: str) -> None:
        """Releases the followers; the work itself stops at its next step once the source is
        deleted (an inline run has no other handle)."""
        wid = workflow_id(user_id, source_id, current_gen(self.deps.pool, user_id, source_id))
        self._cancelled.add(wid)
        items = self._logs.get(wid)
        if items is not None:
            items.close()


# ---- DBOS -------------------------------------------------------------------------------------

_queues: dict[str, Any] = {}     # the DBOS queues of the launch they were registered on
_queue_owner: object = None


def source_queue(name: str = SOURCE_QUEUE):
    """A queue imports go through: one run at a time, globally (across users and processes;
    imports are heavy: OCR, vision, embedding, the GPU). Chat attachments have their own
    (`ASK_QUEUE`): the chat waits on them, and a book's import takes many minutes. So has a material
    with no page images (`TEXT_QUEUE`: a web page behind a book it does not share anything with). The
    GPU stays one at a time (`sources.gpu`). Registered right after the DBOS launch (`register_queue`); a
    launch that did not register them (tests) gets them here, once."""
    global _queue_owner
    from dbos import DBOS
    from dbos._dbos import _dbos_global_instance
    if _queue_owner is not _dbos_global_instance:
        _queues.clear()
        _queue_owner = _dbos_global_instance
    if name not in _queues:
        _queues[name] = DBOS.register_queue(name, global_concurrency=1)
    return _queues[name]


def register_queue() -> None:
    """Call right after the one DBOS launch: a restart's still-queued imports need their queues to
    be dispatched again."""
    source_queue(SOURCE_QUEUE)
    source_queue(ASK_QUEUE)
    source_queue(TEXT_QUEUE)


def queued_item(job_status: str | None) -> Item | None:
    """The `progress` item a follower gets first while the run waits in the queue (else None)."""
    if job_status != "ENQUEUED":
        return None
    return "progress", SourceProgress(stage="parse", done=0, total=0, message=MSG_QUEUED)


class DbosSourceRuns:
    """The service's runs; the deps come from `sources.flows.install` (recovery has no caller)."""

    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def _queue(self, job: ImportJob) -> str:
        if job.mode == "ask":
            return ASK_QUEUE
        with self.pool.connection() as conn:
            row = store.get_source(conn, job.user_id, job.source_id)
        return SOURCE_QUEUE if row is None or row.kind in ("pdf", "image") else TEXT_QUEUE

    def start(self, job: ImportJob) -> str:
        from dbos import SetWorkflowID

        from evowiki.agent import dbos_app
        from evowiki.sources.flows import source_workflow
        if not dbos_app.launched():     # fail closed: never import inline in the service
            raise RuntimeError("DBOS is not launched: source imports need the durable runtime")
        wid = workflow_id(job.user_id, job.source_id, job.gen)
        queue = source_queue(self._queue(job))

        def start() -> None:
            with SetWorkflowID(wid):    # an existing id returns its workflow, nothing reruns
                queue.enqueue(source_workflow, job.model_dump(mode="json"))
        # On a thread of its own, in an empty context: no DBOS context there, so the import is a
        # top-level workflow also when the caller runs inside a step (a durable assistant turn's
        # action executor, where DBOS refuses to start a workflow). The context is fresh on
        # purpose: a build whose threads inherit the caller's context would carry the step's.
        # A replayed caller gets the same id: no rerun.
        with ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(contextvars.Context().run, start).result()
        return wid

    def follow(self, user_id: str, source_id: str) -> Iterator[Item]:
        from dbos import DBOS
        from dbos._error import DBOSNonExistentWorkflowError

        from evowiki.sources.flows import STREAM_KEY
        give_up = time.monotonic() + START_WAIT_S
        while True:
            wid = workflow_id(user_id, source_id, current_gen(self.pool, user_id, source_id))
            try:
                waiting = queued_item(self._status(wid))
                if waiting is not None:    # a run in the queue has written nothing yet
                    yield waiting
                for raw in DBOS.read_stream(wid, STREAM_KEY, polling_interval_sec=0.1):
                    try:
                        name, payload = decode_item(raw)
                    except (ValueError, KeyError):   # an item this build cannot read
                        log.exception("source run %s: unreadable stream item", wid)
                        break                         # the end is told from the stored state
                    yield name, payload
                    if name in END_EVENTS:
                        return
            except DBOSNonExistentWorkflowError:
                # a route bumps the generation, then starts its run: wait for it a little
                if time.monotonic() < give_up and starting(self.pool, user_id, source_id):
                    time.sleep(0.1)
                    continue
            break
        yield settled(self.pool, user_id, source_id, cancelled=self._cancelled(wid))

    def exists(self, user_id: str, source_id: str, gen: int) -> bool:
        """Errors propagate: an unknown answer must not start a live run a second time."""
        from dbos import DBOS
        return DBOS.get_workflow_status(workflow_id(user_id, source_id, gen)) is not None

    def start_later(self, job: ImportJob) -> None:
        """Starts `evowiki.source_start` for the run (id `start:<run id>`: one per run, a second
        call returns the first), outside any DBOS context like `start`."""
        from dbos import DBOS, SetWorkflowID

        from evowiki.agent import dbos_app
        from evowiki.sources.flows import source_start_workflow
        if not dbos_app.launched():
            raise RuntimeError("DBOS is not launched: no durable retries")

        def go() -> None:
            with SetWorkflowID(start_workflow_id(job)):
                DBOS.start_workflow(source_start_workflow, job.model_dump(mode="json"))
        durable.outside_dbos(go)

    @staticmethod
    def _status(wid: str) -> str | None:
        """The workflow's DBOS status ("ENQUEUED", "PENDING", ...); None when unknown or unreadable
        (the follower then just reads the stream)."""
        from dbos import DBOS
        try:
            st = DBOS.get_workflow_status(wid)
        except Exception:
            log.warning("could not read the status of source run %s", wid, exc_info=True)
            return None
        return None if st is None else st.status

    @staticmethod
    def _cancelled(wid: str) -> bool:
        from dbos import DBOS
        try:
            st = DBOS.get_workflow_status(wid)
        except Exception:
            log.warning("could not read the status of source run %s", wid, exc_info=True)
            return False
        return st is not None and st.status == "CANCELLED"

    def cancel(self, user_id: str, source_id: str) -> None:
        from dbos import DBOS
        wid = workflow_id(user_id, source_id, current_gen(self.pool, user_id, source_id))
        try:
            st = DBOS.get_workflow_status(wid)
            if st is not None and st.status in ("PENDING", "ENQUEUED"):
                DBOS.cancel_workflow(wid)
        except Exception:      # the run still stops at its next step once the source is deleted
            log.warning("could not cancel source run %s", wid, exc_info=True)


class NoSourceRuns:
    """`Services.sources` until a runner is set: every use fails closed."""

    def start(self, job: ImportJob) -> str:
        raise RuntimeError("no source runner configured: set Services.sources")

    def follow(self, user_id: str, source_id: str) -> Iterator[Item]:
        raise RuntimeError("no source runner configured: set Services.sources")

    def cancel(self, user_id: str, source_id: str) -> None:
        raise RuntimeError("no source runner configured: set Services.sources")

    def exists(self, user_id: str, source_id: str, gen: int) -> bool:
        raise RuntimeError("no source runner configured: set Services.sources")

    def start_later(self, job: ImportJob) -> None:
        raise RuntimeError("no source runner configured: set Services.sources")
