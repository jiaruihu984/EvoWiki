from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from evowiki.agent.tools import ToolResult
from evowiki.content.models import Lang
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn, ToolCall, ToolDef
from evowiki.store import trace
from evowiki.store.users import UserRow, create_user

T0 = datetime(2026, 10, 21, 1, 0, tzinfo=UTC)
TOOLS = (ToolDef(name="read_page", description="读一页", parameters={"type": "object"}),)


def _user(pool, uid="u1"):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id=uid, background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))


def _req(msgs, tools=()):
    return ChatRequest(purpose="deep.write", prompt_version="deep@v16", subject="p1",
                       system="SYS", messages=tuple(msgs), tools=tools, tool_choice="auto")


def _count(pool, sql, params=()):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchone()["n"]


def test_run_steps_roundtrip_and_idempotent(pool):
    _user(pool)
    m1 = ChatMessage(role="user", text="写这一页")
    turn = ChatTurn(text="", tool_calls=(ToolCall(id="c1", name="read_page", args={"p": 1}),))
    for _ in range(2):                         # a replay (a fresh sink) sends everything again
        sink = trace.PgTraceSink(pool, clock=lambda: T0)
        sink.start("r1", user_id="u1", agent="deep.write", subject="p1", pack_version=3,
                   input={"page_id": "p1"})
        sink.model("r1", 1, _req([m1]), turn, "llm_x", None)
        sink.tool("r1", 2, "c1", "read_page", {"p": 1}, ToolResult(content="页面内容"))
        sink.finish("r1", stop="finished", error=None)
    with pool.connection() as conn:
        run = trace.run(conn, "r1")
        steps = trace.steps(conn, "r1")
        full = trace.request_at(conn, "r1", 1)
    assert run["pack_version"] == 3 and run["stop"] == "finished"
    assert run["input"] == {"page_id": "p1"} and run["finished_at"] == T0
    assert [s["kind"] for s in steps] == ["model", "tool"]
    assert steps[0]["call_id"] == "llm_x" and steps[0]["turn"]["tool_calls"][0]["id"] == "c1"
    assert steps[1]["result"]["content"] == "页面内容" and steps[1]["tool_call_id"] == "c1"
    assert full.system == "SYS" and full.messages == (m1,)
    assert _count(pool, "select count(*) as n from agent_step") == 2
    assert _count(pool, "select count(*) as n from agent_run") == 1


def test_texts_are_stored_once(pool):
    _user(pool)
    sink = trace.PgTraceSink(pool, clock=lambda: T0)
    m1 = ChatMessage(role="user", text="写这一页")
    for rid in ("r1", "r2"):
        sink.start(rid, user_id="u1", agent="deep.write", subject="p1", pack_version=0, input={})
        sink.model(rid, 1, _req([m1]), None, None, "boom")
    assert _count(pool, "select count(*) as n from llm_text where text = 'SYS'") == 1
    with pool.connection() as conn:
        assert trace.steps(conn, "r2")[0]["error"] == "boom"


def test_request_at_rebuilds_each_request_as_sent(pool):
    """Later requests may rewrite earlier messages (elided tool results): each step rebuilds
    exactly what it was sent, tool definitions included."""
    _user(pool)
    sink = trace.PgTraceSink(pool, clock=lambda: T0)
    call = ToolCall(id="c1", name="read_page", args={"p": 1})
    first = _req([ChatMessage(role="user", text="写这一页")], TOOLS)
    second = _req([*first.messages, ChatMessage(role="assistant", tool_calls=(call,)),
                   ChatMessage(role="tool", tool_call_id="c1", text="长" * 3000)], TOOLS)
    third = _req([*second.messages[:2],
                  ChatMessage(role="tool", tool_call_id="c1", text="（已省略）"),
                  ChatMessage(role="assistant", text="好了", echo={"thinking": "想"})], TOOLS)
    sink.start("r1", user_id="u1", agent="deep.write", subject="p1", pack_version=0, input={})
    for seq, req in ((1, first), (3, second), (5, third)):
        sink.model("r1", seq, req, ChatTurn(text="t"), f"llm_{seq}", None)
    with pool.connection() as conn:
        assert [trace.request_at(conn, "r1", s) for s in (1, 3, 5)] == [first, second, third]


