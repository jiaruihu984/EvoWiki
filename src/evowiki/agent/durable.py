"""Durable execution helpers (spec 2026-10-14 §1–§2, B1b): the one place agent and flow code
touches DBOS's step API.

A DBOS workflow body is re-executed from the top on recovery; a step whose output was checkpointed
returns that output instead of running again. So everything nondeterministic or external that a
body does goes through `step`: model calls (the gateway does it itself), render checks, repo reads
and commits, ids, the clock. Between steps the body must be deterministic.

Rules enforced here (FINDINGS B1b a): a step's value is JSON — it is round-tripped through
`json` inside the step, so the body sees the same lists / dicts on the first run and on replay
(DBOS returns the live object the first time and the deserialized one on replay). An exception
raised inside a step is caught there and returned as JSON (`encode_exc`); the body re-raises it
(`decode_exc`) with its class and JSON attributes, on the first run and on replay alike. Only
`evowiki.*` and builtin exception classes are rebuilt; anything else becomes `StepFailed`.

`virtual_workflow(id)` runs a body without DBOS (tests, the inline page runs): the same round
trips, nothing checkpointed. Outside any workflow, and inside a step, every helper is a plain call:
the rest of the app is unchanged.

`outside_dbos(fn)`: DBOS cannot start a workflow inside a step (it asserts), and a start from a
workflow body is recorded as a child of it. Code that may run in either and has to start a root
workflow (the supplement match, the assistant's add_source / revise_page, whose tools run inside
the turn's step) calls it: `fn` runs on a short joined thread with an empty context."""

import builtins
import contextvars
import importlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger("evowiki.agent.durable")


class StepFailed(RuntimeError):
    """A step failed with an exception that cannot be rebuilt in the body, or returned non-JSON."""


@dataclass
class _Virtual:
    workflow_id: str
    next_step: int = 0


_virtual: ContextVar[_Virtual | None] = ContextVar("durable_virtual", default=None)
_step_key: ContextVar[str | None] = ContextVar("durable_step_key", default=None)


def _dbos_ctx() -> Any:
    from dbos._context import get_local_dbos_context
    return get_local_dbos_context()


def _dbos_body() -> bool:
    ctx = _dbos_ctx()
    return bool(ctx is not None and ctx.is_workflow())


def in_workflow() -> bool:
    if _step_key.get() is not None:
        return False
    return _virtual.get() is not None or _dbos_body()


def on_dbos() -> bool:
    """In the body of a real DBOS workflow (not a virtual one, not inside a step)."""
    return _virtual.get() is None and in_workflow()


@contextmanager
def virtual_workflow(workflow_id: str) -> Iterator[None]:
    token = _virtual.set(_Virtual(workflow_id))
    try:
        yield
    finally:
        _virtual.reset(token)


def workflow_id() -> str:
    v = _virtual.get()
    if v is not None:
        return v.workflow_id
    ctx = _dbos_ctx()
    if ctx is not None and ctx.is_within_workflow():
        from dbos import DBOS
        return str(DBOS.workflow_id)
    raise RuntimeError("not in a workflow")


CANCEL_POLL_S = 1.0     # how often a model call in flight reads its workflow's status (DBOS: 1 s too)


def workflow_cancel_check() -> Callable[[], bool] | None:
    """In the body of a real DBOS workflow: a non-blocking check that says whether this workflow
    has been cancelled (「停止」: `DBOS.cancel_workflow`), its status read from the system database
    at most every CANCEL_POLL_S on a background thread (`llm.deadline.BackgroundPoll`; the read
    goes past DBOS's step API, so it records no step). Elsewhere None: a virtual workflow is
    stopped by its own cancel event, before its next call."""
    if not on_dbos():
        return None
    from dbos import DBOS
    from dbos._dbos import _get_dbos_instance

    from evowiki.llm.deadline import BackgroundPoll
    wid = str(DBOS.workflow_id)
    sys_db = _get_dbos_instance()._sys_db

    def cancelled() -> bool:
        st = sys_db.get_workflow_status(wid)
        return st is not None and st["status"] == "CANCELLED"
    return BackgroundPoll(cancelled, CANCEL_POLL_S)


def step_key() -> str:
    key = _step_key.get()
    if key is None:
        raise RuntimeError("step_key() is only available inside a durable step")
    return key


