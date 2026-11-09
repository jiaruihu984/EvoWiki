"""Selections reach SDK JSON, request hashes and audit data, without a paid endpoint."""
import json

import httpx
import pytest
from openai import OpenAI
from pydantic import BaseModel

from evowiki.llm.chat import ChatMessage, ChatRequest
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.openai_compatible import OpenAICompatibleProvider
from evowiki.llm.providers import AnthropicProvider, DeepSeekProvider


class Answer(BaseModel):
    value: int


@pytest.mark.parametrize(("provider", "model", "effort"), [
    ("openai", "gpt-5.2", "xhigh"), ("gemini", "gemini-2.5-flash", "minimal"),
    ("kimi", "kimi-k3.1-pro", "max"), ("glm", "glm-5.3", "low"),
])
@pytest.mark.parametrize("policy_off", [False, True])
def test_real_sdk_receives_concrete_model_and_reasoning(provider, model, effort, policy_off, tmp_path):
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"id": "fake", "object": "chat.completion", "created": 1,
            "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"value":3}'}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}})

    client = OpenAI(api_key="test-only", base_url="http://model.invalid/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    ref = ModelRef(provider=provider, model=model, reasoning_effort=effort)
    cfg = GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True)
    adapter = OpenAICompatibleProvider(provider, client=client)
    gw = Gateway(cfg, CallLog(tmp_path / "calls.jsonl"), mode="record", providers={provider: adapter})
    params = {"thinking": False, "temperature": 0} if policy_off else {}
    request = LLMRequest(purpose="memory.consolidate", prompt_version="test", subject="s",
                         system="Return JSON", prompt="Return value 3", params=params)
    try:
        result, call = gw.call("generator", request, Answer)
        assert result.value == 3
        expected = ("none" if provider in {"openai", "gemini"} else "low") if policy_off else effort
        assert seen[0]["model"] == model and seen[0]["reasoning_effort"] == expected
        assert call.provider == provider and call.model == model
        assert call.params["reasoning_effort"] == expected
        if provider == "kimi":
            assert "temperature" not in seen[0] and "temperature" not in call.params
    finally:
        client.close()


def test_effort_changes_do_not_share_recorded_answers(tmp_path):
    seen = []

    class Provider:
        name = "openai"

        def complete(self, model, request, schema):
            from evowiki.llm.models import ProviderResponse
            seen.append(dict(request.params))
            return ProviderResponse(raw_output=json.dumps({"value": len(seen)}))

    log = CallLog(tmp_path / "calls.jsonl")
    request = LLMRequest(purpose="assistant.turn", prompt_version="t", subject="s", system="", prompt="q")
    calls = []
    for effort in ("low", "high", "low"):
        ref = ModelRef(provider="openai", model="gpt-5.2", reasoning_effort=effort)
        cfg = GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True)
        gw = Gateway(cfg, log, mode="record", providers={"openai": Provider()})
        calls.append(gw.call("generator", request, Answer)[1])
    assert [x["reasoning_effort"] for x in seen] == ["low", "high"]
    assert calls[0].input_hash != calls[1].input_hash
    assert calls[0].id == calls[2].id


def test_anthropic_concrete_effort_is_read_from_request():
    request = LLMRequest(purpose="p", prompt_version="t", subject="s", system="", prompt="q",
                         params={"reasoning_effort": "medium"})
    provider = AnthropicProvider(client=object(), effort="low")
    assert provider._effort_for_request("claude-sonnet-5-5", request) == "medium"


@pytest.mark.parametrize("effort,requests", [("max", 2), ("low", 1)])
def test_starved_always_thinking_model_uses_valid_minimum_once(tmp_path, effort, requests):
    from evowiki.llm.providers import ProviderEmptyOutputError

    class Provider:
        def __init__(self):
            self.seen = []

        def complete(self, model, request, schema):
            self.seen.append(dict(request.params))
            raise ProviderEmptyOutputError("cut", raw_output="", status="truncated")

    provider = Provider()
    ref = ModelRef(provider="kimi", model="kimi-k3.1-pro", reasoning_effort=effort)
    gw = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
        CallLog(tmp_path / "calls.jsonl"), mode="record", providers={"kimi": provider})
    with pytest.raises(ProviderEmptyOutputError):
        gw.call("generator", LLMRequest(purpose="p", prompt_version="t", subject="s", system="", prompt="q"), Answer)
    assert len(provider.seen) == requests
    assert provider.seen[-1]["reasoning_effort"] == "low"
    assert all(p["thinking"] for p in provider.seen)


def test_deepseek_selected_reasoning_is_sent_to_chat():
    from tests.llm.test_providers_chat import _DS, _ds_msg
    client = _DS(_ds_msg("ok"))
    provider = DeepSeekProvider(client=client, chat_thinking=False)
    request = ChatRequest(purpose="p", prompt_version="t", subject="s", system="",
        messages=(ChatMessage(role="user", text="q"),),
        params={"reasoning_effort": "max", "thinking": True})
    provider.chat("deepseek-v4-pro", request)
    assert client.kwargs["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
