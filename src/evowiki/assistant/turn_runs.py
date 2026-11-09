"""A reading-assistant turn as a DBOS workflow (B1c, spec 2026-10-14 §1-§2).

`turn_workflow` runs `loop.run_turn` unchanged over `durable_turn_deps`: the model calls, reads,
tool runs and session-log writes are recorded steps, so a recovered turn replays to where it
stopped and continues. Its events are the workflow's stream (written from the body only); the
HTTP response is a follower, so a client that leaves does not cancel the turn.

`TurnRuns` sits under `AssistantService.send`:
- `InlineTurnRuns`: today's behaviour (the turn is the caller's generator; tests and scripts).
- `DbosTurnRuns`: start the workflow `turn:<session>:<message>` and follow its stream.

`turn_workflow` is registered by the decorator when this module is imported: import it before
`dbos_app.launch` or DBOS cannot find it at recovery. Its deps come from `install` (recovery has
no caller).

Live text (spec 2026-10-15 §3): the model's tokens are produced inside a step, so they go to the
in-process `live.BUS` instead of the stream; the body writes each call's committed text to the
stream after the step. `Follower` merges both: stream events as they are (its `cursor` = stream
events read), live text of the call in flight as `TextDelta(at=...)` while `cursor` equals the
stream position that call's committed text will take. A client that lost the connection follows
again from its cursor (`DbosTurnRuns.resume`).

A question to the learner (ask_user, any pausing tool) ends the turn like an answer: the workflow
returns, nothing waits and the session is free. His next message is a new turn (its own
workflow) that resumes the paused run (assistant.loop).

Stop (`AssistantService.stop`) never cancels the workflow: a cancelled workflow can write neither its
log nor its stream, so what was shown would be lost and the follower would see the stream break.
The stop is a row (`SessionStore.request_stop`) plus `LiveBus.stop`; the body reads it and ends the
turn itself (assistant.loop). A DBOS-cancelled turn (`sweep_foreign_turns`) never reaches the
loop's `cancelled` turn_error either: that path is a closed generator, which is the inline runner only."""

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Protocol

from dbos import DBOS, SetWorkflowID
from dbos._error import DBOSException
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from evowiki.assistant.contracts import AssistantEvent, ErrorEv, MessageIn, StatusLine, TextDelta
from evowiki.assistant.durable import durable_turn_deps
from evowiki.assistant.live import BUS, LiveBus
from evowiki.assistant.loop import TurnDeps, run_turn

if TYPE_CHECKING:
    from evowiki.assistant.service import AssistantService

log = logging.getLogger("evowiki.assistant.turn_runs")

STREAM_KEY = "events"
END_TYPES = frozenset({"done", "error"})
LIVE_MIN_INTERVAL_S = 0.05     # live text is handed out at most this often (coalesced between)
READ_RETRIES = 3               # transient failures in a row (a full connection pool) before the follower gives up
READ_WAIT_S = 1.0              # the stream reader re-checks whether its follower left this often
_EVENT = TypeAdapter(AssistantEvent)


