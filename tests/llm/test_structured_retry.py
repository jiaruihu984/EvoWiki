"""A structured `call` whose reply is cut at max_tokens or empty (a thinking model spent the output
budget on reasoning) is asked once more with thinking off, then fails closed (DeepTutor's
structured_retry, gateway `_record_attempts`). Fakes only: no paid call."""

from types import SimpleNamespace as NS

import pytest
from pydantic import BaseModel

from evowiki.llm.gateway import Gateway, LLMParseError
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import THINKING_PARAM, GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.providers import DeepSeekProvider, ProviderEmptyOutputError

REF = ModelRef(provider="gen", model="g-1")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)


class Answer(BaseModel):
    value: int


def _req(**params) -> LLMRequest:
    return LLMRequest(purpose="unit", prompt_version="unit.v1", subject="x", system="sys",
                      prompt="p", params=params)


class Script:
    """`complete` plays `steps` (an exception is raised, a str returned) and keeps each request."""

    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    def complete(self, model, request, json_schema):
        from evowiki.llm.models import ProviderResponse
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return ProviderResponse(raw_output=step)


def _cut(status="truncated"):
    return ProviderEmptyOutputError("cut", raw_output='{"va', status=status)


def _gw(tmp_path, prov, attempts=1):
    return Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"gen": prov},
                   attempts=AttemptLog(tmp_path / "a.jsonl"), record_max_attempts=attempts)


@pytest.mark.parametrize("status", ["truncated", "empty_output"])
def test_a_starved_reply_is_retried_once_with_thinking_off(tmp_path, status):
    prov = Script(_cut(status), '{"value": 7}')
    g = _gw(tmp_path, prov)                       # runtime gateways: record_max_attempts=1
    out, call = g.call("generator", _req(), Answer)
    assert out == Answer(value=7)
    assert [r.params.get(THINKING_PARAM) for r in prov.requests] == [None, False]
    assert call.params == {THINKING_PARAM: False}          # the call logs what was sent
    assert [a.status for a in g._attempts.attempts()] == [status, "accepted"]
    # the answer is served for the original request from now on
    again, _ = g.call("generator", _req(), Answer)
    assert again == out and len(prov.requests) == 2


def test_a_second_starved_reply_fails_closed(tmp_path):
    prov = Script(_cut(), _cut(), '{"value": 7}')
    g = _gw(tmp_path, prov, attempts=3)
    with pytest.raises(ProviderEmptyOutputError):
        g.call("generator", _req(), Answer)
    assert len(prov.requests) == 2 and g._log.calls() == []
    assert [r.params.get(THINKING_PARAM) for r in prov.requests] == [None, False]


def test_a_request_already_without_thinking_is_not_asked_again(tmp_path):
    prov = Script(_cut(), '{"value": 7}')
    g = _gw(tmp_path, prov, attempts=3)
    with pytest.raises(ProviderEmptyOutputError):
        g.call("generator", _req(**{THINKING_PARAM: False}), Answer)
    assert len(prov.requests) == 1


def test_parse_errors_keep_their_own_retries_and_starved_still_lowers_once(tmp_path):
    prov = Script("not json", _cut(), "still not json")
    g = _gw(tmp_path, prov, attempts=2)
    with pytest.raises(LLMParseError):
        g.call("generator", _req(), Answer)
    assert [r.params.get(THINKING_PARAM) for r in prov.requests] == [None, None, False]


class _DeepSeekClient:
    """First reply cut at max_tokens, the second answers; keeps every request's kwargs."""

    def __init__(self):
        self.kwargs = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.kwargs.append(kw)
        cut = len(self.kwargs) == 1
        return NS(model="deepseek-v4-pro", usage=NS(prompt_tokens=1, completion_tokens=1),
                  choices=[NS(message=NS(content='{"va' if cut else '{"value": 1}',
                                         tool_calls=None),
                              finish_reason="length" if cut else "stop")])


def test_deepseek_gets_thinking_disabled_on_the_retry(tmp_path):
    client = _DeepSeekClient()
    prov = DeepSeekProvider(client=client, env={}, max_tokens=100)
    ref = ModelRef(provider="deepseek", model="deepseek-v4-pro")
    g = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                CallLog(tmp_path / "c.jsonl"), mode="record", providers={"deepseek": prov},
                record_max_attempts=1)
    out, _ = g.call("generator", _req(), Answer)
    assert out == Answer(value=1)
    first, second = client.kwargs
    assert "extra_body" not in first
    assert second["extra_body"] == {"thinking": {"type": "disabled"}}
