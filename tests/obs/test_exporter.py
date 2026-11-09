"""The OTel export to Phoenix (obs/): metadata only, and Phoenix follows Postgres's deletes.
Real Postgres; the span exporter is the SDK's in-memory one and Phoenix's deletes a fake: no
network."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from evowiki.agent.tools import ToolResult
from evowiki.content.models import Lang
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn, ToolCall
from evowiki.obs import spans as sp
from evowiki.obs.exporter import ExportConfig, Exporter
from evowiki.obs.phoenix import PhoenixError
from evowiki.store.trace import PgTraceSink
from evowiki.store.users import UserRow, create_user

T0 = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
SECRET = "我其实一直没懂极限的定义"      # the learner's words: must never reach Phoenix
REPLY = "先看一个数列的例子"
TOOL_OUT = "页面原文：极限是……"


class FakePhoenix:
    def __init__(self) -> None:
        self.traces: list[str] = []
        self.sessions: list[str] = []
        self.down = False

    def delete_trace(self, trace_id: str) -> None:
        if self.down:
            raise PhoenixError("delete trace: HTTP 503")
        self.traces.append(trace_id)

    def delete_sessions(self, session_keys) -> None:
        if self.down:
            raise PhoenixError("delete sessions: HTTP 503")
        self.sessions += list(session_keys)


class Clock:
    def __init__(self) -> None:
        self.now = T0 + timedelta(hours=1)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def world(pool):
    with pool.connection() as conn:
        for uid in ("u1", "u2"):
            create_user(conn, UserRow(user_id=uid, background="none", condition="S2",
                                      lang=Lang.ZH, created_at=T0))
        for sid, uid in (("s1", "u1"), ("s2", "u1"), ("s9", "u2")):
            conn.execute("insert into assistant_session (session_id, user_id, created_at) "
                         "values (%s, %s, %s)", (sid, uid, T0))
    return pool


def _call(pool, cid: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "insert into llm_call (id, input_hash, purpose, prompt_version, subject, provider, "
            "model, params, raw_output, cost, latency, ts) values (%s, %s, 'assistant.turn', "
            "'assistant@v9', 's1', 'deepseek', 'deepseek-chat', '{}', %s, 0.002, 1.5, %s)",
            (cid, f"h-{cid}", REPLY, T0))


def _run(pool, run_id: str, *, user="u1", finish=True, at=T0, parent=None) -> None:
    sink = PgTraceSink(pool, clock=lambda: at)
    extra = {"parent_run": parent} if parent else {}
    sink.start(run_id, user_id=user, agent="assistant", subject="s1", pack_version=4,
               input={"session_id": run_id.split(":")[1] if run_id.startswith("assistant:")
                      else None, "question": SECRET, **extra})
    cid = "c-" + sp.trace_id(run_id)[:8]
    _call(pool, cid)
    req = ChatRequest(purpose="assistant.turn", prompt_version="assistant@v9", subject="s1",
                      system="SYSTEM PROMPT", messages=(ChatMessage(role="user", text=SECRET),),
                      tool_choice="auto")
    turn = ChatTurn(text=REPLY, tool_calls=(ToolCall(id="t1", name="read_page",
                                                     args={"q": SECRET}),),
                    input_tokens=120, output_tokens=30)
    sink.model(run_id, 1, req, turn, cid, None)
    sink.tool(run_id, 2, "t1", "read_page", {"q": SECRET}, ToolResult(content=TOOL_OUT))
    if finish:
        sink.finish(run_id, stop="finished", error=None)


def _exporter(pool, clock=None, spans=None, phoenix=None):
    cfg = ExportConfig(endpoint="http://phoenix.test", link_base="http://evowiki.test")
    spans = spans or InMemorySpanExporter()
    phoenix = phoenix or FakePhoenix()
    return Exporter(pool, cfg, spans=spans, deleter=phoenix, clock=clock or Clock()), spans, phoenix


def _ledger(pool) -> dict[str, str]:
    with pool.connection() as conn:
        return {r["trace_id"]: r["state"] for r in
                conn.execute("select trace_id, state from otel_export").fetchall()}


def _forget_request(pool, session: str) -> str:
    with pool.connection() as conn:
        return conn.execute("select evowiki_forget_request('u1', %s, null, 'api:test') as r",
                            (session,)).fetchone()["r"]


def test_off_unless_an_endpoint_is_configured():
    assert ExportConfig.from_env({}) is None
    assert ExportConfig.from_env({"EVOWIKI_OTEL_ENDPOINT": "  "}) is None
    cfg = ExportConfig.from_env({"EVOWIKI_OTEL_ENDPOINT": "http://127.0.0.1:6006/",
                                 "EVOWIKI_OTEL_RETENTION_DAYS": "3"})
    assert cfg.endpoint == "http://127.0.0.1:6006" and cfg.retention_days == 3
    assert cfg.project == "evowiki"


def test_a_finished_run_is_one_trace_of_metadata_only(world):
    _run(world, "assistant:s1:m1")
    ex, spans, _ = _exporter(world)
    assert ex.tick().exported == 1
    got = spans.get_finished_spans()
    tid = sp.trace_id("assistant:s1:m1")
    assert {format(s.context.trace_id, "032x") for s in got} == {tid}
    root, model, tool = sorted(got, key=lambda s: s.attributes.get("evowiki.seq", 0))
    assert root.parent is None and model.parent.span_id == tool.parent.span_id \
        == root.context.span_id
    assert root.attributes["openinference.span.kind"] == "AGENT"
    assert root.attributes["evowiki.pack_version"] == 4
    assert root.attributes["evowiki.stop"] == "finished"
    assert root.attributes["session.id"] == sp.session_key("s1")
    assert root.attributes["user.id"] == "u1"
    assert root.attributes["evowiki.trace_url"] == f"http://evowiki.test/wiki/traces/otel/{tid}"
    assert root.attributes["llm.token_count.total"] == 150
    assert model.attributes["llm.model_name"] == "deepseek-chat"
    assert model.attributes["evowiki.prompt_version"] == "assistant@v9"
    assert model.attributes["evowiki.cost_usd"] == 0.002
    assert model.attributes["evowiki.input_hash"] == "h-c-" + tid[:8]
    assert (model.end_time - model.start_time) == 1_500_000_000 or model.start_time == \
        root.start_time
    assert tool.attributes["tool.name"] == "read_page"
    # nothing Phoenix gets holds a text: not the question, the reply, the tool's args or result,
    # the system prompt, the subject or a raw EvoWiki id
    dump = json.dumps([{**dict(s.attributes), "name": s.name,
                        "res": dict(s.resource.attributes)} for s in got], ensure_ascii=False)
    for raw in (SECRET, REPLY, TOOL_OUT, "SYSTEM PROMPT", "assistant:s1"):
        assert raw not in dump
    assert _ledger(world) == {tid: "exported"}
    assert ex.tick().exported == 0                       # sent once


def test_snapshot_deleted_by_completed_forget_is_not_exported(world, monkeypatch):
    from evowiki.store import otel_export as store

    _run(world, "assistant:s1:m1")
    ex, spans, _ = _exporter(world)
    read = store.runs_to_export
    raced = False

    def captured_then_deleted(conn, **kwargs):
        nonlocal raced
        rows = read(conn, **kwargs)
        if rows and not raced:
            raced = True
            rid = _forget_request(world, "s1")
            with world.connection() as other:
                other.execute("select evowiki_forget_open(%s)", (rid,))
                other.execute("delete from agent_step where run_id='assistant:s1:m1'")
                other.execute("delete from agent_run where run_id='assistant:s1:m1'")
                other.execute("select evowiki_forget_close(%s)", (rid,))
        return rows

    monkeypatch.setattr(store, "runs_to_export", captured_then_deleted)
    ex.tick()
    assert raced
    assert spans.get_finished_spans() == ()


def test_export_send_keeps_the_generation_fence_until_it_finishes(world):
    class CheckedSpans(InMemorySpanExporter):
        def export(self, spans):
            with world.connection() as conn:
                row = conn.execute("select pg_try_advisory_xact_lock(hashtext(%s)) as held",
                                   ("memory:generation:u1",)).fetchone()
                assert row["held"] is False
            return super().export(spans)

    _run(world, "assistant:s1:m1")
    ex, spans, _ = _exporter(world, spans=CheckedSpans())
    assert ex.tick().exported == 1
    assert spans.get_finished_spans()


def test_an_unfinished_run_waits_until_it_is_stale(world):
    _run(world, "deep:p1:j1", finish=False)
    clock = Clock()
    ex, spans, _ = _exporter(world, clock)
    assert ex.tick().exported == 0
    clock.now = T0 + timedelta(hours=7)
    assert ex.tick().exported == 1
    assert spans.get_finished_spans()[0].attributes["evowiki.unfinished"] is True


def test_errors_and_stops_export_their_type_only():
    assert sp.reason("SplitRefused: 这两点其实是一回事") == "SplitRefused"
    assert sp.reason("模型说：我不知道") == "other"
    assert sp.reason("max_steps") == "max_steps"
    assert sp.reason(None) is None


def test_an_open_forget_request_stops_the_export_and_deletes_what_it_hides(world):
    _run(world, "assistant:s1:m1")
    _run(world, "assistant:s2:m1")
    ex, spans, phoenix = _exporter(world)
    ex.tick()
    rid = _forget_request(world, "s1")
    _run(world, "assistant:s2:m2")              # a new run of the same user: held back
    _run(world, "assistant:s9:m1", user="u2")   # another user's run: exported
    spans.clear()
    report = ex.tick()
    # The durable closure is not complete yet: temporarily retract all uncertain traces
    # of this user. This deletes exported metadata, not the unrelated original run.
    assert set(phoenix.traces) == {sp.trace_id("assistant:s1:m1"), sp.trace_id("assistant:s2:m1")}
    assert set(phoenix.sessions) == {sp.session_key("s1"), sp.session_key("s2")}
    assert {format(s.context.trace_id, "032x") for s in spans.get_finished_spans()} == {
        sp.trace_id("assistant:s9:m1")}
    assert report.deleted == 2
    assert sp.trace_id("assistant:s1:m1") not in _ledger(world)
    assert sp.trace_id("assistant:s2:m1") not in _ledger(world)
    with world.connection() as conn:
        assert conn.execute("select 1 from agent_run where run_id='assistant:s2:m1'").fetchone()
    # Simulate the hard-delete transaction finishing: only the affected original run goes.
    with world.connection() as conn, conn.transaction():
        conn.execute("select evowiki_forget_open(%s)", (rid,))
        conn.execute("delete from agent_step where run_id='assistant:s1:m1'")
        conn.execute("delete from agent_run where run_id='assistant:s1:m1'")
        conn.execute("select evowiki_forget_close(%s)", (rid,))
    assert ex.tick().exported == 2
    assert sp.trace_id("assistant:s2:m1") in _ledger(world)
    assert sp.trace_id("assistant:s2:m2") in _ledger(world)
    assert sp.trace_id("assistant:s1:m1") not in _ledger(world)


def test_a_deleted_run_is_deleted_in_phoenix(world):
    _run(world, "deep:p1:j1")
    ex, _, phoenix = _exporter(world)
    ex.tick()
    with world.connection() as conn:          # what a forget commit or the clean-up does
        conn.execute("delete from agent_step where run_id = 'deep:p1:j1'")
        conn.execute("delete from agent_run where run_id = 'deep:p1:j1'")
    assert ex.tick().deleted == 1
    assert phoenix.traces == [sp.trace_id("deep:p1:j1")] and _ledger(world) == {}


def test_a_session_left_with_no_trace_is_deleted_in_phoenix(world):
    _run(world, "assistant:s1:m1")
    _run(world, "assistant:s1:m2")
    ex, _, phoenix = _exporter(world)
    ex.tick()
    with world.connection() as conn:
        conn.execute("delete from agent_step where run_id = 'assistant:s1:m1'")
        conn.execute("delete from agent_run where run_id = 'assistant:s1:m1'")
    ex.tick()
    assert phoenix.sessions == []                # m2 is still there
    with world.connection() as conn:
        conn.execute("delete from agent_step where run_id = 'assistant:s1:m2'")
        conn.execute("delete from agent_run where run_id = 'assistant:s1:m2'")
    ex.tick()
    assert phoenix.sessions == [sp.session_key("s1")]
    assert _ledger(world) == {}


def test_traces_older_than_the_retention_are_deleted_and_not_exported(world):
    _run(world, "deep:p1:j1")
    clock = Clock()
    ex, spans, phoenix = _exporter(world, clock)
    ex.tick()
    clock.now = T0 + timedelta(days=8)
    _run(world, "deep:p0:old", at=T0 - timedelta(days=9))
    spans.clear()
    ex.tick()
    assert phoenix.traces == [sp.trace_id("deep:p1:j1")]
    assert spans.get_finished_spans() == () and _ledger(world) == {}


def test_a_delete_phoenix_refuses_is_kept_and_tried_again(world):
    _run(world, "deep:p1:j1")
    ex, _, phoenix = _exporter(world)
    ex.tick()
    with world.connection() as conn:
        conn.execute("delete from agent_step where run_id = 'deep:p1:j1'")
        conn.execute("delete from agent_run where run_id = 'deep:p1:j1'")
    phoenix.down = True
    assert ex.tick().failed == 1 and _ledger(world) != {}
    phoenix.down = False
    assert ex.tick().deleted == 1 and _ledger(world) == {}


def test_while_phoenix_is_down_a_hidden_trace_stays_listed_and_nothing_is_sent(world):
    _run(world, "assistant:s1:m1")
    ex, spans, phoenix = _exporter(world)
    ex.tick()
    _forget_request(world, "s1")
    _run(world, "assistant:s2:m1")
    phoenix.down = True
    spans.clear()
    assert ex.tick().failed >= 1
    assert sp.trace_id("assistant:s1:m1") in _ledger(world)
    assert spans.get_finished_spans() == ()
    phoenix.down = False
    ex.tick()
    assert sp.trace_id("assistant:s1:m1") not in _ledger(world)
    assert phoenix.traces == [sp.trace_id("assistant:s1:m1")]


def test_a_failed_send_is_sent_again(world):
    class Down(InMemorySpanExporter):
        def __init__(self):
            super().__init__()
            self.fail = True

        def export(self, spans):
            if self.fail:
                return SpanExportResult.FAILURE
            return super().export(spans)

    _run(world, "deep:p1:j1")
    down = Down()
    ex, _, _ = _exporter(world, spans=down)
    assert ex.tick().failed == 1
    assert _ledger(world) == {sp.trace_id("deep:p1:j1"): "sent"}
    down.fail = False
    assert ex.tick().exported == 1
    assert _ledger(world) == {sp.trace_id("deep:p1:j1"): "exported"}


def test_artifacts_of_the_run_ride_on_its_span(world, admin_pool):
    """The provenance tables are migration 0091's (session B); the parents are artifacts too."""
    with admin_pool.connection() as conn:
        conn.execute("insert into artifact values ('a1', 'u1', 'section', %s, 'vh1', "
                     "'deep:p1:j1', %s), ('n7', 'u1', 'node', 'n7', 'vn', null, %s), "
                     "('o3', 'u1', 'outline', 'p1', 'vo', null, %s)", (SECRET, T0, T0, T0))
        conn.execute("insert into artifact_dependency values ('a1', 'n7', 'node'), "
                     "('a1', 'o3', 'outline')")
    _run(world, "deep:p1:j1")
    ex, spans, _ = _exporter(world)
    ex.tick()
    root = next(s for s in spans.get_finished_spans() if s.parent is None)
    assert root.attributes["evowiki.artifact_ids"] == ("a1",)
    [ev] = root.events
    assert ev.attributes["artifact.kind"] == "section"
    assert ev.attributes["artifact.parent_ids"] == ("n7", "o3")
    assert SECRET not in json.dumps(dict(ev.attributes), ensure_ascii=False)


