"""Gateway model calls inside a durable workflow: one JSON step per call or turn, gateway errors
cross the step as their own class, and a replayed step restores the page's spend."""
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from evowiki.agent import durable
from evowiki.deep.llm import JobMeter, PageBudgetExceeded
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn
from evowiki.llm.gateway import (
    Gateway,
    LLMParseError,
    MissingRecordingError,
    ProviderOutputError,
    ProviderUnavailableError,
)
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMCall, LLMRequest, ModelRef, ProviderResponse
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.providers import ProviderRejectedError, ProviderTransientError
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import Account, BudgetExceededError
from evowiki.llm.usage_control import UsageControl

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
PRICES = PriceTable({"scripted:m": Price(1.0, 2.0)})
ACC = Account(user_id="u1", group="page")
T0 = datetime(2026, 10, 14, tzinfo=UTC)


class Answer(BaseModel):
    value: int


def _req(**over) -> LLMRequest:
    base = {"purpose": "unit", "prompt_version": "unit.v1", "subject": "x", "system": "sys",
            "prompt": "p"}
    return LLMRequest(**(base | over))


def _chat_req() -> ChatRequest:
    return ChatRequest(purpose="unit.chat", prompt_version="unit@v1", subject="s", system="SYS",
                       messages=(ChatMessage(role="user", text="hi"),), tools=())


class PaidProvider:
    """Plays a script of raw outputs / exceptions; every response reports 1000 / 200 tokens."""

    def __init__(self, script):
        self.script = list(script)
        self.complete_calls = 0

    def complete(self, model, request, json_schema):
        self.complete_calls += 1
        out = self.script.pop(0)
        if isinstance(out, Exception):
            raise out
        return ProviderResponse(raw_output=out, model_version=model, cost=None,
                                input_tokens=1000, output_tokens=200)


class NullMeter:
    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        pass


def _gateway(tmp_path, provider, meter, *, log="calls.jsonl"):
    return Gateway(CFG, CallLog(tmp_path / log), mode="record",
                   providers={"scripted": provider}, record_max_attempts=1,
                   clock=lambda: T0, usage=UsageControl(meter, PRICES))


class StepSpy:
    """Wraps `durable.step_ran`: records step names and outputs; `replay_with` makes the next
    steps report a recorded output without running (what a DBOS replay does)."""

    def __init__(self, monkeypatch):
        self.names: list[str] = []
        self.outputs: list = []
        self.replay_with = None
        real = durable.step_ran

        def spy(name, fn):
            self.names.append(name)
            if self.replay_with is not None:
                return self.replay_with, False
            out, ran = real(name, fn)
            self.outputs.append(out)
            return out, ran

        monkeypatch.setattr(durable, "step_ran", spy)


def test_outside_a_workflow_call_is_unchanged(tmp_path, monkeypatch):
    spy = StepSpy(monkeypatch)
    meter = JobMeter(NullMeter(), None)
    gw = _gateway(tmp_path, PaidProvider(['{"value": 1}']), meter)
    parsed, call = gw.call("generator", _req(), Answer, account=ACC)
    assert isinstance(parsed, Answer) and parsed.value == 1 and isinstance(call, LLMCall)
    assert spy.names == [] and meter.spent > 0


def test_call_in_a_virtual_workflow_is_one_step_with_the_same_result(tmp_path, monkeypatch):
    plain = _gateway(tmp_path, PaidProvider(['{"value": 7}']), JobMeter(NullMeter(), None),
                     log="plain.jsonl")
    want_parsed, want_call = plain.call("generator", _req(), Answer, account=ACC)
    spy = StepSpy(monkeypatch)
    meter = JobMeter(NullMeter(), None)
    gw = _gateway(tmp_path, PaidProvider(['{"value": 7}']), meter)
    with durable.virtual_workflow("wf"):
        parsed, call = gw.call("generator", _req(), Answer, account=ACC)
    assert spy.names == ["llm.call:unit"]
    assert isinstance(parsed, Answer) and parsed == want_parsed
    assert isinstance(call, LLMCall)
    assert call.model_copy(update={"latency": 0}) == want_call.model_copy(update={"latency": 0})
    assert meter.spent > 0


