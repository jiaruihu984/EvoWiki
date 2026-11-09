"""A wall-clock bound for one blocking provider request: a total deadline, or for a streamed
request a no-progress watchdog.

The SDKs' `timeout=` is an httpx per-phase timeout (connect / read / write / pool): every byte
received restarts the read clock, so a server that trickles keep-alive bytes (DeepSeek sends
blank lines while it is still working) keeps a request alive far past the configured value.
`bounded_sdk_call` runs the request in a daemon worker thread and waits at most `seconds` for
it. On expiry it aborts the request (closes the response and the per-call HTTP client, so the
worker's socket read fails and the thread ends) and raises `DeadlineExceeded`.

No signals (Windows), no SDK internals: the abort goes through the public
`with_options(http_client=...)` copy of the SDK client and httpx event hooks. A client without
`with_options` (test fakes) still gets the caller-side deadline; only the worker clean-up is
then up to the call itself.

Worst case for the caller: `seconds + ABORT_GRACE_S`. The aborted worker normally ends within
milliseconds (socket closed under it); if the abort lands while a connection is still being
opened it ends at the latest after one more per-phase timeout (the SDK `timeout=`, which
callers set to the same value), so it never lives forever.

A streamed request is bounded by progress instead (`Progress`, `call_with_watchdog`): it is aborted
only when `seconds` pass with no stream event, before the first one (`NoProgress.started` False:
the request sat queued; a queued DeepSeek request gets only SSE keep-alive comments, which the
SDKs drop) or between two (True: it stalled). A long answer that keeps streaming is never cut.

Cancellation (「停止」): `cancel_scope(check)` names a predicate that says whether the run the
request belongs to was cancelled (the gateway's durable step: this DBOS workflow's status; the agent
loop: its spec's `cancelled`). While a request runs in a scope, the caller-side wait looks at it
every POLL_S and a stream reader between two chunks (`cancel_requested`); once it says yes the
request is aborted like an expired one (its response and HTTP client closed, so the stream stops
being read) and `RequestCancelled` is raised. That is not a provider failure: nothing retries it or
switches to a fallback writer on it, and it carries the usage the stream already reported, if any."""

import contextvars
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

log = logging.getLogger("evowiki.llm")
WORKER_NAME = "evowiki-llm-deadline"
ABORT_GRACE_S = 2.0   # how long the caller waits for an aborted worker to end before raising
POLL_S = 0.25         # how often the watchdog looks at a streamed request's progress

Hooks = dict[str, list[Callable[[Any], None]]]
# (timeout seconds, httpx event hooks) -> an SDK-compatible httpx client used for ONE request
HttpClientFactory = Callable[[float, Hooks], Any]


class DeadlineExceeded(Exception):
    """The request was still running at its total deadline and was aborted."""

    def __init__(self, seconds: float):
        super().__init__(f"exceeded the total deadline of {seconds:g}s")
        self.seconds = seconds


class NoProgress(DeadlineExceeded):
    """A streamed request had no stream event for `seconds` and was aborted: `started` False when
    not one arrived (it sat queued), True when it stalled after some."""

    def __init__(self, seconds: float, *, started: bool):
        Exception.__init__(self, (f"stalled: no stream event for {seconds:g}s" if started else
                                  f"queued: no first stream event within {seconds:g}s"))
        self.seconds = seconds
        self.started = started


class RequestCancelled(Exception):
    """The run this request belongs to was cancelled while it was in flight; the request was
    aborted (module doc). Not a provider failure: never retried, never a fallback. The token fields
    hold what the stream had reported when it was cut (None: nothing reported, e.g. DeepSeek, whose
    usage arrives only in its last chunk)."""

    def __init__(self, message: str = "request cancelled: the run was stopped", *,
                 model_version: str | None = None, input_tokens: int | None = None,
                 output_tokens: int | None = None, cache_read_tokens: int | None = None,
                 cache_write_tokens: int | None = None,
                 cache_write_1h_tokens: int | None = None):
        super().__init__(message)
        self.model_version = model_version
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_write_tokens = cache_write_tokens
        self.cache_write_1h_tokens = cache_write_1h_tokens

    @property
    def reported(self) -> bool:
        """The provider reported usage before the cut (it can be billed)."""
        return self.input_tokens is not None or self.output_tokens is not None


