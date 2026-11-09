"""Phase-1 forge flow end to end with a scripted coder (plan T9): propose → build → promote (gate
fake) → pinned view → run the tool → run-scoped overlay of an improved candidate → undo → kill
switch. Runs once with the in-process fake sandbox and once in the real WASI sandbox (skipped when
the runtime is not installed)."""

import json
from datetime import UTC, datetime

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.content.models import Lang
from evowiki.forge import switch, undo
from evowiki.forge.api import ForgeDeps, propose
from evowiki.forge.models import Evidence, ToolExample, ToolRequest
from evowiki.forge.registry import promote, view
from evowiki.forge.sandbox import runtime
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user
from tests.forge.fakes import InProcessSandbox, ScriptedConverse, accounted_fake

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
CTX = ToolContext(user_id="u1", session_id="s1")

COUNT = '''import re


def _count(side):
    total = {}
    for term in side.split("+"):
        m = re.match(r"^(\\d*)(.*)$", term.strip())
        k = int(m.group(1) or 1)
        for el, n in re.findall(r"([A-Z][a-z]?)(\\d*)", m.group(2)):
            total[el] = total.get(el, 0) + k * int(n or 1)
    return total
'''
TOOL_V1 = COUNT + '''

def run(args):
    left, right = args["eq"].split("=")
    return {"ok": _count(left) == _count(right)}
'''
TOOL_V2 = COUNT + '''

def run(args):
    left, right = args["eq"].replace("->", "=").split("=")
    return {"ok": _count(left) == _count(right)}
'''
TESTS = '''import tool


def test_balanced():
    assert tool.run({"eq": "2H2+O2=2H2O"}) == {"ok": True}


def test_unbalanced():
    assert tool.run({"eq": "H2+O2=H2O"}) == {"ok": False}
'''
SCHEMA = json.dumps({"type": "object", "description": "判断化学方程式是否配平。只看元素个数。",
                     "properties": {"eq": {"type": "string"}}, "required": ["eq"]},
                    ensure_ascii=False)
EXAMPLES = (ToolExample(args={"eq": "2H2+O2=2H2O"}, expect={"ok": True}),
            ToolExample(args={"eq": "H2+O2=H2O"}, expect={"ok": False}),
            ToolExample(args={"eq": "CH4+2O2=CO2+2H2O"}, expect={"ok": True}))
EVI = Evidence(citations=("signal:s1",), note="配平总要手算")


def _script(tool: str) -> ScriptedConverse:
    def t(i, name, **args):
        return ChatTurn(text="", tool_calls=(ToolCall(id=f"t{i}", name=name, args=args),))
    return ScriptedConverse(
        t(1, "write_file", path="tool.py", content=tool),
        t(2, "write_file", path="test_tool.py", content=TESTS),
        t(3, "write_file", path="schema.json", content=SCHEMA),
        t(4, "run_tests"),
        t(5, "submit", summary="写好了"))


class _Trace:
    def start(self, *a, **kw): ...
    def model(self, *a, **kw): ...
    def tool(self, *a, **kw): ...
    def finish(self, *a, **kw): ...
    def finish_failed(self, *a, **kw): ...


def _sandboxes():
    yield pytest.param(InProcessSandbox, id="fake")
    yield pytest.param("wasi", id="wasi", marks=pytest.mark.skipif(
        not runtime.available(), reason=runtime.why_unavailable() or "no runtime"))


def _make(kind):
    if kind == "wasi":
        from evowiki.forge.sandbox.wasi import WasiSandbox
        return WasiSandbox.from_env()
    return kind()


def _run(tool, eq):
    res = tool.run({"eq": eq}, CTX)
    assert res.error is None, res
    return json.loads(res.content)


