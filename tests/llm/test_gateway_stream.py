"""Gateway.converse(stream=...) (spec 2026-10-15 §1): the provider streams only on a real provider
call; a turn served from the log, or a replayed durable step, streams nothing. Logging and
metering are those of the non-streamed call. An attempt that streamed text is never retried
(the learner saw it); one that streamed nothing is. A failure carries the text it streamed as
`partial_text`, also across a durable step boundary."""

import pytest

from evowiki.agent import durable
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn
from evowiki.llm.gateway import Gateway, ProviderOutputError
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.providers import ProviderEmptyOutputError, ProviderTransientError
from evowiki.llm.scripted import ScriptedChatProvider, ScriptedStreamFailure
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl
from tests.llm.test_gateway_usage import SpyMeter

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
REQ = ChatRequest(purpose="assistant.turn", prompt_version="a@v1", subject="s1", system="SYS",
                  messages=(ChatMessage(role="user", text="问"),))
ACC = Account(user_id="u1", group="assistant")


class Sink:
    def __init__(self):
        self.log: list[str] = []

    def text(self, chunk):
        self.log.append(chunk)


def gw(tmp_path, script, *, chunk=2, usage=None, attempts=1):
    outs = iter(script)

    def answer(m, r):
        out = next(outs)
        if isinstance(out, BaseException):
            raise out
        return out

    prov = ScriptedChatProvider("scripted", answer, stream_chunk=chunk)
    g = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                providers={"scripted": prov}, attempts=AttemptLog(tmp_path / "a.jsonl"),
                record_max_attempts=attempts, usage=usage)
    return g, prov


def test_a_provider_call_streams_and_logs_once(tmp_path):
    g, prov = gw(tmp_path, [ChatTurn(text="方差大的方向", input_tokens=5, output_tokens=3)])
    sink = Sink()
    turn, call = g.converse("generator", REQ, stream=sink)
    assert sink.log == ["方差", "大的", "方向"] and turn.text == "方差大的方向"
    assert prov.streamed == [True]
    # the log serves it again: nothing streams, no provider call
    sink2 = Sink()
    again, call2 = g.converse("generator", REQ, stream=sink2)
    assert sink2.log == [] and again == turn and call2.id == call.id and len(prov.chat_calls) == 1


def test_a_provider_that_does_not_stream_is_called_as_before(tmp_path):
    g, prov = gw(tmp_path, [ChatTurn(text="答")], chunk=None)
    sink = Sink()
    turn, _ = g.converse("generator", REQ, stream=sink)
    assert turn.text == "答" and sink.log == [] and prov.streamed == [False]


def test_metering_is_the_same_streamed_or_not(tmp_path):
    prices = PriceTable({"scripted:m": Price(1.0, 2.0, 0.1, 1.25)})
    out = ChatTurn(text="答案", input_tokens=100, output_tokens=20, cache_read_tokens=50,
                   cache_write_tokens=10)
    records = []
    for stream in (None, Sink()):
        meter = SpyMeter()
        d = tmp_path / ("s" if stream else "n")
        d.mkdir()
        g, _ = gw(d, [out], usage=UsageControl(meter=meter, prices=prices))
        g.converse("generator", REQ, account=ACC, stream=stream)
        records.append(meter.records())
    assert records[0] == records[1] and records[0][0][2].cost_usd is not None


def test_a_failure_after_visible_text_is_not_retried(tmp_path):
    """DeepTutor's output_emitted rule: retrying would show the text twice or mixed."""
    cut = ScriptedStreamFailure("先说一", ProviderEmptyOutputError("cut", status="truncated"))
    g, prov = gw(tmp_path, [cut, ChatTurn(text="重来")], attempts=3)
    sink = Sink()
    with pytest.raises(ProviderEmptyOutputError) as info:
        g.converse("generator", REQ, stream=sink)
    assert len(prov.chat_calls) == 1 and info.value.partial_text == "先说一"
    assert sink.log == ["先说", "一"]                  # no second answer
    assert [a.status for a in g._attempts.attempts()] == ["truncated"]


def test_an_empty_turn_after_visible_text_is_not_retried(tmp_path):
    g, prov = gw(tmp_path, [ChatTurn(text=" "), ChatTurn(text="重来")], chunk=1, attempts=3)
    sink = Sink()
    with pytest.raises(ProviderOutputError) as info:
        g.converse("generator", REQ, stream=sink)
    assert len(prov.chat_calls) == 1 and info.value.partial_text == " "


def test_a_failure_before_any_text_is_still_retried(tmp_path):
    g, prov = gw(tmp_path, [ProviderEmptyOutputError("cut", status="truncated"),
                            ChatTurn(text="重来")], attempts=2)
    sink = Sink()
    turn, _ = g.converse("generator", REQ, stream=sink)
    assert turn.text == "重来" and len(prov.chat_calls) == 2
    assert sink.log == ["重来"]


def test_a_failure_carries_what_it_streamed(tmp_path):
    g, _ = gw(tmp_path, [ScriptedStreamFailure("说到一半", ProviderTransientError("cut"))])
    with pytest.raises(ProviderTransientError) as info:
        g.converse("generator", REQ, stream=Sink())
    assert info.value.partial_text == "说到一半"


def test_a_failure_before_any_text_carries_none(tmp_path):
    g, _ = gw(tmp_path, [ProviderTransientError("down")])
    with pytest.raises(ProviderTransientError) as info:
        g.converse("generator", REQ, stream=Sink())
    assert getattr(info.value, "partial_text", None) is None


def test_partial_text_crosses_a_durable_step(tmp_path):
    g, _ = gw(tmp_path, [ScriptedStreamFailure("一半", ProviderTransientError("cut"))])
    sink = Sink()
    with durable.virtual_workflow("turn:s1:m1"), pytest.raises(ProviderTransientError) as info:
        g.converse("generator", REQ, stream=sink)
    assert sink.log == ["一半"] and info.value.partial_text == "一半"
