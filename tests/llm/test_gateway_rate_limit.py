"""HTTP 429 at the gateway (RATE_LIMIT_*): waited out, one llm_call however many tries, every try audited,
never after text was shown, never past the limit. Scripted provider, no paid call."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from evowiki.llm.chat import ChatMessage, ChatRequest, ChatResponse, ChatTurn
from evowiki.llm.deadline import RequestCancelled, cancel_scope, cancellable_sleep
from evowiki.llm.gateway import RATE_LIMIT_RETRIES, RATE_LIMIT_WAIT_MAX_S, Gateway
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef, ProviderResponse
from evowiki.llm.providers import ProviderBusyError, _sdk_call

REF = ModelRef(provider="p", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
CHAT = ChatRequest(purpose="assistant.turn", prompt_version="v", subject="s", system="SYS",
                   messages=(ChatMessage(role="user", text="问"),))
ASK = LLMRequest(purpose="goals.path", prompt_version="v", subject="s", system="SYS", prompt="问")


class Out(BaseModel):
    a: int


def limited(retry_after=None) -> ProviderBusyError:
    e = ProviderBusyError("请切换模型重试: HTTP 429", provider="p")
    e.rate_limited, e.retry_after = True, retry_after
    return e


class Fake:
    streams = True

    def __init__(self, *script):
        self.script, self.sends = list(script), 0

    def _next(self, stream=None):
        self.sends += 1
        item = self.script.pop(0)
        if isinstance(item, tuple):          # text shown, then this failure
            stream.text(item[0])
            item = item[1]
        if isinstance(item, Exception):
            raise item
        return item

    def chat(self, model, request, stream=None):
        return ChatResponse(turn=self._next(stream), model_version="v", cost=0.001)

    def complete(self, model, request, json_schema):
        return ProviderResponse(raw_output=self._next(), model_version="v", cost=0.001)


def gateway(tmp_path, fake):
    waits, calls, attempts = [], CallLog(tmp_path / "c.jsonl"), AttemptLog(tmp_path / "a.jsonl")
    g = Gateway(CFG, calls, mode="record", providers={"p": fake}, attempts=attempts,
                record_max_attempts=1, sleep=waits.append)
    return g, waits, calls, attempts


def test_a_429_then_success_is_one_turn_and_one_call(tmp_path):
    fake = Fake(limited(), limited(), ChatTurn(text="答"))
    g, waits, calls, attempts = gateway(tmp_path, fake)
    turn, call = g.converse("generator", CHAT)
    assert turn.text == "答" and fake.sends == 3
    assert [c.id for c in calls.calls()] == [call.id]
    assert len(waits) == 2 and 0.75 <= waits[0] <= 1 and 1.5 <= waits[1] <= 2
    assert [a.status for a in attempts.attempts()] == ["provider_error", "provider_error", "accepted"]


def test_the_structured_call_takes_the_same_path(tmp_path):
    fake = Fake(limited(), '{"a": 1}')
    g, waits, calls, _ = gateway(tmp_path, fake)
    assert g.call("generator", ASK, Out)[0].a == 1
    assert fake.sends == 2 and len(waits) == 1 and len(calls.calls()) == 1


def test_retry_after_is_honoured(tmp_path):
    fake = Fake(limited(7.0), ChatTurn(text="答"))
    g, waits, _, _ = gateway(tmp_path, fake)
    g.converse("generator", CHAT)
    assert waits == [7.0]


def test_a_retry_after_too_long_to_sit_through_fails_at_once(tmp_path):
    fake = Fake(limited(RATE_LIMIT_WAIT_MAX_S + 1), ChatTurn(text="答"))
    g, waits, calls, _ = gateway(tmp_path, fake)
    with pytest.raises(ProviderBusyError):
        g.converse("generator", CHAT)
    assert fake.sends == 1 and waits == [] and calls.calls() == []


def test_a_lasting_429_fails_after_the_limit(tmp_path):
    fake = Fake(*[limited() for _ in range(RATE_LIMIT_RETRIES + 5)])
    g, waits, calls, attempts = gateway(tmp_path, fake)
    with pytest.raises(ProviderBusyError):
        g.converse("generator", CHAT)
    assert fake.sends == RATE_LIMIT_RETRIES + 1 and len(waits) == RATE_LIMIT_RETRIES
    assert calls.calls() == [] and len(attempts.attempts()) == RATE_LIMIT_RETRIES + 1


def test_nothing_is_resent_after_text_was_shown(tmp_path):
    fake = Fake(("先说", limited()), ChatTurn(text="答"))
    g, waits, _, _ = gateway(tmp_path, fake)
    with pytest.raises(ProviderBusyError):
        g.converse("generator", CHAT, stream=SimpleNamespace(text=lambda _chunk: None))
    assert fake.sends == 1 and waits == []


def test_a_queued_request_is_busy_but_not_retried(tmp_path):
    """It has already waited out its whole timeout (providers: NoProgress)."""
    fake = Fake(ProviderBusyError("queued", provider="p"), ChatTurn(text="答"))
    g, waits, _, _ = gateway(tmp_path, fake)
    with pytest.raises(ProviderBusyError):
        g.converse("generator", CHAT)
    assert fake.sends == 1 and waits == []


def test_a_stopped_run_ends_the_wait():
    with cancel_scope(lambda: True), pytest.raises(RequestCancelled):
        cancellable_sleep(30)


def test_the_provider_reads_retry_after():
    class TooMany(Exception):
        status_code = 429
        response = SimpleNamespace(headers={"retry-after": "4"})

    def send(**_):
        raise TooMany("x")
    with pytest.raises(ProviderBusyError) as e:
        _sdk_call("p", SimpleNamespace(purpose="t", subject="s"), send)
    assert e.value.rate_limited and e.value.retry_after == 4.0
