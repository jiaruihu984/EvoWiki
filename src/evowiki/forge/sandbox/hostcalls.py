"""Host read-only RPC for forged tools by deterministic re-execution (plan P2 T5).

The WASI guest's stdin is a file, so it cannot block on a reply. A forged tool is a pure function,
so re-running it is safe (Temporal / DBOS replay): each execution gets the host results recorded so
far; the guest's `host.call` returns them in order and, at the first call without a recorded
result, the guest stops with ``{"ok": true, "need": {"tool", "args"}}``. `drive` validates that
need (it is untrusted guest output), runs it through the host port, records the exchange and runs
the guest again.

Trust boundary: the guest only ever names a tool and JSON arguments. The host refuses any tool
outside both `host.allowed` and the read-only allowlist `limits.HOST_READ_TOOLS`, caps the number
of calls and the size of arguments and results, and fails closed (a plain error result) on any
problem. Nothing here executes guest code: `run_once` is the sandbox's own single execution.
"""
import json
from collections.abc import Callable
from typing import Any

from evowiki.forge.limits import (
    HOST_ARGS_CHARS,
    HOST_MAX_CALLS,
    HOST_READ_TOOLS,
    HOST_RESULT_CHARS,
)
from evowiki.forge.models import HostExchange
from evowiki.forge.sandbox import HostSpec

_READ_ONLY = frozenset(t for tools in HOST_READ_TOOLS.values() for t in tools)
NAME_SHOWN = 60


class CassetteMismatch(LookupError):
    """A host call that the cassette did not record (VCR "none" record mode)."""


def _norm(args: Any) -> Any:
    return json.loads(json.dumps(args, ensure_ascii=False, sort_keys=True))


class CassetteHost:
    """A HostPort replaying an example's cassette strictly (VCR "none" record mode): the i-th call
    must equal cassette[i] (tool and args); an extra or different call fails. `unused()` after the
    run lists cassette entries never asked for (the build counts that as a failed example)."""

    def __init__(self, cassette: tuple[HostExchange, ...]):
        self._cassette = tuple(cassette)
        self._i = 0
        self.mismatched = False           # set once any call diverged from the cassette

    def __call__(self, tool: str, args: dict[str, Any]) -> HostExchange:
        if self._i >= len(self._cassette):
            self.mismatched = True
            raise CassetteMismatch("读取数据的次数比记录的多")
        ex = self._cassette[self._i]
        if ex.tool != tool or _norm(ex.args) != _norm(args):
            self.mismatched = True
            raise CassetteMismatch("读取数据的方式和记录不一致")
        self._i += 1
        return ex

    def unused(self) -> tuple[HostExchange, ...]:
        return self._cassette[self._i:]


def _fail(reason: str) -> dict[str, Any]:
    return {"ok": False, "error": reason}


def _shown(name: Any) -> str:
    return str(name)[:NAME_SHOWN]


def _parse_need(need: Any) -> tuple[str, dict[str, Any]] | str:
    """(tool, normalised args) of a guest's need, or the plain reason it is refused."""
    if not isinstance(need, dict) or set(need) != {"tool", "args"}:
        return "工具发出的读取请求格式不对"
    tool, args = need["tool"], need["args"]
    if not isinstance(tool, str) or not isinstance(args, dict):
        return "工具发出的读取请求格式不对"
    try:
        raw = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return "工具发出的读取请求参数不是 JSON"
    if len(raw) > HOST_ARGS_CHARS:
        return f"工具发出的读取请求参数太长（上限 {HOST_ARGS_CHARS} 字）"
    return tool, json.loads(raw)


def drive(run_once: Callable[[list[dict[str, Any]]], dict[str, Any]], host: HostSpec | None, *,
          max_calls: int = HOST_MAX_CALLS) -> tuple[dict[str, Any], tuple[HostExchange, ...]]:
    """Run the guest until it returns without a `need`. `run_once(recorded)` runs the guest once
    with the recorded exchanges and returns its result dict. A `need` with no `host`, for a tool
    outside `host.allowed`, or past `max_calls` ends with {"ok": False, "error": <plain reason>}.
    Host results are clipped to HOST_RESULT_CHARS before they are recorded."""
    calls: list[HostExchange] = []
    while True:
        res = run_once([c.model_dump(mode="json") for c in calls])
        if not (isinstance(res, dict) and res.get("ok") and "need" in res):
            return res, tuple(calls)
        parsed = _parse_need(res["need"])
        if isinstance(parsed, str):
            return _fail(parsed), tuple(calls)
        tool, args = parsed
        if host is None:
            return _fail(f"这个工具不能调用 {_shown(tool)}（没有可用的读取接口）"), tuple(calls)
        if tool not in host.allowed or tool not in _READ_ONLY:
            return _fail(f"这个工具不能调用 {_shown(tool)}"), tuple(calls)
        if len(calls) >= max_calls:
            return _fail(f"读取数据的次数超过上限（{max_calls} 次）"), tuple(calls)
        try:
            got = host.port(tool, args)
        except CassetteMismatch as exc:
            return _fail(str(exc)), tuple(calls)
        except Exception:  # noqa: BLE001 - fail closed with a plain reason, never crash the run
            return _fail(f"读取数据失败（{tool}）"), tuple(calls)
        if (not isinstance(got, HostExchange) or got.tool != tool
                or _norm(got.args) != args):
            return _fail(f"读取数据失败（{tool}）"), tuple(calls)
        calls.append(HostExchange(tool=tool, args=args, ok=got.ok,
                                  content=got.content[:HOST_RESULT_CHARS]))
