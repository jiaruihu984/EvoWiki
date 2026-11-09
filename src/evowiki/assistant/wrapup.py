"""Durable background consolidation followed by cited anchor updates.

The database records pending revisions on answer/rating/compaction writes. A user-partitioned
DBOS queue drains them on foreground wakeups, startup and a periodic sweep. Published source
watermarks survive DBOS history cleanup. Closing session compactions are no longer produced;
in-turn compaction remains responsible for the active conversation's context.

Failures remain in the session log and retry with bounded backoff. Forgetting and background
pause stop publication; complete source provenance connects generated calls to deletion.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from dbos import DBOS, SetEnqueueOptions, SetWorkflowID
from dbos._error import DBOSException
from pydantic import BaseModel, ConfigDict

from evowiki.agent import durable
from evowiki.assistant.carryover import RECENT_SESSIONS, is_idle
from evowiki.assistant.session import Entry, SessionInfo, fold, session_version
from evowiki.llm.usage import Account, BudgetExceededError
from evowiki.memory.consolidate import unconsolidated
from evowiki.memory.forget import wrapup_workflow_prefix
from evowiki.store.dreaming import MAX_FAILED

log = logging.getLogger(__name__)

QUEUE = "assistant-wrapup"
RETRY_DELAYS_S = (60.0, 600.0, 3600.0)
MAX_SEGMENTS = 3              # consolidations of one run (each covers SEGMENT_TURNS_MAX turns)

Reason = Literal["budget", "failed", "postponed"]


class Job(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str
    session_id: str
    version: int
    attempt: int

    @property
    def workflow_id(self) -> str:
        return f"{wrapup_workflow_prefix(self.user_id)}{self.session_id}:{self.version}:{self.attempt}"


class Postponed(RuntimeError):
    """A forget request of this user is open: the delete goes first."""


class Wrapups(Protocol):
    def opened(self, info: SessionInfo) -> None:
        """A session event wakes this user's durable consolidation and anchor backlog."""
        ...

    def compacted(self, info: SessionInfo) -> None:
        """A turn of `info` compacted the session: consolidate what it folded."""
        ...


# ---- what a run does (each function is one step: reads and writes outside the turn's records) ---

def _account(deps: Any, user_id: str) -> Account | None:
    return Account(user_id=user_id, group="assistant") if deps.gateway.metered else None


def _attempts(entries: list[Entry], version: int) -> list[Entry]:
    return [e for e in entries if e.type == "wrapup_failed" and e.payload.get("version") == version]


def needs_wrapup(info: SessionInfo, entries: list[Entry], *, memory: bool) -> bool:
    """Whether an idle session still has unconsolidated turns."""
    if not is_idle(entries):
        return False
    state = fold(info, entries)
    return bool(memory and unconsolidated(entries, len(state.turns)))


def job_of(info: SessionInfo, entries: list[Entry], *, version: int | None = None) -> Job | None:
    """The run to ask for now; None: this version failed too often for anything but the budget."""
    version = session_version(entries) if version is None else version
    tried = _attempts(entries, version)
    if sum(e.payload.get("reason") == "failed" for e in tried) >= MAX_FAILED:
        return None
    return Job(user_id=info.user_id, session_id=info.session_id, version=version,
               attempt=len(tried))


def _summarise(deps: Any, job: Job) -> bool:
    """Update the stable anchor after consolidation; never append a closing compaction."""
    try:
        info = deps.store.info(job.session_id)
    except LookupError:
        return False
    mem = deps.memory
    if mem is not None and mem.store.hidden(job.user_id).paused:
        raise Postponed(job.user_id)
    if (mem is not None and mem.anchors is not None
            and not mem.anchors.update(info, gateway=deps.gateway,
                                       account=_account(deps, job.user_id), now=deps.clock())):
        raise Postponed(job.user_id)
    return True


def _consolidate(deps: Any, job: Job) -> bool:
    """One segment into memory. True: it may have left more."""
    mem = deps.memory
    if mem is None:
        return False
    if mem.store.hidden(job.user_id).paused:
        raise Postponed(job.user_id)
    try:
        info = deps.store.info(job.session_id)
    except LookupError:
        return False
    entries = deps.store.entries(job.session_id)
    state = fold(info, entries)
    through = len(state.turns) if is_idle(entries) else state.first_kept
    if through <= 0 or not unconsolidated(entries, through):
        return False
    mem.consolidate(state, gateway=deps.gateway, account=_account(deps, job.user_id),
                    first=state.first_kept, through=through,
                    language=deps.data.language(job.user_id))
    return True


