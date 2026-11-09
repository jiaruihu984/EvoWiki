"""Traced steps (llm/run_context.py, observability plan 2026-10-29 §5 1a): every model call of a
user is a step of a run; rejections after parsing are steps too; agent loops share their numbers."""
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from evowiki.agent.trace import current_run_id, reject, traced_step
from evowiki.llm import run_context
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatResponse, ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef, ProviderResponse
from evowiki.llm.prices import PriceTable
from evowiki.llm.usage import Account, NullMeter
from evowiki.llm.usage_control import UsageControl

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
CFG = GatewayConfig(generator=ModelRef(provider="gen", model="g-1"),
                    verifier=ModelRef(provider="ver", model="v-1"))
U1 = Account(user_id="u1", group="page")


class Answer(BaseModel):
    value: int


class Store:
    """An in-memory StepStore: every write as a tuple."""

    def __init__(self):
        self.rows: list[tuple] = []

    def start(self, run_id, *, user_id, agent, subject, pack_version, input, step_key=None):
        self.rows.append(("start", run_id, user_id, agent, subject, step_key, input))

    def chat(self, run_id, seq, request, turn, call_id, error):
        self.rows.append(("chat", run_id, seq, request.purpose, call_id, error))

    def call(self, run_id, seq, request, schema_name, call, error):
        self.rows.append(("call", run_id, seq, request.purpose, call.id if call else None,
                          error))

    def reject(self, run_id, seq, reason, call, detail):
        self.rows.append(("reject", run_id, seq, reason, call.id if call else None, detail))

    def finish(self, run_id, *, stop, error):
        self.rows.append(("finish", run_id, stop, error))

    def kinds(self):
        return [r[0] for r in self.rows]


@pytest.fixture
def store():
    s = Store()
    run_context.configure(s)
    yield s
    run_context.configure(None)


class Provider:
    def __init__(self, script):
        self.script = list(script)

    def complete(self, model, request, json_schema):
        out = self.script.pop(0)
        if isinstance(out, Exception):
            raise out
        return ProviderResponse(raw_output=out, model_version=model)

    def chat(self, model, request):
        return ChatResponse(turn=ChatTurn(text=self.script.pop(0)), model_version=model)


def _gw(tmp_path, *script):
    return Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                   providers={"gen": Provider(script)}, clock=lambda: T0,
                   usage=UsageControl(NullMeter(), PriceTable({})))


def _req(purpose="graph.split", **over):
    return LLMRequest(**({"purpose": purpose, "prompt_version": "v1", "subject": "graph/n1",
                          "system": "s", "prompt": "p"} | over))


def test_without_a_store_nothing_is_traced(tmp_path):
    gw = _gw(tmp_path, '{"value": 1}')
    with traced_step("split", "graph/n1", "u1") as step:
        _, call = gw.call("generator", _req(), Answer, account=U1)
        assert step.run_id is None and current_run_id() is None
    assert call.run_id is None


def test_a_step_is_a_run_with_its_calls_and_rejections(tmp_path, store):
    gw = _gw(tmp_path, '{"value": 1}', '{"value": 2}')
    with traced_step("split", "graph/n1", "u1", input={"node": "n1"}) as step:
        rid = step.run_id
        assert current_run_id() == rid
        _, c1 = gw.call("generator", _req(), Answer, account=U1)
        reject("只拆出了 1 个子点", why="too few")
        _, c2 = gw.call("generator", _req(prompt="p2"), Answer, account=U1)
    assert current_run_id() is None
    assert rid.startswith("split:") and c1.run_id == rid == c2.run_id
    assert store.rows == [
        ("start", rid, "u1", "step", "graph/n1", "split", {"node": "n1"}),
        ("call", rid, 1, "graph.split", c1.id, None),
        ("reject", rid, 2, "只拆出了 1 个子点", c1.id, {"why": "too few"}),
        ("call", rid, 3, "graph.split", c2.id, None),
        ("finish", rid, "ok", None)]


def test_a_step_that_ends_on_a_rejection_says_so(tmp_path, store):
    gw = _gw(tmp_path, '{"value": 1}')
    with traced_step("split", "graph/n1", "u1") as step:
        _, call = gw.call("generator", _req(), Answer, account=U1)
        step.reject("关系绕成了圈", call)
    assert store.rows[-1] == ("finish", step.run_id, "rejected: 关系绕成了圈", None)


def test_a_raising_step_finishes_with_its_error_and_reraises(tmp_path, store):
    with pytest.raises(ValueError, match="boom"), traced_step("path", "goal", "u1"):
        raise ValueError("boom")
    assert store.rows[-1][2:] == (None, "ValueError: boom")