@pytest.mark.parametrize("kind", list(_sandboxes()))
def test_forge_flow_end_to_end(pool, monkeypatch, kind):
    monkeypatch.delenv(switch.ENV, raising=False)
    sb = _make(kind)
    ids = iter(["fc_v1", "fc_v2"])
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))

    def deps(tool):
        return ForgeDeps(pool=pool, converse=accounted_fake(_script(tool)), sandbox=sb, trace=_Trace(),
                         now=lambda: T0, new_id=lambda: next(ids))

    # 1. forge + build (host re-runs tests and the three examples)
    req = ToolRequest(name="check_balanced", title="配平检查", purpose="判断化学方程式是否配平", agent="deep",
                      examples=EXAMPLES)
    r1 = propose("tool", req, EVI, user_id="u1", deps=deps(TOOL_V1))
    assert r1.build.ok, r1.build.reason
    b1 = r1.build                     # one of the three examples is held out from the coder
    assert (b1.tests_passed, b1.examples_passed, b1.holdout_passed, b1.holdout_total) == (2, 2, 1, 1)

    with pool.connection() as conn:
        # 2. promote behind a (fake) passing gate
        assert promote(conn, r1.candidate_id, gate_ref="gate:1", by="improver", why="配平检查",
                       gate_ok=lambda ref: ref == "gate:1") == 1
        v = view(conn, "u1", enabled=switch.enabled(conn, "u1", "tool"), sandbox=sb)
    assert v.tool_names("deep") == {"check_balanced": ("eq",)}
    [tool] = v.tools_for("deep", {"check_balanced"})
    assert tool.spec.effect == "read" and tool.spec.category == "read"
    assert _run(tool, "2H2+O2=2H2O") == {"ok": True}
    assert _run(tool, "H2+O2=H2O") == {"ok": False}
    assert v.tools_for("deep", set()) == []

    # 3. an improved candidate (parent = v1) seen only through a run-scoped overlay
    req2 = req.model_copy(update={"parent": r1.candidate_id, "examples": EXAMPLES + (
        ToolExample(args={"eq": "2H2+O2->2H2O"}, expect={"ok": True}),)})
    r2 = propose("tool", req2, EVI, user_id="u1", deps=deps(TOOL_V2))
    assert r2.build.ok, r2.build.reason
    with pool.connection() as conn:
        assert store.get_candidate(conn, r2.candidate_id).parent_id == r1.candidate_id
        overlay = v.with_candidate(r2.candidate_id)
        fresh = view(conn, "u1", enabled=True, sandbox=sb)
    assert overlay.version == v.version
    [better] = overlay.tools_for("deep", {"check_balanced"})
    assert _run(better, "2H2+O2->2H2O") == {"ok": True}
    assert tool.run({"eq": "2H2+O2->2H2O"}, CTX).error              # the pinned view is unchanged
    assert fresh.tools["check_balanced"].candidate_id == r1.candidate_id   # nothing was written

    # 4. undo from the change list: the tool is gone from a new view
    with pool.connection() as conn:
        [row] = undo.changes(conn, "u1")
        assert row["text"] == "新增了工具「配平检查」" and row["can_undo"]
        assert undo.undo(conn, "u1", row["change_id"]) == 2
        assert undo.undo(conn, "u1", row["change_id"]) is None
        after = view(conn, "u1", enabled=switch.enabled(conn, "u1", "tool"), sandbox=sb)
        assert after.tools_for("deep", {"check_balanced"}) == []

        # 5. kill switch: a promoted tool disappears, nothing is deleted, and comes back
        promote(conn, r2.candidate_id, gate_ref="gate:2", by="improver", why="更宽的写法",
                gate_ok=lambda ref: True)
        switch.set_enabled(conn, "u1", "tool", False, T0)
        off = view(conn, "u1", enabled=switch.enabled(conn, "u1", "tool"), sandbox=sb)
        assert off.tools_for("deep", {"check_balanced"}) == []
        switch.set_enabled(conn, "u1", "tool", True, T0)
        monkeypatch.setenv(switch.ENV, "off")                       # env wins over the setting
        assert not switch.enabled(conn, "u1", "tool")
        monkeypatch.delenv(switch.ENV)
        on = view(conn, "u1", enabled=switch.enabled(conn, "u1", "tool"), sandbox=sb)
    [back] = on.tools_for("deep", {"check_balanced"})
    assert back.forged.candidate_id == r2.candidate_id


@pytest.mark.skipif(not runtime.available(), reason=runtime.why_unavailable() or "no runtime")
def test_a_spoofed_test_report_cannot_pass_the_build():
    """The guest reports its own test results, so a test file can forge a passing report. The
    build's verdict still rests on the examples, which the host runs and compares itself."""
    from evowiki.forge.build import build
    from evowiki.forge.sandbox.wasi import WasiSandbox
    from evowiki.forge.workspace import Workspace
    sb = WasiSandbox.from_env()
    spoof = ("import os\nos.write(1, b'{\"ok\": true, \"cases\": [{\"name\": \"test_fake\", "
             "\"passed\": true, \"detail\": \"\"}]}\\n')\nos._exit(0)\n")
    wrong = "def run(args):\n    return {'ok': True}\n"
    files = {"tool.py": wrong, "test_tool.py": spoof, "schema.json": SCHEMA}
    assert sb.run_tests(files).passed == 1                          # the spoof does get through
    req = ToolRequest(name="check_balanced", title="配平检查", purpose="p", agent="deep", examples=EXAMPLES)
    rep = build(Workspace.of(files), req, sb)
    assert not rep.ok and rep.examples_failed + rep.holdout_failed == 1, rep
    assert "样例" in rep.reason, rep


@pytest.mark.skipif(not runtime.available(), reason=runtime.why_unavailable() or "no runtime")
def test_real_wasi_smoke():
    from evowiki.forge.sandbox.wasi import WasiSandbox
    sb = WasiSandbox.from_env()
    files = {"tool.py": TOOL_V1, "test_tool.py": TESTS, "schema.json": SCHEMA}
    rep = sb.run_tests(files)
    assert (rep.passed, rep.failed, rep.error) == (2, 0, ""), rep
    out = sb.run_tool(files, {"eq": "CH4+2O2=CO2+2H2O"})
    assert out.ok and out.output == {"ok": True}, out
    # the guest cannot reach the network or write to its workspace
    probe = ("import socket, os\n\ndef run(args):\n    r = {}\n    try:\n"
             "        socket.create_connection(('example.com', 80), timeout=1)\n"
             "        r['net'] = True\n    except Exception:\n        r['net'] = False\n"
             "    try:\n        open('/work/x.txt', 'w').write('x')\n        r['write'] = True\n"
             "    except Exception:\n        r['write'] = False\n"
             "    r['env'] = sorted(os.environ)\n    return r\n")
    out = sb.run_tool({"tool.py": probe}, {})
    assert out.ok and out.output == {"net": False, "write": False, "env": []}, out