def _conflicts(deps: Any, job: Job) -> None:
    mem = deps.memory
    if mem is None:
        return
    if mem.store.hidden(job.user_id).paused:
        raise Postponed(job.user_id)
    mem.check_conflicts(job.user_id, deps.gateway, _account(deps, job.user_id))


def _record(deps: Any, job: Job, reason: Reason, error: str) -> None:
    """The failure in the session's log; a session deleted meanwhile has no log to keep it."""
    try:
        deps.store.append(job.session_id, deps.new_id("ent"), "wrapup_failed",
                          {"version": job.version, "attempt": job.attempt, "reason": reason,
                           "error": error}, deps.clock())
    except LookupError:
        log.info("wrap-up of %s: the session is gone", job.session_id)


def run_job(deps: Any, job: Job, retry: Callable[[Job, float], None] | None = None) -> str:
    """One run: its stages as recorded steps. `retry(job, delay)` asks for the next attempt."""
    try:
        if deps.memory is not None and not deps.memory.background_allowed(job.user_id):
            raise Postponed(job.user_id)
        for _ in range(MAX_SEGMENTS):
            if not durable.step("wrapup.consolidate", lambda: _consolidate(deps, job)):
                break
        else:
            entries = deps.store.entries(job.session_id)
            state = fold(deps.store.info(job.session_id), entries)
            if unconsolidated(entries, len(state.turns)):
                raise Postponed('consolidation backlog')
        durable.step("wrapup.conflicts", lambda: _conflicts(deps, job))
        if not durable.step("wrapup.summarise", lambda: _summarise(deps, job)):
            return "gone"
    except DBOSException:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is recorded and tried again
        reason: Reason = ("postponed" if isinstance(exc, Postponed)
                          else "budget" if isinstance(exc, BudgetExceededError) else "failed")
        error = type(exc).__name__
        log.warning("wrap-up of %s (attempt %d) did not finish: %s: %s", job.session_id,
                    job.attempt, error, exc)
        durable.step("wrapup.failed", lambda: _record(deps, job, reason, error))
        if retry is not None and job.attempt < len(RETRY_DELAYS_S):
            durable.step("wrapup.retry", lambda: retry(
                job.model_copy(update={"attempt": job.attempt + 1}), RETRY_DELAYS_S[job.attempt]))
        return reason
    return "done"


def retry_pending(deps: Any, user_id: str) -> None:
    if deps.memory is not None:
        deps.memory.retry_pending(user_id)


def _opened(deps: Any, info: SessionInfo) -> list[Job]:
    """Read pending work after a session event; this selection makes no model call."""
    jobs = []
    mem = deps.memory
    if mem is not None and not mem.background_allowed(info.user_id):
        return jobs
    if mem is not None and mem.anchors is not None:
        for row in mem.anchors.pending(info.user_id):
            try:
                session = deps.store.info(row['session_id'])
            except LookupError:
                continue
            entries = deps.store.entries(session.session_id)
            if is_idle(entries):
                job = job_of(session, entries, version=row['revision'])
                if job is not None:
                    jobs.append(job)
        return jobs
    for session in deps.store.recent_sessions(info.user_id, limit=RECENT_SESSIONS,
                                             exclude=info.session_id):
        entries = deps.store.entries(session.session_id)
        if needs_wrapup(session, entries, memory=mem is not None):
            job = job_of(session, entries)
            if job is not None:
                jobs.append(job)
    return jobs


def _compacted(deps: Any, info: SessionInfo) -> Job | None:
    if deps.memory is not None and deps.memory.anchors is not None:
        rows = deps.memory.anchors.pending(info.user_id)
        row = next((r for r in rows if r['session_id'] == info.session_id), None)
        if row is None:
            return None
        return job_of(info, deps.store.entries(info.session_id), version=row['revision'])
    return job_of(info, deps.store.entries(info.session_id))


# ---- inline (tests, scripts: no DBOS): the same run, in the caller ------------------------------

@dataclass
class InlineWrapups:
    deps: Any

    def opened(self, info: SessionInfo) -> None:
        for job in _opened(self.deps, info):
            run_job(self.deps, job)
        retry_pending(self.deps, info.user_id)

    def compacted(self, info: SessionInfo) -> None:
        job = _compacted(self.deps, info)
        if job is not None:
            run_job(self.deps, job)


# ---- DBOS ------------------------------------------------------------------------------------

_queue: Any = None
_deps: Callable[[], Any] | None = None           # the turn's plain deps (recovery has no caller)
_forget_retry: Callable[[str], None] | None = None


def install(deps: Callable[[], Any], forget_retry: Callable[[str], None] | None = None) -> None:
    global _deps, _forget_retry
    _deps, _forget_retry = deps, forget_retry


