"""Runs INSIDE the WASI guest: the `host` module a forged tool imports (plan P2 T5). Stdlib only.

`host.call(tool, args)` returns the text result of one of the agent's read-only tools. The guest
cannot wait for a reply (its stdin is a file), so the tool is re-executed: `guest_runner` fills
`_recorded` with the results served so far; at the first call without one the guest stops with a
`need` and the host serves it before the next execution. The host never imports this file; it is
copied read-only to /runner.
"""
import json

_recorded = []      # set by guest_runner from the request
_allowed = ()
_i = 0
_need = None
_fake = None        # tests: host.install(fn)


class HostError(Exception):
    """The host tool failed (e.g. no such page) or the call differs from the recorded one."""


class _Need(BaseException):
    """Stops the execution; BaseException so a tool's `except Exception` cannot swallow it."""


def install(fn):
    """For test_tool.py: `fn(tool, args) -> str` answers every host.call in tests."""
    global _fake
    _fake = fn


def call(tool, args):
    """Call one of the agent's read-only tools; returns its text result."""
    global _i, _need
    if _fake is not None:
        return _fake(tool, args)
    if tool not in _allowed:
        raise PermissionError(f"不能调用 {tool}")
    args = json.loads(json.dumps(args, ensure_ascii=False))
    if _i < len(_recorded):
        ex = _recorded[_i]
        _i += 1
        if ex["tool"] != tool or ex["args"] != args:
            raise HostError("调用顺序和上次不同（工具必须是纯函数）")
        if not ex["ok"]:
            raise HostError(ex["content"])
        return ex["content"]
    if _need is None:                 # the first unserved call wins; later ones wait their turn
        _need = {"tool": tool, "args": args}
    raise _Need()
