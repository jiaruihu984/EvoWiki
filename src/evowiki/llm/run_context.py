"""The traced run a model call belongs to (observability plan 2026-10-29 §5 1a, ruling 2).

Every product step runs inside a run: an `agent_run` row, reused for single JSON calls and chat
calls as well as agent loops (no parallel table). `traced_step` opens one for a step (re-exported
by agent/trace.py); an agent loop binds the run it already traces (`bind_run`). While a run is
current, the gateway appends every `call` and `converse` to it as a step (`record_call`,
`record_chat`), stamps the run id on the llm_call rows it writes, and keys a call without an
account by the run's user. A call with a user that arrives outside any run gets a run of its own
(`auto_run`): no model call of a user is untraced, whether or not its caller opened a step.

Rejections after parsing (`reject`): a reply the step refused (SplitRefused, a repair, an unusable
compaction reply, a re-ask, a rule-guard veto) is a 'reject' step with its reason and the refused
call's id and output hash; the run's `stop` says so when it ended on one. Ruling 3: a rejected or
unsaved candidate stays here, never in `artifact`.

This module sits in `evowiki.llm` and imports nothing from `evowiki.agent`: the frozen experiment
harnesses import the gateway, and their import graph is pinned (the durable workflow module is
looked up in `sys.modules`, like the gateway's `_workflow_durable`).

Step numbers come from the run (`Run.next_seq`), shared by an agent loop's own steps and the
gateway's, so they never collide. A step the gateway appends for a call with an id is unique per
(run, call id) (migration 0090): a durable workflow that re-executes its body appends nothing twice.

Where steps go (`StepStore`): the store `configure` installed (the service's Postgres trace sink),
none otherwise: without a store nothing opens a run and `current_run_id()` is None, so offline
tools and tests that never configure one are unchanged."""

import functools
import hashlib
import inspect
import logging
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Protocol, TypeVar

from evowiki.llm.chat import ChatRequest, ChatTurn
from evowiki.llm.models import LLMCall, LLMRequest
from evowiki.llm.prompt_steps import step_of

F = TypeVar("F", bound=Callable[..., Any])
COMMIT_ENV = "EVOWIKI_COMMIT"
STEP_AGENT = "step"         # agent_run.agent of a traced step that is not an agent loop

log = logging.getLogger(__name__)


class StepStore(Protocol):
    """What a traced run writes (store/trace.py PgTraceSink). Every call may repeat with the same
    arguments (a durable replay re-executes the workflow body); a repeat changes nothing."""

    def start(self, run_id: str, *, user_id: str, agent: str, subject: str, pack_version: int,
              input: dict, step_key: str | None = None) -> None: ...

    def chat(self, run_id: str, seq: int, request: ChatRequest, turn: ChatTurn | None,
             call_id: str | None, error: str | None) -> None: ...

    def call(self, run_id: str, seq: int, request: LLMRequest, schema_name: str,
             call: LLMCall | None, error: str | None) -> None: ...

    def reject(self, run_id: str, seq: int, reason: str, call: LLMCall | None,
               detail: dict[str, Any]) -> None: ...

    def finish(self, run_id: str, *, stop: str | None, error: str | None) -> None: ...


_store: StepStore | None = None


def configure(store: StepStore | None) -> None:
    """Install the process's step store (the service does at startup); None removes it."""
    global _store
    _store = store


def configured() -> StepStore | None:
    return _store


@dataclass
class Run:
    """The current run. `loop`: an agent loop records its own chat turns (the gateway then
    appends only the JSON calls made inside it, e.g. by a tool). `last` / `last_call` / `rejected`:
    the kind of the last step appended, the last call, the last rejection's reason."""

    run_id: str
    user_id: str
    step_key: str
    store: StepStore
    loop: bool = False
    last: str = ""
    last_call: LLMCall | None = None
    rejected: str = ""
    stop: str | None = None
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def outcome(self) -> str:
        """The run's `stop`: set explicitly (`StepRun.stop`), else "rejected: <reason>" when the
        last step was a rejection, else "ok"."""
        if self.stop is not None:
            return self.stop
        return f"rejected: {self.rejected}"[:500] if self.last == "reject" else "ok"


_current: ContextVar[Run | None] = ContextVar("evowiki_run", default=None)
_own_turn: ContextVar[bool] = ContextVar("evowiki_run_own_turn", default=False)


@contextmanager
def own_turn() -> Iterator[None]:
    """The agent loop's own model call: the loop writes its step (`record_chat` leaves it alone)."""
    token = _own_turn.set(True)
    try:
        yield
    finally:
        _own_turn.reset(token)


def current() -> Run | None:
    return _current.get()


def current_run_id() -> str | None:
    """The run id of the step running now (None outside a traced run)."""
    run = _current.get()
    return run.run_id if run is not None else None


def current_user_id() -> str | None:
    run = _current.get()
    return run.user_id if run is not None else None


@contextmanager
def _bound(run: Run) -> Iterator[Run]:
    token = _current.set(run)
    try:
        yield run
    finally:
        _current.reset(token)