class TurnJob(BaseModel):
    """The workflow's only argument (JSON)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    user_id: str
    message_id: str
    msg: MessageIn

    @property
    def workflow_id(self) -> str:
        return f"turn:{self.session_id}:{self.message_id}"


def encode_event(ev: AssistantEvent) -> dict:
    return ev.model_dump(mode="json")


def decode_event(d: object) -> AssistantEvent:
    try:
        return _EVENT.validate_python(d)
    except ValidationError as exc:
        raise ValueError(f"unreadable assistant event: {exc.error_count()} errors") from exc


class AssistantUnavailable(RuntimeError):
    """The durable runtime this turn needs is not running (HTTP 503)."""


def _ended_early() -> ErrorEv:
    return ErrorEv(code="internal", retryable=True, detail="turn ended without done")


def _internal(exc: BaseException) -> ErrorEv:
    return ErrorEv(code="internal", retryable=True, detail=f"{type(exc).__name__}: {exc}"[:500])


# ---- installed deps (DBOS recovery has no caller) -------------------------------------------

_service: "AssistantService | None" = None


def install(service: "AssistantService") -> None:
    global _service
    _service = service


def uninstall() -> None:
    global _service
    _service = None


def installed() -> "AssistantService":
    if _service is None:
        raise RuntimeError("turn_runs.install(service) was not called: no deps for the turn")
    return _service


# ---- the workflow ---------------------------------------------------------------------------

def execute(job: TurnJob, deps: TurnDeps, emit: Callable[[AssistantEvent], None],
            bus: LiveBus | None = None) -> str:
    """The body: the turn's events go to `emit`; the last one is always `done` or `error`.
    Returns that last event's type. `bus`: where model calls stream their live text (every event
    of `run_turn` is emitted, so its event positions are the stream's)."""
    last = ""
    wid = job.workflow_id
    try:
        info = deps.store.info(job.session_id)           # LookupError -> error event below
        live = bus.source(wid) if bus is not None else None
        progress = (lambda text: bus.status(wid, text)) if bus is not None else None
        stop = (lambda: bus.stopped(wid)) if bus is not None else None
        for ev in run_turn(deps, info, job.msg, message_id=job.message_id, live=live,
                           progress=progress, stop=stop):
            if bus is not None:
                bus.status(wid, "")      # a tool's step line holds only until its next event
            emit(ev)
            last = ev.type
    except Exception as exc:        # fail closed: the follower gets an error event
        if isinstance(exc, DBOSException):
            raise                   # DBOS control flow / nondeterminism: never masked as a turn error
        log.exception("assistant turn %s crashed", job.workflow_id)
        if last not in END_TYPES:
            err = _internal(exc)
            emit(err)
            last = err.type
    finally:
        if bus is not None:
            bus.end(job.workflow_id)
    if last not in END_TYPES:
        err = _ended_early()
        emit(err)
        last = err.type
    return last


@DBOS.workflow(name="evowiki.assistant_turn")
def turn_workflow(job: dict) -> str:
    tj = TurnJob.model_validate(job)
    try:
        deps = durable_turn_deps(installed().turn_deps)
        return execute(tj, deps, lambda ev: DBOS.write_stream(STREAM_KEY, encode_event(ev)),
                       BUS)
    finally:
        DBOS.close_stream(STREAM_KEY)


# ---- runners --------------------------------------------------------------------------------

class TurnRuns(Protocol):
    def start(self, job: TurnJob) -> None: ...

    def follow(self, user_id: str, job: TurnJob) -> Iterator[AssistantEvent]: ...

    def busy(self, session_id: str) -> bool: ...

    def user_busy(self, user_id: str) -> bool:
        """Any turn of this user is running (the Bot's idle page writing waits for it)."""
        ...

    def running(self, session_id: str) -> str | None:
        """The message id of the session's running turn; None: none."""
        ...

    def stop_now(self, session_id: str, message_id: str) -> None:
        """Cuts that turn's model call in flight without waiting for its next poll."""
        ...


class InlineTurnRuns:
    """Today's behaviour: the turn is the caller's generator (a disconnect closes it); the
    service's in-process lock is the only busy rule."""

    def __init__(self, deps: TurnDeps):
        self._deps = deps

    def start(self, job: TurnJob) -> None:
        return None

    def follow(self, user_id: str, job: TurnJob) -> Iterator[AssistantEvent]:
        info = self._deps.store.info(job.session_id)
        return run_turn(self._deps, info, job.msg, message_id=job.message_id)

    def busy(self, session_id: str) -> bool:
        return False

    def user_busy(self, user_id: str) -> bool:
        return False

    def running(self, session_id: str) -> str | None:
        return None

    def stop_now(self, session_id: str, message_id: str) -> None:
        return None


def _app_version() -> str:
    from dbos._utils import GlobalParams  # the same source B1b's `_unfinishable` reads
    return GlobalParams.app_version


def _await_settled(wid: str, timeout: float = 10.0, poll: float = 0.02) -> None:
    """Bounded wait for the workflow to leave PENDING / ENQUEUED (after its stream closed it is
    about to return). On timeout it logs and goes on: the stream already ended."""
    end = time.monotonic() + timeout
    while True:
        st = DBOS.get_workflow_status(wid)
        if st is None or st.status not in ("PENDING", "ENQUEUED"):
            return
        if time.monotonic() >= end:
            log.warning("assistant turn %s still %s after its end event", wid, st.status)
            return
        time.sleep(poll)


