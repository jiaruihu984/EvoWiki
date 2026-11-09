from datetime import UTC, datetime

import pytest

from evowiki.agent.loop import run_loop
from evowiki.agent.spec import AgentSpec, LoopLimits
from evowiki.agent.tools import ToolContext
from evowiki.harness.pack import EMPTY, HarnessPack
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.prices import PriceTable
from evowiki.llm.scripted import ScriptedChatProvider, ScriptedProvider
from evowiki.llm.usage_control import UsageControl
from evowiki.store import trace
from tests.harness.conftest import ACC, CFG, Out, _Meter, request
from tests.runtime.helpers import make_user


def test_no_source_is_empty(make_gateway):
    assert make_gateway().harness_for("u1") is EMPTY
    gw = make_gateway(harness=lambda uid: HarnessPack(1, {"H1.write": "x"}))
    assert gw.harness_for(None) is EMPTY


def test_guidance_comes_from_pack(make_gateway):
    gw = make_gateway(harness=lambda uid: HarnessPack(4, {"H1.decompose": "只说具体，不缩小范围。"}))
    assert gw.harness_for("u1").version == 4
    _, call = gw.call("generator", request(), Out, account=ACC)
    sent = gw.provider.calls[0]
    assert sent.prompt == "目标：x\n只说具体，不缩小范围。\n只回复 JSON。"
    assert sent.system == "只回复 JSON。只说具体，不缩小范围。"
    assert sent.prompt_version == call.prompt_version == "intake@v30+user"


def test_both_sources_refused(make_gateway):
    with pytest.raises(ValueError):
        make_gateway(harness=lambda uid: EMPTY, overrides=lambda uid: {})


class Requests:
    def __init__(self):
        self.rows = []

    def record(self, call_id, system, prompt, schema_name, *, source_user_id=None, source_nodes=()):
        self.rows.append((call_id, system, prompt, schema_name))


def test_a_new_call_records_what_it_sent(make_gateway):
    log = Requests()
    gw = make_gateway(requests=log)
    _, call = gw.call("generator", request(), Out)
    sent = gw.provider.calls[0]
    assert log.rows == [(call.id, sent.system, sent.prompt, "Out")]
    gw.call("generator", request(), Out)          # a cache replay: nothing new
    assert len(log.rows) == 1


def test_the_input_is_recorded_before_the_call_is_appended(tmp_path):
    """A failed llm_call append never loses what was sent: the request row is already there."""
    log = Requests()

    class BrokenLog(CallLog):
        def append(self, call):
            assert len(log.rows) == 1          # recorded first
            raise RuntimeError("call store down")

    prov = ScriptedProvider("scripted", lambda m, r: '{"text": "好"}')
    gw = Gateway(CFG, BrokenLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"scripted": prov}, requests=log)
    with pytest.raises(RuntimeError, match="call store down"):
        gw.call("generator", request(), Out)
    [sent] = prov.calls
    [(_, system, prompt, schema)] = log.rows
    assert (system, prompt, schema) == (sent.system, sent.prompt, "Out")


def test_a_redacted_call_is_recorded_redacted(make_gateway, pool):
    gw = make_gateway(requests=trace.PgRequestLog(pool))
    req = request().model_copy(update={"prompt": "目标：口令 s3cret-42\n只回复 JSON。",
                                       "redact": ("s3cret-42",)})
    _, call = gw.call("generator", req, Out)
    assert "s3cret-42" in gw.provider.calls[0].prompt      # sent as is
    with pool.connection() as conn:
        texts = [r["text"] for r in conn.execute("select text from llm_text").fetchall()]
        [row] = conn.execute("select call_id from llm_request").fetchall()
    assert row["call_id"] == call.id
    assert texts and not any("s3cret-42" in t for t in texts)
    assert any("[client context redacted]" in t for t in texts)


def test_a_guided_call_records_the_guided_input(make_gateway):
    log = Requests()
    gw = make_gateway(harness=lambda uid: HarnessPack(4, {"H1.decompose": "只说具体。"}),
                      requests=log)
    gw.call("generator", request(), Out, account=ACC)
    sent = gw.provider.calls[0]
    [(_, system, prompt, _)] = log.rows
    assert (system, prompt) == (sent.system, sent.prompt)
    assert "只说具体。" in prompt and "⟦g:" not in system + prompt