def outside_dbos[T](fn: Callable[[], T]) -> T:
    """`fn()` on a short thread with an empty context (no DBOS workflow / step, no durable step
    key), joined; its value returned, its exception re-raised here."""
    out: list[T] = []
    errors: list[Exception] = []

    def body() -> None:
        try:
            out.append(fn())
        except Exception as exc:  # noqa: BLE001 - handed to the caller
            errors.append(exc)
    t = threading.Thread(target=contextvars.Context().run, args=(body,), name="outside-dbos",
                         daemon=True)
    t.start()
    t.join()
    if errors:
        raise errors[0]
    return out[0]


# ---- exceptions ----

def _jsonable(v: Any) -> bool:
    try:
        json.dumps(v)
    except (TypeError, ValueError):
        return False
    return True


def encode_exc(exc: BaseException) -> dict[str, Any]:
    cls = type(exc)
    return {"module": cls.__module__, "qualname": cls.__qualname__,
            "args": [a if _jsonable(a) else str(a) for a in exc.args],
            "attrs": {k: v for k, v in vars(exc).items() if _jsonable(v)},
            "dropped": sorted(k for k, v in vars(exc).items() if not _jsonable(v)),
            "text": str(exc)}


def _resolve(module: str, qualname: str) -> type[Exception] | None:
    if module == "builtins":
        cls = getattr(builtins, qualname, None)
    elif module.startswith("evowiki.") and "<locals>" not in qualname:
        try:
            obj: Any = importlib.import_module(module)
            for part in qualname.split("."):
                obj = getattr(obj, part)
            cls = obj
        except (ImportError, AttributeError):
            return None
    else:
        return None
    return cls if isinstance(cls, type) and issubclass(cls, Exception) else None


def decode_exc(d: dict[str, Any]) -> Exception:
    cls = _resolve(d["module"], d["qualname"])
    if cls is None:
        return StepFailed(f"{d['qualname']}: {d['text']}")
    exc = cls.__new__(cls)
    Exception.__init__(exc, *d["args"])
    for k, v in d["attrs"].items():
        setattr(exc, k, v)
    for k in d.get("dropped", []):      # not JSON: the read does not raise AttributeError, it sees None
        setattr(exc, k, None)
    return exc


# ---- steps ----

def _guarded(fn: Callable[[], Any], key_of: Callable[[], str]) -> dict[str, Any]:
    token = _step_key.set(key_of())
    try:
        value = fn()
    except Exception as exc:  # crosses the boundary as JSON, re-raised in the body
        log.warning("step raised %s", type(exc).__name__, exc_info=exc)
        return {"raised": encode_exc(exc)}
    finally:
        _step_key.reset(token)
    try:
        return {"value": json.loads(json.dumps(value))}
    except (TypeError, ValueError) as exc:
        return {"raised": encode_exc(StepFailed(f"step value is not JSON: {exc}"))}


def step_ran(name: str, fn: Callable[[], Any]) -> tuple[Any, bool]:
    if not in_workflow():
        return fn(), True
    ran: list[bool] = []

    def run() -> dict[str, Any]:
        ran.append(True)
        return _guarded(fn, _key)

    v = _virtual.get()
    if v is not None:
        def _key() -> str:
            return f"{v.workflow_id}:{v.next_step}"
        out = run()
        v.next_step += 1
    else:
        from dbos import DBOS

        def _key() -> str:
            return f"{DBOS.workflow_id}:{DBOS.step_id}"
        out = DBOS.run_step({"name": name, "retries_allowed": False}, run)
    if "raised" in out:
        raise decode_exc(out["raised"])
    return out["value"], bool(ran)


def step(name: str, fn: Callable[[], Any]) -> Any:
    return step_ran(name, fn)[0]


def ids(new_id: Callable[[str], str]) -> Callable[[str], str]:
    def durable_id(prefix: str) -> str:
        return step("new_id", lambda: new_id(prefix)) if in_workflow() else new_id(prefix)
    return durable_id


def sleep(seconds: float, sleeper: Callable[[float], None] = time.sleep) -> None:
    """A pause in a workflow body: DBOS's durable sleep in a DBOS workflow (a recovered workflow
    does not sleep again for the time already slept), else `sleeper` (tests pass a fake one)."""
    if on_dbos():
        from dbos import DBOS
        DBOS.sleep(seconds)
        return
    sleeper(seconds)


def now(clock: Callable[[], datetime]) -> datetime:
    if not in_workflow():
        return clock()
    return datetime.fromisoformat(step("clock", lambda: clock().isoformat()))