def sweep_foreign_turns() -> int:
    """After launch: cancels PENDING / ENQUEUED `turn:` workflows of another app version (nobody
    recovers them; they would keep their session busy forever). Returns how many. The listing runs
    in a worker thread so a caller on an event-loop thread works too (DBOS refuses its sync API
    there); the app lifespan already runs the whole durable start off the loop."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_sweep_foreign_turns).result()


def _sweep_foreign_turns() -> int:
    n = 0
    for w in DBOS.list_workflows(workflow_id_prefix="turn:", status=["PENDING", "ENQUEUED"],
                                 load_input=False, load_output=False):
        if w.app_version != _app_version():
            DBOS.cancel_workflow(w.workflow_id)
            log.warning("assistant turn %s (app version %s) can never finish: cancelled",
                        w.workflow_id, w.app_version)
            n += 1
    return n


def _require_launched() -> None:
    from evowiki.agent import dbos_app
    if not dbos_app.launched():     # fail closed: never run a turn inline in the service
        raise AssistantUnavailable("DBOS is not launched: assistant turns need the durable runtime")


class DbosTurnRuns:
    """The workflow always runs over the *installed* service's deps (recovery has no caller), so
    the service that sends must be that one: `AssistantService` binds itself here, `start`
    refuses (fail closed) when another service is installed."""

    def __init__(self) -> None:
        self._service: AssistantService | None = None

    def bind(self, service: "AssistantService") -> None:
        self._service = service

    def start(self, job: TurnJob) -> None:
        _require_launched()
        if self._service is None or _service is not self._service:
            raise RuntimeError("the installed assistant service is not the one sending this turn "
                               "(DurableRuntime.start installs it before launch)")
        with SetWorkflowID(job.workflow_id):
            DBOS.start_workflow(turn_workflow, job.model_dump(mode="json"))

    def follow(self, user_id: str, job: TurnJob) -> "Follower":
        return self._follow(job.workflow_id)

    @staticmethod
    def resume(workflow_id: str, after: int) -> "Follower":
        """Follow a turn again from stream position `after` (the cursor the client last saw).
        LookupError when there is no such turn (a turn of another session is never named: the
        workflow id carries the session)."""
        _require_launched()
        if DBOS.get_workflow_status(workflow_id) is None:
            raise LookupError(f"no assistant turn {workflow_id}")
        return Follower(workflow_id, after=after)

    @staticmethod
    def _follow(wid: str, after: int = 0) -> "Follower":
        return Follower(wid, after=after)

    def busy(self, session_id: str) -> bool:
        """A PENDING / ENQUEUED turn of *this app version*: recovery only picks up its own
        version's workflows, so another version's pending turn would block the session forever
        (`sweep_foreign_turns` cancels those at launch)."""
        return self.running(session_id) is not None

    def running(self, session_id: str) -> str | None:
        _require_launched()
        prefix = f"turn:{session_id}:"
        hits = DBOS.list_workflows(workflow_id_prefix=prefix, status=["PENDING", "ENQUEUED"],
                                   app_version=_app_version(),
                                   load_input=False, load_output=False, limit=1)
        return hits[0].workflow_id.removeprefix(prefix) if hits else None

    def stop_now(self, session_id: str, message_id: str) -> None:
        BUS.stop(f"turn:{session_id}:{message_id}")

    def user_busy(self, user_id: str) -> bool:
        """A PENDING / ENQUEUED turn of this app version whose job is the user's."""
        _require_launched()
        for st in DBOS.list_workflows(workflow_id_prefix="turn:", status=["PENDING", "ENQUEUED"],
                                      app_version=_app_version(), load_input=True,
                                      load_output=False):
            args = (st.input or {}).get("args") or ()
            if args and isinstance(args[0], dict) and args[0].get("user_id") == user_id:
                return True
        return False


# ---- the follower ---------------------------------------------------------------------------

_POKE = object()