def test_converse_in_a_workflow_is_one_step(tmp_path, monkeypatch):
    spy = StepSpy(monkeypatch)
    turn = ChatTurn(text="hello", input_tokens=10, output_tokens=3)
    gw = _gateway(tmp_path, ScriptedChatProvider("scripted", lambda m, r: turn),
                  JobMeter(NullMeter(), None))
    with durable.virtual_workflow("wf"):
        got, call = gw.converse("generator", _chat_req(), account=ACC)
    assert spy.names == ["llm.converse:unit.chat"]
    assert isinstance(got, ChatTurn) and got.text == "hello" and isinstance(call, LLMCall)


def test_provider_error_crosses_as_its_type_with_status(tmp_path):
    truncated = ProviderOutputError("cut", raw_output="{", cost=0.5, status="truncated",
                                    model_version="mv", input_tokens=5, output_tokens=6)
    # truncated, then truncated again with thinking off: the second one is raised
    gw = _gateway(tmp_path, PaidProvider([truncated, truncated]), JobMeter(NullMeter(), None))
    with durable.virtual_workflow("wf"), pytest.raises(ProviderOutputError) as info:
        gw.call("generator", _req(), Answer, account=ACC)
    exc = info.value
    # the cost is the one the usage control priced from the tokens, not the provider's own 0.5
    assert (exc.status, exc.raw_output, exc.model_version) == ("truncated", "{", "mv")
    assert exc.cost == pytest.approx(1.7e-05)
    assert (exc.input_tokens, exc.output_tokens) == (5, 6) and str(exc) == "cut"


def test_replayed_step_restores_the_page_spend(tmp_path, monkeypatch):
    spy = StepSpy(monkeypatch)
    meter = JobMeter(NullMeter(), None)
    provider = PaidProvider(['{"value": 1}'])
    gw = _gateway(tmp_path, provider, meter)
    with durable.virtual_workflow("wf"):
        gw.call("generator", _req(), Answer, account=ACC)
    once = meter.spent
    assert once > 0 and spy.outputs[0]["charged"] == pytest.approx(once)

    spy.replay_with = spy.outputs[0]
    with durable.virtual_workflow("wf"):
        parsed, _ = gw.call("generator", _req(), Answer, account=ACC)
    assert parsed.value == 1 and provider.complete_calls == 1
    assert meter.spent == pytest.approx(2 * once)


def test_a_replay_on_a_meter_without_restore_is_skipped(tmp_path, monkeypatch):
    spy = StepSpy(monkeypatch)
    provider = PaidProvider(['{"value": 1}'])
    gw = _gateway(tmp_path, provider, NullMeter())
    with durable.virtual_workflow("wf"):
        gw.call("generator", _req(), Answer, account=ACC)
    spy.replay_with = spy.outputs[0]
    with durable.virtual_workflow("wf"):
        parsed, _ = gw.call("generator", _req(), Answer, account=ACC)
    assert parsed.value == 1 and provider.complete_calls == 1


def test_budget_exceeded_inside_the_step_is_reraised(tmp_path):
    gw = _gateway(tmp_path, PaidProvider(['{"value": 1}']), JobMeter(NullMeter(), 0.0))
    with durable.virtual_workflow("wf"), pytest.raises(BudgetExceededError) as info:
        gw.call("generator", _req(), Answer, account=ACC)
    assert isinstance(info.value, PageBudgetExceeded) and info.value.limit_usd == 0.0


@pytest.mark.parametrize("exc", [
    MissingRecordingError("m"), ProviderUnavailableError("u"), LLMParseError("p"),
    ProviderOutputError("o", status="refusal", cost=0.1, raw_output="r", model_version="v",
                        input_tokens=1, output_tokens=2, cache_read_tokens=3,
                        cache_write_tokens=4),
    BudgetExceededError(ACC, "day", 1.0), PageBudgetExceeded(ACC, 2.0),
    ProviderRejectedError("r"), ProviderTransientError("t"),
], ids=lambda e: type(e).__name__)
def test_every_gateway_error_round_trips_by_class(exc):
    def boom():
        raise exc

    with durable.virtual_workflow("wf"), pytest.raises(type(exc)) as info:
        durable.step("s", boom)
    assert type(info.value) is type(exc) and str(info.value) == str(exc)
    for key in vars(exc):
        if key != "account":    # a pydantic Account is not JSON-able here, so it is not rebuilt
            assert getattr(info.value, key) == getattr(exc, key)