def _gone(pool, *run_ids):
    with pool.connection() as conn:
        conn.execute("delete from agent_step where run_id = any(%s)", (list(run_ids),))
        conn.execute("delete from agent_run where run_id = any(%s)", (list(run_ids),))


def test_a_nested_run_is_in_its_parents_session_and_follows_its_deletes(world):
    _run(world, "assistant:s1:m1")
    _run(world, "step:compact:x1", parent="assistant:s1:m1")
    ex, spans, phoenix = _exporter(world)
    assert ex.tick().exported == 2
    nested = next(s for s in spans.get_finished_spans()
                  if s.parent is None and s.attributes["evowiki.agent"] == "assistant"
                  and s.attributes.get("evowiki.parent_trace_id"))
    assert nested.attributes["session.id"] == sp.session_key("s1")
    assert nested.attributes["evowiki.parent_trace_id"] == sp.trace_id("assistant:s1:m1")
    assert format(nested.links[0].context.trace_id, "032x") == sp.trace_id("assistant:s1:m1")
    # the request hides the turn: the nested run goes with it, not only the turn's own run
    _forget_request(world, "s1")
    ex.tick()
    assert set(phoenix.traces) == {sp.trace_id("assistant:s1:m1"), sp.trace_id("step:compact:x1")}
    assert _ledger(world) == {}