def test_the_trace_has_the_request_as_sent(pool, tmp_path):
    """A loop turn through a gateway with the user's H1 guidance: the traced request is the one
    the provider got (override text, no markers, prompt_version +user), so phase 2 replays it."""
    prov = ScriptedChatProvider("scripted", lambda m, r: ChatTurn(text="好"))
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov},
                 usage=UsageControl(meter=_Meter(), prices=PriceTable({})),
                 harness=lambda uid: HarnessPack(4, {"H1.decompose": "只说具体。"}))
    sink = trace.PgTraceSink(pool, clock=lambda: datetime(2026, 10, 21, tzinfo=UTC))
    make_user(pool, "u1")
    sink.start("r1", user_id="u1", agent="t", subject="s", pack_version=4, input={})
    spec = AgentSpec(
        name="t", tools=(), limits=LoopLimits(max_tool_calls=1, on_cap="stop", text_is_answer=True),
        request=lambda s: ChatRequest(purpose="t", prompt_version="t@v1", subject="s",
                                      system="规则。⟦g:decompose⟧默认的话。⟦/g⟧",
                                      messages=s.messages, tool_choice=s.tool_choice),
        sent=lambda r: gw.as_sent(r, ACC), trace=sink, run_id="r1")
    gen = run_loop(spec, lambda r: gw.converse("generator", r, account=ACC),
                   ToolContext(user_id="u1", session_id="s"),
                   [ChatMessage(role="user", text="问")])
    with pytest.raises(StopIteration):
        next(gen)
    with pool.connection() as conn:
        got = trace.request_at(conn, "r1", 1)
    [sent] = prov.chat_calls
    assert got == sent
    assert "只说具体。" in got.system and "默认的话" not in got.system and "⟦g:" not in got.system
    assert got.prompt_version == "t@v1+user"


def test_as_sent_uses_the_pinned_guidance(make_gateway):
    """`as_sent` with the run's pinned guidance never reads the live source; without an account
    nothing is applied, pinned or not."""
    reads = []
    gw = make_gateway(
        harness=lambda uid: reads.append(uid) or HarnessPack(2, {"H1.decompose": "新的。"}))
    req = ChatRequest(purpose="t", prompt_version="t@v1", subject="s",
                      system="规则。⟦g:decompose⟧默认的话。⟦/g⟧",
                      messages=(ChatMessage(role="user", text="问"),))
    sent = gw.as_sent(req, ACC, {"decompose": "旧的。"})
    assert sent.system == "规则。旧的。" and sent.prompt_version == "t@v1+user" and reads == []
    assert gw.as_sent(req, None, {"decompose": "旧的。"}).system == "规则。默认的话。"
    assert gw.as_sent(req, ACC).system == "规则。新的。" and reads == ["u1"]


def test_with_harness_is_a_copy_sharing_logs_and_usage(make_gateway):
    """A replay's gateway: packs from the candidate source, everything else shared; the original
    gateway keeps its own source."""
    log = Requests()
    gw = make_gateway(harness=lambda uid: HarnessPack(1, {"H1.decompose": "原来的。"}), requests=log)
    cand = gw.with_harness(lambda uid: HarnessPack(1, {"H1.decompose": "候选的。"}))
    assert cand is not gw
    assert cand.harness_for("u1").values == {"H1.decompose": "候选的。"}
    assert gw.harness_for("u1").values == {"H1.decompose": "原来的。"}
    _, call = cand.call("generator", request(), Out, account=ACC)
    assert "候选的。" in gw.provider.calls[0].system
    # the call log, the request log and the providers are the original's
    assert gw._log.get(call.input_hash) is not None
    assert [r[0] for r in log.rows] == [call.id]
    gw.call("generator", request(), Out, account=ACC)
    assert "原来的。" in gw.provider.calls[1].system


def test_a_salted_copy_never_reuses_recorded_calls(make_gateway):
    gw = make_gateway()
    _, recorded = gw.call("generator", request(), Out)
    a = gw.salted("hr1")
    _, c1 = a.call("generator", request(), Out)
    assert len(gw.provider.calls) == 2 and c1.id != recorded.id
    assert gw.provider.calls[0] == gw.provider.calls[1]      # the request sent is unchanged
    _, again = a.call("generator", request(), Out)            # same salt: served from the log
    assert again.id == c1.id and len(gw.provider.calls) == 2
    gw.salted("hr2").call("generator", request(), Out)
    assert len(gw.provider.calls) == 3
    _, plain = gw.call("generator", request(), Out)           # the original: unchanged keys
    assert plain.id == recorded.id and len(gw.provider.calls) == 3


def test_with_harness_replaces_overrides(make_gateway):
    gw = make_gateway(overrides=lambda uid: {"decompose": "用户的。"})
    cand = gw.with_harness(lambda uid: HarnessPack(1, {"H1.decompose": "候选的。"}))
    cand.call("generator", request(), Out, account=ACC)
    assert "候选的。" in gw.provider.calls[0].system and gw.overrides is not None