@contextmanager
def bind_run(run_id: str, user_id: str, step_key: str) -> Iterator[Run | None]:
    """Make a run already started on the configured store current in the block (not across a
    generator's yields): the JSON calls made inside are appended to it and their rows carry its
    id; chat calls are left to the run's own code (`Run.loop`). Without a configured store or a
    run id nothing is bound."""
    run = loop_run(run_id, user_id, step_key, _store) if _store is not None else None
    with binding(run):
        yield run


def loop_run(run_id: str, user_id: str, step_key: str, sink: Any) -> Run | None:
    """The Run of an agent loop that traces itself as `run_id` on `sink` (agent/loop.py binds it
    around each model call and tool run, `binding`): the JSON calls made inside go to the sink
    that started the run. None without a run id, or when the sink cannot take them (a sink with
    no `call`, e.g. a test's loop-only sink)."""
    if not run_id or not all(hasattr(sink, m) for m in ("call", "chat", "reject")):
        return None
    return Run(run_id, user_id, step_key, sink, loop=True)


@contextmanager
def binding(run: Run | None) -> Iterator[None]:
    """`run` as the current run in the block (nothing for None)."""
    if run is None:
        yield
        return
    with _bound(run):
        yield


class StepRun:
    """The handle `traced_step` yields: `run_id` (None when not traced), `reject` (as the module
    function), `stop` (the run's outcome text)."""

    def __init__(self, run: Run | None):
        self._run = run

    @property
    def run_id(self) -> str | None:
        return self._run.run_id if self._run is not None else None

    def reject(self, reason: str, call: LLMCall | None = None, **detail: Any) -> None:
        if self._run is not None:
            _reject(self._run, reason, call, detail)

    def stop(self, text: str) -> None:
        if self._run is not None:
            self._run.stop = text


def step_key_of(purpose: str) -> str:
    """The prompt_steps key of a purpose (the purpose itself when no step lists it)."""
    step = step_of(purpose)
    return step.key if step is not None else purpose


def _durable() -> Any:
    mod = sys.modules.get("evowiki.agent.durable")
    return mod if mod is not None and mod.in_workflow() else None


def _new_run_id(step_key: str, under: str | None = None) -> str:
    """`<under>:<step_key>:<hex>`, `<step_key>:<hex>` without `under`. A step inside another run
    is under the parent's id, so whatever deletes a run by its id prefix (memory forget:
    `assistant:<session>:`, a turn's `assistant:<session>:<message>:`) deletes it too."""
    base = f"{under}:{step_key}" if under else step_key

    def new() -> str:
        return f"{base}:{uuid.uuid4().hex}"
    durable = _durable()
    # in a workflow body the id is a recorded step: a re-executed body reuses its run
    return durable.step(f"trace.run:{step_key}", new) if durable is not None else new()


@contextmanager
def traced_step(step_key: str, subject: str, user_id: str | None, *, input: dict | None = None,
                pack_version: int = 0, under: str | None = None) -> Iterator[StepRun]:
    """Run a product step as an `agent_run` (agent STEP_AGENT, `step_key` the prompt_steps key):
    every `Gateway.call` / `converse` inside it is appended as a step, and the run finishes with
    its outcome (`Run.outcome`) or, when the block raises, the exception as `error`
    ("Type: message"), which is re-raised unchanged.

    Inside a run of the same step and user the block joins that run (a step's entry and its
    model call can both be wrapped; the outer one owns the run). Inside another run it opens its
    own, with `input.parent_run` and an id under the parent's (`_new_run_id`). `under`: the id
    prefix of a run that belongs to something without a run of its own (compaction: its
    session's `assistant:<session>`). Without a configured store or a user nothing is traced
    (`StepRun.run_id` None)."""
    store, cur = _store, _current.get()
    if store is None or not user_id:
        yield StepRun(None)
        return
    if cur is not None and not cur.loop and cur.step_key == step_key and cur.user_id == user_id:
        yield StepRun(cur)
        return
    run_id = _new_run_id(step_key, cur.run_id if cur is not None else under)
    parent = {"parent_run": cur.run_id} if cur is not None else {}
    store.start(run_id, user_id=user_id, agent=STEP_AGENT, subject=subject,
                pack_version=pack_version, input={**(input or {}), **parent}, step_key=step_key)
    run = Run(run_id, user_id, step_key, store)
    with _bound(run):
        try:
            yield StepRun(run)
        except BaseException as exc:
            try:
                store.finish(run_id, stop=None, error=_error(exc))
            except Exception as err:      # the step's own error is the one that propagates
                log.exception("trace run %s: finish failed", run_id)
                exc.add_note(f"trace finish of {run_id} failed: {type(err).__name__}: {err}")
            raise
    store.finish(run_id, stop=run.outcome(), error=None)


Pick = str | Callable[[dict[str, Any]], Any]     # an argument's name, or a function of the args


def _pick(how: Pick, args: dict[str, Any]) -> Any:
    return how(args) if callable(how) else args.get(how)