_cancel_checks: ContextVar[tuple[Callable[[], bool], ...]] = ContextVar(
    "llm_cancel_checks", default=())


@contextmanager
def cancel_scope(check: Callable[[], bool] | None) -> Iterator[None]:
    """Requests made inside are aborted once `check()` is true (module doc); scopes nest (any
    check that says yes cancels). None: no new check."""
    if check is None:
        yield
        return
    token = _cancel_checks.set((*_cancel_checks.get(), check))
    try:
        yield
    finally:
        _cancel_checks.reset(token)


def cancel_requested() -> bool:
    """Whether a check of the current scope says the run was cancelled. A check that raises is
    logged and counts as no (the run's own next step still sees a cancellation)."""
    for check in _cancel_checks.get():
        try:
            if check():
                return True
        except Exception:
            log.warning("cancel check failed", exc_info=True)
    return False


def cancellable_sleep(seconds: float) -> None:
    """Wait `seconds`; a cancelled run raises RequestCancelled at the next poll instead."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        if cancel_requested():
            raise RequestCancelled("cancelled")
        time.sleep(min(left, POLL_S))


def _cancellable() -> bool:
    return bool(_cancel_checks.get())


class BackgroundPoll:
    """A cancel check whose probe is slow or may block (a database read): `__call__` never
    blocks; it answers with the last probe's result and starts a new probe on a daemon thread
    when none is running and `every` seconds have passed since the last one began. Once the probe
    said yes the answer stays yes."""

    def __init__(self, probe: Callable[[], bool], every: float,
                 clock: Callable[[], float] = time.monotonic):
        self._probe, self._every, self._clock = probe, every, clock
        self._lock = threading.Lock()
        self._yes = False
        self._running = False
        self._began: float | None = None

    def __call__(self) -> bool:
        with self._lock:
            if self._yes:
                return True
            now = self._clock()
            if not self._running and (self._began is None or now - self._began >= self._every):
                self._running, self._began = True, now
                threading.Thread(target=self._run, name="evowiki-cancel-poll",
                                 daemon=True).start()
            return self._yes

    def _run(self) -> None:
        try:
            yes = bool(self._probe())
        except Exception:
            log.warning("cancel probe failed", exc_info=True)
            yes = False
        with self._lock:
            self._yes = self._yes or yes
            self._running = False


class Progress:
    """Liveness of one streamed request: its reader calls beat() for every stream event that is
    work (text, reasoning, tool arguments, any data chunk; not a keep-alive or a ping)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._last: float | None = None

    def beat(self) -> None:
        self._last = self.clock()

    @property
    def last(self) -> float | None:
        return self._last


