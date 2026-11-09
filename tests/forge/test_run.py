import json
import logging
from datetime import UTC, datetime

import pytest

from evowiki.agent import durable
from evowiki.agent.tools import ToolContext, ToolResult, ToolSpec
from evowiki.content.models import Lang
from evowiki.forge import optin, registry, switch
from evowiki.forge import run as forge_run
from evowiki.forge.registry import SandboxedTool
from evowiki.forge.run import ForgeRuntime
from evowiki.llm.usage import Account
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user
from tests.forge.fakes import InProcessSandbox
from tests.forge.test_propose import script

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
CTX = ToolContext(user_id="u1", session_id="s1")


def schema(desc, **props):
    return {"type": "object", "description": desc,
            "properties": {k: {"type": v} for k, v in props.items()}, "required": sorted(props)}


DOUBLE = {"tool.py": "def run(args):\n    return {'n': args['n'] * 2}\n",
          "schema.json": json.dumps(schema("把 n 翻倍", n="integer"))}
TRIPLE = {"tool.py": "def run(args):\n    return {'n': args['n'] * 3}\n",
          "schema.json": json.dumps(schema("把 n 变成三倍", n="integer"))}
READER = {"tool.py": ("import host\n"
                      "def run(args):\n"
                      "    return {'t': host.call('read_page', {'page_id': args['p']})}\n"),
          "schema.json": json.dumps(schema("读一页", p="string"))}


@pytest.fixture
def users(pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2",
                                  lang=Lang.EN, created_at=T0))
    return "u1"


def cand(pool, cid, name, files, *, title="工具", host_tools=(), origin="round", status="built"):
    with pool.connection() as conn:
        store.insert_candidate(conn, store.CandidateRow(
            id=cid, user_id="u1", kind="tool", name=name, parent_id=None,
            spec={"name": name, "title": title, "agent": "deep", "host_tools": list(host_tools)},
            evidence={}, files=files, build={"ok": True}, status=status, run_id=None,
            cost_usd=None, created_at=T0, updated_at=T0, origin=origin))


def live(pool, cid, name, files, *, agent="deep", **kw):
    """A promoted, opted-in tool."""
    cand(pool, cid, name, files, **kw)
    with pool.connection() as conn:
        registry.promote(conn, cid, gate_ref="gate:1", by="round", why="w",
                         gate_ok=lambda ref: True, now=T0)
        optin.set_local(conn, "u1", agent, name, True, T0)


def request_on(pool, agent="deep"):
    with pool.connection() as conn:
        optin.set_local(conn, "u1", agent, "request_tool", True, T0)


class Trace:
    def start(self, *a, **kw): ...
    def model(self, *a, **kw): ...
    def tool(self, *a, **kw): ...
    def finish(self, *a, **kw): ...


class Gw:
    """The slice of Gateway the forge uses; records the account of every call. `costs`: the
    prices of the first calls (then the inner's own)."""

    def __init__(self, inner=None, costs=()):
        self.inner = inner or script()
        self.accounts = []
        self.costs = list(costs)

    def converse(self, role, req, *, account=None):
        self.accounts.append(account)
        t, c = self.inner(req)
        if self.costs:
            c = c.model_copy(update={"cost": self.costs.pop(0)})
        return t, c


class Recording(InProcessSandbox):
    def __init__(self):
        self.calls = []

    def run_tool(self, files, args, host=None):
        self.calls.append(args)
        return super().run_tool(files, args, host)


class ReadPage:
    def __init__(self):
        self.ctxs = []
        self.spec = ToolSpec(name="read_page", description="读一个页面的正文", effect="read",
                             parameters={"type": "object", "properties": {}})

    def run(self, args, ctx):
        self.ctxs.append(ctx)
        return ToolResult(content=f"<{args['page_id']}>")


def runtime(pool, gw=None, sandbox=None):
    return ForgeRuntime(pool=pool, gateway=gw or Gw(), sandbox=sandbox or InProcessSandbox(),
                        embedder=HashingEmbedder(dim=256), trace=Trace(), clock=lambda: T0)