def test_a_run_nested_in_a_deleted_run_is_deleted_and_never_sent_again(world):
    _run(world, "assistant:s1:m1")
    _run(world, "step:compact:x1", parent="assistant:s1:m1")
    ex, spans, phoenix = _exporter(world)
    ex.tick()
    _gone(world, "assistant:s1:m1")          # the parent went, the nested run was left behind
    spans.clear()
    ex.tick()
    assert sp.trace_id("step:compact:x1") in phoenix.traces and _ledger(world) == {}
    ex.tick()
    assert spans.get_finished_spans() == ()


def test_call_and_reject_steps_export_metadata_only():
    run = {"run_id": "step:split:r1", "user_id": "u1", "agent": "step", "step_key": "split",
           "code_version": "abc123+dirty", "pack_version": 0, "started_at": T0.isoformat(),
           "finished_at": (T0 + timedelta(seconds=3)).isoformat(),
           "stop": f"rejected: {SECRET}", "error": None,
           "input": {"auto": True, "parent_run": "assistant:s1:m1", "question": SECRET}}
    call = {"id": "llm_1", "purpose": "graph.split", "prompt_version": "split@v5",
            "provider": "deepseek", "model": "deepseek-chat", "cost": 0.0, "latency": 0.0,
            "replayable": False, "served_from": "llm_0", "input_hash": "ih", "user_id": "u1"}
    steps = [
        {"seq": 1, "kind": "call", "call_id": "llm_1", "ts": T0 + timedelta(seconds=1),
         "call": call, "input_tokens": None, "output_tokens": None,
         "payload": {"purpose": "graph.split", "subject": SECRET, "prompt_version": "split@v5",
                     "schema": "SplitOut", "error": None, "input_call": "llm_0",
                     "served_from": "llm_0", "output_hash": "oh1"}},
        {"seq": 2, "kind": "reject", "call_id": "llm_1", "ts": T0 + timedelta(seconds=2),
         "call": call, "payload": {"reason": SECRET, "detail": {"why": SECRET},
                                   "output_hash": "oh1"}},
        {"seq": 3, "kind": "call", "call_id": "llm_2", "ts": T0 + timedelta(seconds=3),
         "call": {**call, "id": "llm_2", "served_from": None, "cost": 0.01},
         "input_tokens": 200, "output_tokens": 50,
         "payload": {"purpose": "graph.split", "subject": SECRET, "schema": "SplitOut",
                     "error": f"ValueError: {SECRET}", "output_hash": "oh2"}},
    ]
    got = sp.run_spans(run, steps, [], resource=sp.resource("evowiki"), link_base="http://x")
    root, first, rej, second = got
    assert root.name == "split" and root.attributes["evowiki.code_version"] == "abc123+dirty"
    assert root.attributes["evowiki.stop"] == "rejected"
    assert root.attributes["evowiki.auto"] is True
    assert "session.id" not in root.attributes      # the parent's session comes from lineage
    assert root.attributes["llm.token_count.total"] == 250
    assert first.attributes["evowiki.served_from"] == "llm_0"
    assert first.attributes["evowiki.schema"] == "SplitOut"
    assert first.attributes["evowiki.output_hash"] == "oh1"
    assert rej.attributes["evowiki.reject_reason"] == "other"
    assert second.attributes["evowiki.error_type"] == "ValueError"
    assert second.attributes["llm.token_count.total"] == 250
    dump = json.dumps([dict(s.attributes) for s in got] + [s.name for s in got],
                      ensure_ascii=False)
    assert SECRET not in dump