def register() -> None:
    """After the DBOS launch: the queue (a restart's queued runs are dispatched again)."""
    global _queue
    _queue = DBOS.register_queue(QUEUE, partition_concurrency=1)
    DBOS.apply_schedules([{'schedule_name': 'memory:pending', 'workflow_fn': sweep_workflow,
                          'schedule': '* * * * *', 'context': None, 'automatic_backfill': False}])
    _wake_pending()


def uninstall() -> None:
    global _queue, _deps, _forget_retry
    _queue, _deps, _forget_retry = None, None, None


def _installed() -> Any:
    if _deps is None:
        raise RuntimeError("wrapup.install was not called: no deps for the wrap-up")
    return _deps()


def _enqueue(fn: Any, wid: str, user_id: str, *args: Any, delay: float | None = None) -> None:
    if _queue is None:
        raise RuntimeError("the assistant-wrapup queue is not registered")
    queue = _queue

    def go() -> None:
        with SetWorkflowID(wid), SetEnqueueOptions(queue_partition_key=user_id,
                                                   delay_seconds=delay):
            queue.enqueue(fn, *args)
    # may run inside a turn's step: a workflow cannot be started from there, and these are roots
    durable.outside_dbos(go)


def _enqueue_job(job: Job, delay: float | None = None) -> None:
    _enqueue(wrapup_workflow, job.workflow_id, job.user_id, job.model_dump(), delay=delay)


@DBOS.workflow(name="evowiki.assistant.wrapup")
def wrapup_workflow(raw: dict[str, Any]) -> str:
    job = Job.model_validate(raw)
    deps = _installed()
    out = run_job(deps, job, retry=lambda j, delay: _enqueue_job(j, delay))
    # a delete that gave up waiting for this run goes on now (a workflow of its own: this one is
    # still active until it returns, and it would wait for itself)
    if durable.step("wrapup.forget_open", lambda: bool(deps.memory is not None
                                                       and deps.memory.store.hidden(job.user_id).paused)):
        durable.step("wrapup.forget_retry", lambda: _enqueue(
            forget_retry_workflow, f"wrapup_forget:{job.workflow_id}", job.user_id, job.user_id))
    return out


@DBOS.workflow(name="evowiki.assistant.wrapup_forget")
def forget_retry_workflow(user_id: str) -> None:
    if _forget_retry is not None:
        durable.step("wrapup.forget_run", lambda: _forget_retry(user_id))


@DBOS.workflow(name="evowiki.assistant.wrapup_pending")
def pending_workflow(user_id: str) -> None:
    deps = _installed()
    durable.step("wrapup.pending", lambda: retry_pending(deps, user_id))


@dataclass
class DbosWrapups:
    """`deps`: the plain (not durable) turn deps; what is read here stays out of the turn's
    records, so a session's words never land in the workflow of another session."""

    deps: Callable[[], Any]

    def opened(self, info: SessionInfo) -> None:
        durable.step("assistant.wrapup.opened", lambda: self._opened(info))

    def compacted(self, info: SessionInfo) -> None:
        durable.step("assistant.wrapup.compacted", lambda: self._compacted(info))

    def _opened(self, info: SessionInfo) -> None:
        """Never fails the turn: the next new session asks again."""
        try:
            for job in _opened(self.deps(), info):
                _enqueue_job(job)
            _enqueue(pending_workflow, f"{wrapup_workflow_prefix(info.user_id)}pending:"
                     f"{info.session_id}", info.user_id, info.user_id)
        except Exception:
            log.exception("could not ask for the wrap-up of %s's sessions", info.user_id)

    def _compacted(self, info: SessionInfo) -> None:
        try:
            job = _compacted(self.deps(), info)
            if job is not None:
                _enqueue_job(job)
        except Exception:
            log.exception("could not ask for the consolidation of %s", info.session_id)


@DBOS.workflow(name="evowiki.assistant.memory_sweep")
def sweep_workflow(scheduled_at: Any, context: Any = None) -> None:
    durable.step('memory.sweep', _wake_pending)


def _wake_pending() -> None:
    if _deps is None:
        return
    deps = _installed()
    if deps.memory is None or deps.memory.anchors is None:
        return
    from evowiki.store.db import fetch_all
    with deps.memory.anchors.pool.connection() as conn:
        users = fetch_all(conn, "select distinct user_id from dream_pending where revision>processed")
    for row in users:
        if not deps.memory.background_allowed(row['user_id']):
            continue
        for pending in deps.memory.anchors.pending(row['user_id']):
            try:
                info = deps.store.info(pending['session_id'])
            except LookupError:
                continue
            entries = deps.store.entries(info.session_id)
            job = job_of(info, entries, version=pending['revision'])
            if job is not None and is_idle(entries):
                _enqueue_job(job)