def pin(rt, **kw):
    args = {"user_id": "u1", "agent": "deep", "run_id": "r1", "group": "page",
            "pack_values": {}, **kw}
    return rt.for_run(**args)


def names(tools):
    return [t.spec.name for t in tools]


def test_nothing_opted_in_and_request_off_is_none(pool, users):
    cand(pool, "fc_a", "double", DOUBLE)
    assert pin(runtime(pool)) is None


def test_first_class_capped_rest_through_call_tool(pool, users, monkeypatch):
    live(pool, "fc_a", "double", DOUBLE, title="翻倍")
    live(pool, "fc_b", "triple", TRIPLE, title="三倍")
    monkeypatch.setattr(forge_run, "FIRST_CLASS_MAX", 1)
    run = pin(runtime(pool), query="把数变成三倍")
    tools = run.tools([], CTX)
    assert names(tools) == ["triple", "call_tool", "find_tool"]
    assert isinstance(tools[0], SandboxedTool)
    assert [t.spec for t in run.tools([], CTX)] == [t.spec for t in tools]   # fixed for the run
    call = tools[1].run({"name": "double", "args": {"n": 4}}, CTX)
    assert call.error is None and json.loads(call.content) == {"n": 8}
    assert call.summary == "用了「翻倍」"
    unknown = tools[1].run({"name": "nope", "args": {}}, CTX)
    assert unknown.error and "double" in unknown.content and "triple" in unknown.content
    found = tools[2].run({"query": "翻倍"}, CTX)
    first = json.loads(found.content.splitlines()[0])
    assert first["name"] == "double" and first["title"] == "翻倍"
    assert first["parameters"]["properties"] == {"n": {"type": "integer"}}


def test_all_opted_in_first_class_without_find_tool(pool, users):
    live(pool, "fc_a", "double", DOUBLE)
    live(pool, "fc_b", "triple", TRIPLE)
    run = pin(runtime(pool))
    assert names(run.tools([], CTX)) == ["double", "triple", "call_tool"]
    assert run.version == 2 and run.first_class == ("double", "triple")


def test_host_port_runs_builtin_with_ctx_on_the_host(pool, users):
    live(pool, "fc_r", "read_one", READER, host_tools=("read_page",))
    sb = Recording()
    reader = ReadPage()
    run = pin(runtime(pool, sandbox=sb))
    [tool, _call] = run.tools([reader], CTX)
    res = tool.run({"p": "a"}, CTX)
    assert res.error is None and json.loads(res.content) == {"t": "<a>"}
    assert [c.user_id for c in reader.ctxs] == ["u1"]
    assert sb.calls == [{"p": "a"}]                 # the guest got the tool's args only
    # a built-in outside HOST_READ_TOOLS is never a host tool
    other = ReadPage()
    other.spec = ToolSpec(name="write_section", description="写", effect="read",
                          parameters={"type": "object", "properties": {}})
    port = forge_run.host_port([other], "deep", CTX)
    assert port("write_section", {}).ok is False and other.ctxs == []


def request_args(**kw):
    return {"name": "double", "title": "翻倍", "purpose": "把整数 n 翻倍",
            "examples": [{"args": {"n": 2}, "expect": {"n": 4}}], **kw}


def test_request_tool_builds_into_the_run_overlay_only(pool, users):
    request_on(pool)
    gw = Gw()
    run = pin(runtime(pool, gw))
    tools = run.tools([], CTX)
    assert names(tools) == ["call_tool", "request_tool"]
    assert tools[1].spec.category == "draft" and tools[1].spec.effect == "read"
    res = tools[1].run(request_args(), CTX)
    assert res.error is None and "已就绪" in res.content and res.summary == "准备「翻倍」"
    assert all(a == Account(user_id="u1", group="page") for a in gw.accounts) and gw.accounts
    with pool.connection() as conn:
        [c] = store.candidates_by_origin(conn, "u1", "run:r1", statuses=("built",))
        assert c.status == "built" and c.name == "double"
        assert store.registry_head(conn, "u1") == (0, {})          # registry untouched
    call = tools[0].run({"name": "double", "args": {"n": 5}}, CTX)
    assert json.loads(call.content) == {"n": 10}
    again = tools[1].run(request_args(name="triple", title="三倍", purpose="三倍"), CTX)
    assert again.error and "已经准备过" in again.content
    assert names(run.tools([], CTX)) == ["call_tool", "request_tool"]   # list unchanged