def test_a_failed_call_is_a_step(tmp_path, store):
    gw = _gw(tmp_path, "not json", "not json", "not json")
    with pytest.raises(ValueError), traced_step("split", "graph/n1", "u1"):
        gw.call("generator", _req(), Answer, account=U1)
    call = [r for r in store.rows if r[0] == "call"]
    assert len(call) == 1 and call[0][4] is None and call[0][5].startswith("LLMParseError")


def test_the_same_step_joins_and_another_nests(tmp_path, store):
    with traced_step("split", "graph/n1", "u1") as outer:
        with traced_step("split", "graph/n1", "u1") as inner:
            assert inner.run_id == outer.run_id
        with traced_step("path", "goal", "u1") as other:
            assert other.run_id.startswith(f"{outer.run_id}:path:")    # deleted with it
    starts = [r for r in store.rows if r[0] == "start"]
    assert len(starts) == 2 and starts[1][6] == {"parent_run": outer.run_id}


def test_a_call_outside_a_step_gets_a_run_of_its_own(tmp_path, store):
    gw = _gw(tmp_path, '{"value": 1}')
    _, call = gw.call("generator", _req(purpose="goals.path"), Answer, account=U1)
    assert store.kinds() == ["start", "call", "finish"]
    assert store.rows[0][5] == "path" and store.rows[0][6] == {"auto": True}
    assert call.run_id == store.rows[0][1]


def test_a_call_without_a_user_outside_a_step_is_not_traced(tmp_path, store):
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"gen": Provider(['{"value": 1}'])}, clock=lambda: T0)
    gw.call("generator", _req(), Answer)
    assert store.rows == []


def test_a_chat_call_outside_a_loop_keeps_its_messages(tmp_path, store):
    gw = _gw(tmp_path, "摘要")
    req = ChatRequest(purpose="assistant.compact", prompt_version="compact@v6", subject="s1",
                      system="sys", messages=(ChatMessage(role="user", text="很长的对话"),))
    _, call = gw.converse("generator", req, account=U1)
    assert store.kinds() == ["start", "chat", "finish"]
    assert store.rows[0][5] == "compact" and store.rows[1][4] == call.id


def test_a_tool_chat_call_inside_a_loop_is_a_step_and_the_loops_own_turn_is_not(tmp_path, store):
    """Live 2026-11-04: read_source's vision calls had an llm_call and no step, so no input anywhere."""
    gw = _gw(tmp_path, "这页的文字", "回答")
    run = run_context.loop_run("r1", "u1", "assistant.turn", store)
    req = ChatRequest(purpose="source.vision", prompt_version="v", subject="p1", system="sys",
                      messages=(ChatMessage(role="user", text="读这页"),))
    with run_context.binding(run):
        _, tool_call = gw.converse("generator", req, account=U1)
        with run_context.own_turn():
            gw.converse("generator", req.model_copy(update={"purpose": "assistant.turn", "subject": "p2"}), account=U1)
    assert [r[0] for r in store.rows] == ["chat"] and store.rows[0][4] == tool_call.id


def test_inside_a_step_a_call_without_an_account_is_keyed_by_the_run_user(tmp_path, store):
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"gen": Provider(['{"value": 1}', '{"value": 2}'])}, clock=lambda: T0)
    with traced_step("split", "graph/n1", "u1"):
        _, a = gw.call("generator", _req(), Answer)
    with traced_step("split", "graph/n1", "u2"):
        _, b = gw.call("generator", _req(), Answer)
    assert (a.user_id, b.user_id) == ("u1", "u2") and a.input_hash != b.input_hash


def test_a_step_can_live_under_a_session_prefix(store):
    with traced_step("compact", "s1", "u1", under="assistant:s1") as step:
        assert step.run_id.startswith("assistant:s1:compact:")


def test_a_traced_generator_is_bound_only_while_it_runs(tmp_path, store):
    from evowiki.llm.run_context import traced_entry
    seen = []

    @traced_entry("write", subject="page_id")
    def stream(user_id, page_id):
        seen.append(current_run_id())
        yield 1
        seen.append(current_run_id())
        return "done"

    gen = stream("u1", "p1")
    assert next(gen) == 1 and current_run_id() is None
    with pytest.raises(StopIteration):
        next(gen)
    assert seen[0] == seen[1] and seen[0].startswith("write:")
    assert store.rows[0][4:6] == ("p1", "write") and store.rows[-1][2] == "ok"


def test_code_version_is_pinned_by_the_environment(monkeypatch):
    run_context.code_version.cache_clear()
    monkeypatch.setenv(run_context.COMMIT_ENV, "abc123")
    try:
        assert run_context.code_version() == "abc123"
    finally:
        run_context.code_version.cache_clear()
