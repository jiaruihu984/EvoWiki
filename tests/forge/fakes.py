"""Test-only fakes for the forge. Nothing in src/ may import this (it execs forged code in-process)."""
import sys
import types
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evowiki.forge.binding import ForgeConverse
from evowiki.forge.models import RunOutcome, SandboxStats, TestCase, TestReport
from evowiki.forge.sandbox import HostSpec
from evowiki.forge.sandbox.hostcalls import drive
from evowiki.llm.chat import ChatRequest, ChatTurn
from evowiki.llm.models import LLMCall
from evowiki.llm.usage import Account

OK = SandboxStats(wall_s=0.0, exit="ok")


def fake_call(i: int = 0) -> LLMCall:
    return LLMCall(id=f"c{i}", input_hash=f"h{i}", purpose="t", prompt_version="v", subject="s",
                   provider="p", model="m", model_version=None, params={}, raw_output="",
                   parsed_output=None, cost=None, latency=0.0, ts=datetime(2026, 1, 1, tzinfo=UTC))


class ScriptedConverse:
    """Pops one scripted ChatTurn (or exception) per call and records the requests."""

    def __init__(self, *items: ChatTurn | Exception):
        self.items = list(items)
        self.requests: list[ChatRequest] = []

    def __call__(self, req: ChatRequest) -> tuple[ChatTurn, LLMCall]:
        self.requests.append(req)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, fake_call(len(self.requests))


_GUEST_HOST = (Path(__file__).resolve().parents[2] / "src" / "evowiki" / "forge" / "sandbox"
               / "guest_host.py").read_text(encoding="utf-8")


def _host_module(recorded=(), allowed=()) -> types.ModuleType:
    """A fresh copy of the real guest `host` module (one per guest execution, like WASI)."""
    mod = types.ModuleType("host")
    exec(_GUEST_HOST, mod.__dict__)  # noqa: S102 - tests only
    mod._recorded = list(recorded)
    mod._allowed = tuple(allowed)
    return mod


class InProcessSandbox:
    policy = "read-only"

    def run_tool(self, files: Mapping[str, str], args: dict[str, Any],
                 host: HostSpec | None = None) -> RunOutcome:
        tools = tuple(host.allowed) if host else ()

        def once(recorded):
            mod = _host_module(recorded, tools)
            saved = sys.modules.get("host")
            sys.modules["host"] = mod
            try:
                ns: dict[str, Any] = {"__name__": "tool"}
                exec(files["tool.py"], ns)  # noqa: S102 - tests only
                res = {"ok": True, "output": ns["run"](args)}
            except BaseException as e:  # noqa: BLE001 - like guest_runner
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:2000]}
            finally:
                if saved is None:
                    sys.modules.pop("host", None)
                else:
                    sys.modules["host"] = saved
            if mod._need is not None:
                res = {"ok": True, "need": mod._need}
            return res

        res, calls = drive(once, host)
        if not res.get("ok"):
            return RunOutcome(ok=False, error=str(res.get("error", ""))[:2000], stats=OK,
                              host_calls=calls)
        return RunOutcome(ok=True, output=res["output"], stats=OK, host_calls=calls)

    def run_tests(self, files: Mapping[str, str]) -> TestReport:
        saved = sys.modules.get("tool")
        saved_host = sys.modules.get("host")
        sys.modules["host"] = _host_module()
        try:
            mod = types.ModuleType("tool")
            try:
                exec(files["tool.py"], mod.__dict__)  # noqa: S102
            except Exception as e:  # noqa: BLE001
                return TestReport(cases=(), stats=OK, error=f"{type(e).__name__}: {e}"[:2000])
            sys.modules["tool"] = mod
            ns: dict[str, Any] = {"__name__": "test_tool"}
            try:
                exec(files["test_tool.py"], ns)  # noqa: S102
            except Exception as e:  # noqa: BLE001
                return TestReport(cases=(), stats=OK, error=f"{type(e).__name__}: {e}"[:2000])
            cases = []
            for name, fn in list(ns.items()):
                if name.startswith("test_") and callable(fn):
                    try:
                        fn()
                        cases.append(TestCase(name=name, passed=True))
                    except Exception as e:  # noqa: BLE001
                        cases.append(TestCase(name=name, passed=False,
                                              detail=f"{type(e).__name__}: {e}"[:500]))
            return TestReport(cases=tuple(cases), stats=OK)
        finally:
            if saved is None:
                sys.modules.pop("tool", None)
            else:
                sys.modules["tool"] = saved
            if saved_host is None:
                sys.modules.pop("host", None)
            else:
                sys.modules["host"] = saved_host


def accounted_fake(inner, user="u1", group="other", cost=None):
    """A ForgeConverse over a scripted converse (tests): the gateway ignores the role."""
    class _Gw:
        def converse(self, role, req, *, account=None):
            t, c = inner(req)
            return (t, c) if cost is None else (t, c.model_copy(update={"cost": cost}))
    return ForgeConverse(gateway=_Gw(), account=Account(user_id=user, group=group))