def test_request_tool_reuses_a_near_duplicate_without_a_model_call(pool, users, monkeypatch):
    live(pool, "fc_a", "double", DOUBLE, title="翻倍")
    request_on(pool)
    monkeypatch.setattr(forge_run, "REUSE_SIM", 0.3)
    gw = Gw()
    run = pin(runtime(pool, gw))
    req = next(t for t in run.tools([], CTX) if t.spec.name == "request_tool")
    res = req.run(request_args(name="twice", purpose="把 n 翻倍"), CTX)
    assert res.error is None and "已有可用的工具「翻倍」(double)" in res.content
    assert gw.inner.requests == [] and run.requested == 0
    same_name = req.run(request_args(), CTX)
    assert "已有可用的工具" in same_name.content and gw.inner.requests == []


def test_request_tool_unavailable_when_switched_off_mid_run(pool, users):
    request_on(pool)
    run = pin(runtime(pool))
    with pool.connection() as conn:
        switch.set_enabled(conn, "u1", "tool", False)
    req = run.tools([], CTX)[1]
    res = req.run(request_args(), CTX)
    assert res.error and "现在不能生成新工具" in res.content


def test_request_tool_refuses_builtin_names_and_bad_args(pool, users):
    request_on(pool)
    run = pin(runtime(pool))
    req = run.tools([ReadPage()], CTX)[1]
    assert "重名" in req.run(request_args(name="read_page"), CTX).content
    assert req.run(request_args(name="call_tool"), CTX).error
    assert req.run(request_args(host_tools=["web_search"]), CTX).error
    assert run.requested == 0


def test_request_tool_with_host_tools_is_refused_before_any_model_call(pool, users):
    """Model-written examples carry no host recordings: a data-reading tool would always fail its
    build after the coder was paid, so request_tool forges pure tools only (spec §11)."""
    request_on(pool)
    gw = Gw()
    run = pin(runtime(pool, gw))
    req = run.tools([ReadPage()], CTX)[1]
    assert "host_tools" not in req.spec.parameters["properties"]
    res = req.run(request_args(host_tools=["read_page"]), CTX)
    assert res.error and "纯计算" in res.content and "作为参数" in res.content
    assert res.summary == "准备「翻倍」"
    assert gw.inner.requests == [] and run.requested == 0
    with pool.connection() as conn:
        assert store.candidates_by_origin(conn, "u1", "run:r1",
                                          statuses=("built", "build_failed", "building")) == []
    ok = req.run(request_args(host_tools=[]), CTX)          # an empty list is no host data
    assert ok.error is None and "已就绪" in ok.content


def test_request_tool_tells_the_run_what_the_build_cost(pool, users):
    request_on(pool)
    seen = []
    run = pin(runtime(pool, Gw(costs=[0.01, 0.02, 0.03, 0.04])), on_cost=seen.append)
    res = run.tools([], CTX)[1].run(request_args(), CTX)
    assert res.error is None and seen == [pytest.approx(0.10)]


def test_a_failed_build_still_reports_its_cost(pool, users, monkeypatch):
    request_on(pool)
    seen = []
    run = pin(runtime(pool, Gw(costs=[0.01, 0.02, 0.03, 0.04])), on_cost=seen.append)

    def boom(*a, **kw):
        raise RuntimeError("build blew up")

    monkeypatch.setattr("evowiki.forge.api.build", boom)
    res = run.tools([], CTX)[1].run(request_args(), CTX)
    assert res.error and seen == [pytest.approx(0.10)]