def test_request_at_needs_a_model_step(pool):
    _user(pool)
    sink = trace.PgTraceSink(pool, clock=lambda: T0)
    sink.start("r1", user_id="u1", agent="deep.write", subject="p1", pack_version=0, input={})
    sink.tool("r1", 2, "c1", "read_page", {}, ToolResult(content="x"))
    with pool.connection() as conn, pytest.raises(LookupError):
        trace.request_at(conn, "r1", 2)
    with pool.connection() as conn, pytest.raises(LookupError):
        trace.request_at(conn, "r1", 9)


def test_finishing_an_unknown_run_raises(pool):
    sink = trace.PgTraceSink(pool, clock=lambda: T0)
    with pytest.raises(LookupError):
        sink.finish("nope", stop="finished", error=None)
    with pool.connection() as conn:
        assert trace.run(conn, "nope") is None


def test_request_log_records_once(pool):
    log = trace.PgRequestLog(pool)
    for _ in range(2):
        log.record("llm_1", "系统", "题目", "Outline@1")
    with pool.connection() as conn:
        row = conn.execute("select r.schema_name, s.text as system, p.text as prompt "
                           "from llm_request r join llm_text s on s.hash = r.system_hash "
                           "join llm_text p on p.hash = r.prompt_hash").fetchall()
    assert row == [{"schema_name": "Outline@1", "system": "系统", "prompt": "题目"}]


def _run(sink, rid, subject, text):
    sink.start(rid, user_id="u1", agent="deep.write", subject=subject, pack_version=0, input={})
    sink.model(rid, 1, _req([ChatMessage(role="user", text=text)], TOOLS), ChatTurn(text="t"),
               None, None)


def test_signal_run_ids_links_runs_to_pages_with_a_later_signal(pool):
    _user(pool)
    old = T0 - timedelta(days=40)
    sink = trace.PgTraceSink(pool, clock=lambda: old)
    _run(sink, "r1", "p1", "一")
    _run(sink, "r2", "p2", "二")
    with pool.connection() as conn:
        for seq, at, page in ((1, T0, "p1"), (2, old, "p2")):   # p2's signal is before the window
            conn.execute(
                "insert into wiki_event (user_id, seq, event_id, occurred_at, type, payload) "
                "values ('u1', %s, %s, %s, 'signal_recorded', %s)",
                (seq, f"e{seq}", at, Jsonb({"type": "signal_recorded",
                                            "signal": {"signal_id": f"s{seq}", "page_id": page}})))
        assert trace.signal_run_ids(conn, older_than=T0 - timedelta(days=30)) == {"r1"}


def _deep(sink, rid, subject, agent="deep.write", finish=True):
    sink.start(rid, user_id="u1", agent=agent, subject=subject, pack_version=0,
               input={"page_id": subject, "round": 0})
    if finish:
        sink.finish(rid, stop="finished", error=None)


def test_deep_runs_for_page_are_the_newest_finished_deep_writes_before_a_time(pool):
    _user(pool)
    times = iter(T0 + timedelta(minutes=i) for i in range(20))
    sink = trace.PgTraceSink(pool, clock=lambda: next(times))
    _deep(sink, "a", "p1")                       # T0+0 (finish consumes a tick too)
    _deep(sink, "other-page", "p2")
    _deep(sink, "assistant", "p1", agent="assistant")
    _deep(sink, "bot", "p1", agent="bot.agent")
    _deep(sink, "unfinished", "p1", finish=False)
    _deep(sink, "b", "p1")
    _deep(sink, "c", "p1")
    with pool.connection() as conn:
        got = trace.deep_runs_for_page(conn, "u1", "p1", before=T0 + timedelta(hours=1))
        assert [r["run_id"] for r in got] == ["c", "b", "a"]
        assert set(got[0]) >= {"run_id", "input", "started_at", "stop"}
        assert got[0]["input"] == {"page_id": "p1", "round": 0} and got[0]["stop"] == "finished"
        b_start = next(r["started_at"] for r in got if r["run_id"] == "b")
        before_b = trace.deep_runs_for_page(conn, "u1", "p1", before=b_start)
        assert [r["run_id"] for r in before_b] == ["a"]
        assert [r["run_id"] for r in trace.deep_runs_for_page(
            conn, "u1", "p1", before=T0 + timedelta(hours=1), limit=2)] == ["c", "b"]
        assert trace.deep_runs_for_page(conn, "u2", "p1", before=T0 + timedelta(hours=1)) == []