def traced_entry(step_key: str, *, subject: Pick, user: Pick = "user_id",
                 input: Callable[[dict[str, Any]], dict] | None = None) -> Callable[[F], F]:
    """Decorate a step's entry so a call of it is `traced_step(step_key, subject, user)`; `subject`
    and `user` name an argument or compute it from the bound arguments (`input` the run's input). The step's model calls and
    its commit run inside the run (`current_run_id()` at the commit point). A generator function
    (a streaming step) is bound to its run only while it runs, never across a yield: the consumer
    may resume it from another thread or context."""
    def deco(fn: F) -> F:
        sig = inspect.signature(fn)

        def where(a: tuple, kw: dict) -> tuple[str, str | None, dict]:
            b = sig.bind(*a, **kw)
            b.apply_defaults()
            uid = _pick(user, b.arguments)
            return (str(_pick(subject, b.arguments) or ""), (str(uid) if uid else None),
                    input(b.arguments) if input is not None else {})

        if inspect.isgeneratorfunction(fn):
            @functools.wraps(fn)
            def gen(*a: Any, **kw: Any) -> Any:
                subj, uid, inp = where(a, kw)
                return (yield from _traced_gen(step_key, subj, uid, inp, fn(*a, **kw)))
            return gen  # type: ignore[return-value]

        @functools.wraps(fn)
        def run(*a: Any, **kw: Any) -> Any:
            subj, uid, inp = where(a, kw)
            with traced_step(step_key, subj, uid, input=inp):
                return fn(*a, **kw)
        return run  # type: ignore[return-value]
    return deco


def _traced_gen(step_key: str, subject: str, user_id: str | None, input: dict,
                inner: Generator) -> Generator:
    """`inner` as one run, bound around each resumption (`traced_entry`)."""
    store = _store
    if store is None or not user_id or _current.get() is not None:
        return (yield from inner)
    run_id = _new_run_id(step_key)
    store.start(run_id, user_id=user_id, agent=STEP_AGENT, subject=subject, pack_version=0,
                input=input, step_key=step_key)
    run = Run(run_id, user_id, step_key, store)
    try:
        while True:
            with _bound(run):
                try:
                    value = next(inner)
                except StopIteration as done:
                    result = done.value
                    break
            yield value
    except GeneratorExit:
        inner.close()
        store.finish(run_id, stop="closed", error=None)
        raise
    except BaseException as exc:
        try:
            store.finish(run_id, stop=None, error=_error(exc))
        except Exception as err:
            log.exception("trace run %s: finish failed", run_id)
            exc.add_note(f"trace finish of {run_id} failed: {type(err).__name__}: {err}")
        raise
    store.finish(run_id, stop=run.outcome(), error=None)
    return result


@contextmanager
def auto_run(purpose: str, subject: str, user_id: str | None) -> Iterator[None]:
    """A model call of a user outside any run: a run of its own (`traced_step` of the purpose's
    step). Inside a run, or without a user or a store, nothing."""
    if _current.get() is not None or _store is None or not user_id:
        yield
        return
    with traced_step(step_key_of(purpose), subject, user_id, input={"auto": True}):
        yield


def reject(reason: str, call: LLMCall | None = None, **detail: Any) -> None:
    """Record that the current step refused a parsed reply (module doc); `call` defaults to the
    run's last call. Outside a run nothing."""
    run = _current.get()
    if run is not None:
        _reject(run, reason, call, detail)


def _reject(run: Run, reason: str, call: LLMCall | None, detail: dict[str, Any]) -> None:
    run.store.reject(run.run_id, run.next_seq(), reason, call or run.last_call, detail)
    run.last, run.rejected = "reject", reason


def output_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def record_call(request: LLMRequest, schema_name: str, call: LLMCall | None,
                error: BaseException | None) -> None:
    """Append a JSON call (or its failure) to the current run, if any."""
    run = _current.get()
    if run is None:
        return
    run.store.call(run.run_id, run.next_seq(), request, schema_name, call, _error(error))
    run.last, run.last_call = "call", call


def record_chat(request: ChatRequest, turn: ChatTurn | None, call: LLMCall | None,
                error: BaseException | None) -> None:
    """Append a chat call to the current run (its full message list). A loop records its own turns
    (`own_turn`); any other chat call in a loop is a tool's (a page read by the vision model), and
    without this step nothing would hold its input."""
    run = _current.get()
    if run is None or (run.loop and _own_turn.get()):
        return
    run.store.chat(run.run_id, run.next_seq(), request, turn,
                   call.id if call is not None else None, _error(error))
    run.last, run.last_call = "call", call


def _error(exc: BaseException | None) -> str | None:
    return None if exc is None else f"{type(exc).__name__}: {exc}"[:500]


@cache
def code_version() -> str:
    """The code that runs: EVOWIKI_COMMIT, else the git sha of this checkout (with "+dirty" when
    tracked files have changes), else "unknown". Read once per process."""
    pinned = os.environ.get(COMMIT_ENV, "").strip()
    if pinned:
        return pinned
    root = Path(__file__).resolve().parents[3]
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=root, capture_output=True, text=True, timeout=5,
                               check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if not sha:
        return "unknown"
    return f"{sha}+dirty" if dirty else sha