class AbortSwitch:
    """Per-request abort: runs every registered closer once and makes the request's HTTP client
    refuse to go on (its request hook raises; a response arriving after the abort is closed)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._aborted = False
        self._closers: list[Callable[[], None]] = []

    @property
    def aborted(self) -> bool:
        return self._aborted

    def on_abort(self, close: Callable[[], None]) -> None:
        with self._lock:
            if not self._aborted:
                self._closers.append(close)
                return
        close()
        raise DeadlineExceeded(0)

    def event_hooks(self) -> Hooks:
        def on_request(request: Any) -> None:
            if self._aborted:
                raise DeadlineExceeded(0)

        def on_response(response: Any) -> None:
            self.on_abort(response.close)   # closing it mid-body closes its socket

        return {"request": [on_request], "response": [on_response]}

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            closers, self._closers = self._closers, []
        for close in closers:
            try:
                close()
            except Exception:  # best effort: the caller's deadline already holds
                log.debug("closing an aborted request failed", exc_info=True)


def call_with_deadline[T](fn: Callable[[], T], seconds: float, abort: Callable[[], None], *,
                          grace: float = ABORT_GRACE_S) -> T:
    """fn() in a daemon worker thread (contextvars copied). Its result or exception when it ends
    within `seconds`; otherwise abort(), wait up to `grace` for the worker, raise
    DeadlineExceeded. A result that lands before the expiry is decided (under a lock) is kept,
    so a paid response is never dropped by that race. In a `cancel_scope` the wait looks at the
    cancellation every POLL_S: once cancelled, abort() and raise RequestCancelled."""
    _positive(seconds)
    if not _cancellable():
        return _supervised(fn, abort, grace,
                           lambda done: None if done.wait(seconds) else DeadlineExceeded(seconds))
    end = time.monotonic() + seconds

    def expired(done: threading.Event) -> Exception | None:
        while True:
            if cancel_requested():
                return RequestCancelled()
            left = end - time.monotonic()
            if left <= 0:
                return DeadlineExceeded(seconds)
            if done.wait(min(left, POLL_S)):
                return None

    return _supervised(fn, abort, grace, expired)


def call_with_watchdog[T](fn: Callable[[], T], seconds: float, abort: Callable[[], None],
                          progress: Progress, *, grace: float = ABORT_GRACE_S,
                          poll: float = POLL_S) -> T:
    """As call_with_deadline, but the bound is `seconds` with no progress: from the start until
    the first beat, then from each beat to the next. Past it: abort and raise NoProgress. A
    cancellation (`cancel_requested`, looked at every `poll`) aborts and raises RequestCancelled."""
    _positive(seconds)
    clock = progress.clock
    start = clock()          # before the worker starts

    def expired(done: threading.Event) -> Exception | None:
        while True:
            if cancel_requested():
                return RequestCancelled()
            last = progress.last
            left = (start if last is None else last) + seconds - clock()
            if left <= 0:
                return NoProgress(seconds, started=last is not None)
            if done.wait(min(left, poll)):
                return None

    return _supervised(fn, abort, grace, expired)


def _positive(seconds: float) -> None:
    if not seconds > 0:
        raise ValueError(f"deadline must be > 0 seconds, got {seconds!r}")


_reply_started: ContextVar[Callable[[], None] | None] = ContextVar("llm_reply_started", default=None)


@contextmanager
def on_reply_started(fn: Callable[[], None]) -> Iterator[None]:
    """Within the block, `fn` is told when a streamed call's reasoning ends and its answer starts.
    Only a thinking DeepSeek call streams its JSON answer; the others never say."""
    token = _reply_started.set(fn)
    try:
        yield
    finally:
        _reply_started.reset(token)


def reply_started() -> None:
    fn = _reply_started.get()
    if fn is not None:
        fn()


def _supervised[T](fn: Callable[[], T], abort: Callable[[], None], grace: float,
                   expired: Callable[[threading.Event], Exception | None]) -> T:
    """fn() in the worker; `expired(done)` waits and says whether (and how) it ran out."""
    lock = threading.Lock()
    outcome: list[tuple[bool, Any]] = []
    gave_up = [False]
    done = threading.Event()
    ctx = contextvars.copy_context()

    def work() -> None:
        try:
            res: tuple[bool, Any] = (True, ctx.run(fn))
        except BaseException as exc:  # noqa: BLE001 - handed to the caller below
            res = (False, exc)
        with lock:
            if not gave_up[0]:
                outcome.append(res)
        done.set()

    worker = threading.Thread(target=work, name=WORKER_NAME, daemon=True)
    worker.start()
    why = expired(done)
    with lock:
        if not outcome:
            gave_up[0] = True
    if gave_up[0]:
        abort()
        worker.join(grace)
        raise why if why is not None else DeadlineExceeded(0)
    ok, value = outcome[0]
    if ok:
        return value
    raise value


def bounded_sdk_call[T](client: Any, call: Callable[[Any], T], seconds: float,
                        http_client_factory: HttpClientFactory | None,
                        progress: Progress | None = None) -> T:
    """call(sdk_client) under a total deadline of `seconds`, or, with `progress` (a streamed
    request whose reader beats it), under the no-progress watchdog of `seconds`. With a factory
    and an SDK client that has `with_options`, the request runs on its own HTTP client with SDK
    retries off (max_retries=0: one HTTP attempt per call); the abort closes that client, and it
    is closed after every call."""
    switch = AbortSwitch()
    http = None
    bound = client
    if http_client_factory is not None and hasattr(client, "with_options"):
        http = http_client_factory(seconds, switch.event_hooks())
        switch.on_abort(http.close)
        bound = client.with_options(http_client=http, max_retries=0)
    try:
        if progress is None:
            return call_with_deadline(lambda: call(bound), seconds, switch.abort)
        return call_with_watchdog(lambda: call(bound), seconds, switch.abort, progress)
    finally:
        if http is not None and not switch.aborted:
            http.close()