def test_deep_runs_for_page_leave_out_harness_replays(pool):
    """A replay is a run too, but never a case source (by its run id, or by its job marker)."""
    _user(pool)
    times = iter(T0 + timedelta(minutes=i) for i in range(20))
    sink = trace.PgTraceSink(pool, clock=lambda: next(times))
    _deep(sink, "a", "p1")
    _deep(sink, trace.REPLAY_RUN_PREFIX + "hr1:refine:p1:x:write:0:w0", "p1")
    rid = "marked-only"
    sink.start(rid, user_id="u1", agent="deep.write", subject="p1", pack_version=0,
               input={"page_id": "p1", "job": {"trigger": "harness_replay"}})
    sink.finish(rid, stop="finished", error=None)
    with pool.connection() as conn:
        got = trace.deep_runs_for_page(conn, "u1", "p1", before=T0 + timedelta(hours=1))
    assert [r["run_id"] for r in got] == ["a"]


# --- traced steps (migration 0090, llm/run_context.py) ------------------------------------------

def _json_call(cid, raw='{"v": 1}', served_from=None):
    from evowiki.llm.models import LLMCall
    return LLMCall(id=cid, input_hash="h", purpose="graph.split", prompt_version="split@v5",
                   subject="graph/n1", provider="p", model="m", model_version=None, params={},
                   raw_output=raw, parsed_output={"v": 1}, cost=0.0, latency=0.1, ts=T0,
                   served_from=served_from, replayable=served_from is None)


def test_a_traced_step_stores_its_calls_rejections_and_versions(pool, monkeypatch):
    from evowiki.llm import run_context
    from evowiki.llm.models import LLMRequest
    run_context.code_version.cache_clear()
    monkeypatch.setenv(run_context.COMMIT_ENV, "sha-1")
    _user(pool)
    req = LLMRequest(purpose="graph.split", prompt_version="split@v5", subject="graph/n1",
                     system="SYS", prompt="拆这个点")
    c1, hit = _json_call("llm_a"), _json_call("llm_a_hit1", served_from="llm_a")
    try:
        for _ in range(2):                      # a re-executed workflow body adds nothing
            sink = trace.PgTraceSink(pool, clock=lambda: T0)
            trace.PgRequestLog(pool).record("llm_a", "SYS", "拆这个点", "SplitAnswer")
            sink.start("s1", user_id="u1", agent="step", subject="graph/n1", pack_version=0,
                       input={}, step_key="split")
            sink.call("s1", 1, req, "SplitAnswer", c1, None)
            sink.reject("s1", 2, "只拆出了 1 个子点", c1, {})
            sink.call("s1", 3, req, "SplitAnswer", hit, None)
            sink.call("s1", 4, req, "SplitAnswer", None, "LLMParseError: x")
            sink.chat("s1", 5, _req([ChatMessage(role="user", text="对话")]), ChatTurn(text="摘"),
                      "llm_c", None)
            sink.finish("s1", stop="ok", error=None)
            # a re-execution may number differently: the call ids still dedupe
            sink.call("s1", 9, req, "SplitAnswer", c1, None)
        with pool.connection() as conn:
            run = trace.run(conn, "s1")
            steps = trace.steps(conn, "s1")
    finally:
        run_context.code_version.cache_clear()
    assert (run["step_key"], run["code_version"], run["stop"]) == ("split", "sha-1", "ok")
    assert [s["kind"] for s in steps] == ["call", "reject", "call", "call", "model"]
    first, rej, served, failed, chat = steps
    assert first["call_id"] == "llm_a" and first["prompt"] == "拆这个点" and first["system"] == "SYS"
    assert served["input_call"] == "llm_a" and served["prompt"] == "拆这个点"
    assert rej["reason"] == "只拆出了 1 个子点" and rej["output_hash"] == first["output_hash"]
    assert failed["call_id"] is None and failed["error"] == "LLMParseError: x"
    assert chat["via"] == "gateway" and chat["messages"][0]["text"] == "对话"


def test_a_loop_run_gets_its_agent_as_step_key(pool):
    _user(pool)
    trace.PgTraceSink(pool, clock=lambda: T0).start("r9", user_id="u1", agent="deep.write",
                                                     subject="p1", pack_version=0, input={})
    with pool.connection() as conn:
        assert trace.run(conn, "r9")["step_key"] == "deep.write"
