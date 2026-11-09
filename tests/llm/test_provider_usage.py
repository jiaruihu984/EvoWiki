"""Providers report token counts (plan rev 3.2 §9.4). Never touches the network."""
from types import SimpleNamespace

import pytest

from evowiki.llm.gateway import ProviderOutputError
from evowiki.llm.models import LLMCall, LLMRequest, ProviderResponse
from evowiki.llm.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    ProviderEmptyOutputError,
    ProviderRefusalError,
)

REQ = LLMRequest(purpose="p", prompt_version="v1", subject="s", system="SYS", prompt="PROMPT")
SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}}}


def _anthropic(stop_reason="end_turn", text='{"value": 1}'):
    def create(**kw):
        return SimpleNamespace(
            stop_reason=stop_reason, model="claude-sonnet-5-5",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=340))
    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))


def _deepseek(resp):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: resp)))


def _chat_completion(finish_reason="stop", content='{"value": 1}', usage=True):
    """A real openai SDK ChatCompletion, i.e. exactly the shape DeepSeek's API is parsed into."""
    openai_types = pytest.importorskip("openai.types.chat")
    body = {"id": "c1", "object": "chat.completion", "created": 0, "model": "deepseek-v4-pro",
            "choices": [{"index": 0, "finish_reason": finish_reason,
                         "message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    return openai_types.ChatCompletion.model_validate(body)


def test_provider_response_tokens_default_to_none_and_stay_out_of_llm_call():
    r = ProviderResponse(raw_output="x")
    assert r.input_tokens is None and r.output_tokens is None
    assert "input_tokens" not in LLMCall.model_fields


def test_anthropic_reports_tokens():
    r = AnthropicProvider(client=_anthropic()).complete("claude-sonnet-5-5", REQ, SCHEMA)
    assert (r.input_tokens, r.output_tokens) == (1200, 340)


@pytest.mark.parametrize("stop, text, exc", [("refusal", "no", ProviderRefusalError),
                                             ("max_tokens", '{"va', ProviderEmptyOutputError)])
def test_anthropic_paid_failures_carry_tokens(stop, text, exc):
    with pytest.raises(exc) as info:
        AnthropicProvider(client=_anthropic(stop, text)).complete("claude-sonnet-5-5", REQ,
                                                                   SCHEMA)
    assert (info.value.input_tokens, info.value.output_tokens) == (1200, 340)


def test_deepseek_sdk_response_reports_tokens():
    r = DeepSeekProvider(client=_deepseek(_chat_completion()), env={}).complete(
        "deepseek-v4-pro", REQ, SCHEMA)
    assert (r.input_tokens, r.output_tokens) == (120, 30)


def test_deepseek_truncation_carries_tokens():
    with pytest.raises(ProviderEmptyOutputError) as info:
        DeepSeekProvider(client=_deepseek(_chat_completion("length", '{"va')), env={}).complete(
            "deepseek-v4-pro", REQ, SCHEMA)
    assert (info.value.input_tokens, info.value.output_tokens) == (120, 30)


def test_deepseek_without_usage_reports_none():
    r = DeepSeekProvider(client=_deepseek(_chat_completion(usage=False)), env={}).complete(
        "deepseek-v4-pro", REQ, SCHEMA)
    assert r.input_tokens is None and r.output_tokens is None


def test_provider_output_error_tokens_default_none():
    e = ProviderOutputError("x")
    assert e.input_tokens is None and e.output_tokens is None