class Follower:
    """The events of one turn for one client: the stream from `after` on, merged with the live
    text of the model call in flight (module doc). `cursor` = stream events read so far (live text
    does not move it): a client that comes back resumes from there and loses nothing - a live
    piece it saw is resent with its committed text at the same `at`."""

    def __init__(self, wid: str, *, after: int = 0, bus: LiveBus = BUS,
                 min_interval: float = LIVE_MIN_INTERVAL_S):
        self.workflow_id = wid
        self.cursor = after
        self._bus = bus
        self._min_interval = min_interval
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._stop = threading.Event()
        self._sent: tuple[int, int] | None = None   # (base_index, chars handed out)
        self._status_seq = 0             # the LiveBus status last handed out (0: none)
        self._unsub: Callable[[], None] | None = None
        self._gen = self._run()

    def __iter__(self) -> "Follower":
        return self

    def __next__(self) -> AssistantEvent:
        return next(self._gen)

    def close(self) -> None:
        self._gen.close()
        self._leave()

    def _leave(self) -> None:
        self._stop.set()
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _read(self) -> None:
        """Reader thread: the stream from the cursor into the queue (no event loop here)."""
        from dbos._error import DBOSStreamTimeoutError
        i, failed = self.cursor, 0
        while not self._stop.is_set():
            try:
                for raw in DBOS.read_stream(self.workflow_id, STREAM_KEY, offset=i,
                                            polling_interval_sec=0.1,
                                            timeout_seconds=READ_WAIT_S):
                    self._q.put(("event", raw))
                    i += 1
                    failed = 0
                    if self._stop.is_set():
                        return
            except DBOSStreamTimeoutError:
                continue                    # nothing new: check whether we are still wanted
            except Exception as exc:  # noqa: BLE001 - the turn runs on; past the retries: an error event
                failed += 1
                if failed > READ_RETRIES:
                    self._q.put(("fail", exc))
                    return
                log.warning("assistant turn %s: reading its stream failed (%d), reading on",
                            self.workflow_id, failed)
                self._stop.wait(1.0)
                continue
            self._q.put(("end", None))
            return

    def _status(self) -> StatusLine | None:
        """A long tool's step line (LiveBus.status), once per change; none when cleared."""
        seq, text = self._bus.status_of(self.workflow_id)
        if seq == self._status_seq:
            return None
        self._status_seq = seq
        return StatusLine(text=text[:200]) if text.strip() else None

    def _live(self) -> TextDelta | None:
        snap = self._bus.snapshot(self.workflow_id)
        if snap is None or snap.base_index != self.cursor:
            return None                         # no call in flight, or not at this position yet
        done = self._sent[1] if self._sent is not None and self._sent[0] == snap.base_index else 0
        if len(snap.text) <= done:
            return None
        self._sent = (snap.base_index, len(snap.text))
        return TextDelta(at=snap.base_at + done, text=snap.text[done:])

    def _run(self) -> Iterator[AssistantEvent]:
        wid = self.workflow_id
        self._unsub = self._bus.subscribe(wid, lambda: self._q.put(("live", None)))
        threading.Thread(target=self._read, name="evowiki-turn-follow", daemon=True).start()
        last_live = 0.0
        due = True                              # live text due (a call may be streaming already)
        try:
            while True:
                wait = None
                if due:
                    wait = max(0.0, last_live + self._min_interval - time.monotonic())
                try:
                    kind, item = self._q.get(timeout=wait) if wait is not None else self._q.get()
                except queue.Empty:
                    kind, item = "live", None
                if kind == "live":
                    if time.monotonic() - last_live < self._min_interval:
                        due = True
                        continue
                    due = False
                    st = self._status()
                    if st is not None:
                        yield st
                    ev = self._live()
                    if ev is not None:
                        last_live = time.monotonic()
                        yield ev
                    continue
                if kind == "fail":
                    log.error("assistant turn %s: reading its stream failed", wid,
                              exc_info=item)
                    yield _ended_early()
                    return
                if kind == "end":
                    yield _ended_early()
                    return
                try:
                    ev = decode_event(item)
                except ValueError:
                    log.exception("assistant turn %s: unreadable stream event", wid)
                    yield _ended_early()
                    return
                self.cursor += 1
                if ev.type in END_TYPES:
                    # the end event is written before the workflow returns: let it leave PENDING
                    # first, or the next send (the in-process lock is released when this
                    # generator ends) would find the session "busy" for a moment
                    _await_settled(wid)
                    yield ev
                    return
                yield ev
                due = True                      # a call may already be streaming at the new cursor
        finally:
            self._leave()