def test_pin_and_request_are_durable_steps_and_replay_rebuilds_the_overlay(
        pool, users, monkeypatch):
    request_on(pool)
    gw = Gw()
    real = durable.step
    log = []

    def recording(name, fn):
        out = real(name, fn)
        log.append((name, out))
        return out

    monkeypatch.setattr(durable, "step", recording)
    gw.costs = [0.01, 0.02, 0.03, 0.04]
    with durable.virtual_workflow("wf-forge"):
        run = pin(runtime(pool, gw))
        _call, req = run.tools([], CTX)
        req.run(request_args(), CTX)
    assert [n for n, _ in log] == ["forge.pin", "forge.request_tool"]
    calls = len(gw.inner.requests)

    recorded = iter(log)

    def replay(name, fn):                 # a recovered workflow: recorded values, nothing re-run
        rec_name, value = next(recorded)
        assert rec_name == name
        return json.loads(json.dumps(value))

    monkeypatch.setattr(durable, "step", replay)
    seen = []
    with durable.virtual_workflow("wf-forge"):
        run2 = pin(runtime(pool, gw), on_cost=seen.append)
        call2, req2 = run2.tools([], CTX)
        res = req2.run(request_args(), CTX)
    assert res.error is None and len(gw.inner.requests) == calls
    assert seen == [pytest.approx(0.10)]       # the recorded cost: the page / turn spend survives
    assert run2.requested == 1 and "double" in run2.reachable
    assert json.loads(call2.run({"name": "double", "args": {"n": 1}}, CTX).content) == {"n": 2}


def test_kill_switch_off_offers_nothing(pool, users):
    live(pool, "fc_a", "double", DOUBLE)
    request_on(pool)
    with pool.connection() as conn:
        switch.set_enabled(conn, "u1", "tool", False)
    assert pin(runtime(pool)) is None


def test_candidate_overlay_is_reachable_but_not_first_class(pool, users):
    live(pool, "fc_a", "double", DOUBLE)
    cand(pool, "fc_c", "triple", TRIPLE, title="三倍")
    run = pin(runtime(pool), candidate="fc_c")
    tools = run.tools([], CTX)
    assert names(tools) == ["double", "call_tool", "find_tool"]
    assert json.loads(tools[1].run({"name": "triple", "args": {"n": 2}}, CTX).content) == {"n": 6}


def test_session_overlay_only_for_that_session(pool, users):
    cand(pool, "fc_s", "triple", TRIPLE, origin="session:s1")
    cand(pool, "fc_f", "double", DOUBLE, origin="session:s1", status="build_failed")
    kw = {"agent": "assistant", "group": "assistant"}
    run = pin(runtime(pool), session_id="s1", **kw)
    assert set(run.reachable) == {"triple"} and run.first_class == ()
    assert run.origin == "session:s1"
    assert pin(runtime(pool), session_id="s2", **kw) is None


def test_collision_with_a_builtin_fails_closed_and_loudly(pool, users, caplog):
    live(pool, "fc_x", "read_page", DOUBLE)
    with caplog.at_level(logging.ERROR, logger="evowiki.forge.run"):
        assert pin(runtime(pool)) is None
    assert any("forge pin failed" in r.message for r in caplog.records)


def test_collision_with_an_actual_builtin_the_static_list_misses_fails_closed(pool, users,
                                                                               caplog):
    """`harness.tool_names.TOOLS` can lag the tools an agent really builds: the run checks the
    actual built-ins too and then offers no forged tools at all."""
    live(pool, "fc_x", "mark_source_use", DOUBLE)
    request_on(pool)
    run = pin(runtime(pool))
    other = ReadPage()
    other.spec = ToolSpec(name="mark_source_use", description="标记", effect="read",
                          parameters={"type": "object", "properties": {}})
    with caplog.at_level(logging.ERROR, logger="evowiki.forge.run"):
        assert run.tools([other], CTX) == []
    assert any("collide" in r.message for r in caplog.records)
    assert names(run.tools([ReadPage()], CTX)) == ["mark_source_use", "call_tool",
                                                   "request_tool"]
