import copy
import json
import time
from types import SimpleNamespace as NS

import pytest
from pydantic import BaseModel

from evowiki.llm.chat import ChatMessage, ChatRequest, ImagePart, TextPart, ToolCall, ToolDef
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.openai_compatible import OpenAICompatibleProvider, compatible_messages
from evowiki.llm.providers import (
    ProviderEmptyOutputError,
    ProviderRefusalError,
    ProviderRejectedError,
    ProviderTimeoutError,
    ProviderTransientError,
)

REQ = LLMRequest(purpose="p", prompt_version="1", subject="s", system="SYS", prompt="中文")
CHAT = ChatRequest(purpose="p", prompt_version="1", subject="s", system="SYS",
                   messages=(ChatMessage(role="user", text="hello"),))
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


class Client:
    def __init__(self, response=None, error=None):
        self.kwargs = None
        self.response, self.error = response, error
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response or NS(model="actual", choices=[NS(finish_reason="stop", message=NS(
            content='{"answer":"中文"}', refusal=None, tool_calls=None))], usage=NS(
                prompt_tokens=100, completion_tokens=30, prompt_tokens_details=NS(cached_tokens=40)))


@pytest.mark.parametrize("name", ["openai", "gemini", "kimi", "openai_compatible"])
def test_profiles_json_reasoning_and_cached_tokens(name):
    client = Client()
    effort = "high"
    provider = OpenAICompatibleProvider(name=name, client=client)
    result = provider.complete("model", REQ.model_copy(update={"params": {
        "reasoning_effort": effort}}), SCHEMA)
    kw = client.kwargs
    assert result.input_tokens == 60 and result.cache_read_tokens == 40
    assert result.output_tokens == 30 and result.model_version == "actual"
    assert ("max_completion_tokens" in kw) == (name == "openai")
    if name == "kimi":
        assert kw["extra_body"] == {"thinking": {"type": "enabled"}}
        assert "reasoning_effort" not in kw
    else:
        assert kw["reasoning_effort"] == effort and "extra_body" not in kw
    if name in ("openai", "gemini"):
        assert kw["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
        assert kw["response_format"]["json_schema"]["strict"] is True
    else:
        assert kw["response_format"] == {"type": "json_object"}
        assert "JSON Schema" in kw["messages"][1]["content"]
    assert "additionalProperties" not in SCHEMA


def test_images_memory_and_tool_echo_round_trip():
    request = CHAT.model_copy(update={"messages": (
        ChatMessage(role="user", text="memory", origin="memory"),
        ChatMessage(role="assistant", text="earlier"),
        ChatMessage(role="user", text="look", parts=(ImagePart(
            media_type="image/png", sha256="a" * 64, data_b64="abc"),)),
        ChatMessage(role="assistant", tool_calls=(ToolCall(id="x", name="lookup", args={}),),
                    echo={"reasoning_content": "reason", "tool_extra_content": {
                        "x": {"google": {"thought_signature": "signed"}}}}),
    )})
    messages = compatible_messages(request, reasoning=True)
    assert "历史资料" in messages[1]["content"]
    assert messages[3]["content"][0]["image_url"]["url"] == "data:image/png;base64,abc"
    assert messages[4]["reasoning_content"] == "reason"
    assert messages[4]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "signed"
    assert "reasoning_content" not in compatible_messages(request, reasoning=False)[4]


class Chunks:
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


def chunk(delta, finish=None):
    return NS(model="actual", usage=None, choices=[NS(delta=NS(**delta), finish_reason=finish)])


def test_stream_folds_tools_signatures_reasoning_and_usage():
    chunks = Chunks([
        chunk({"reasoning_content": "secret"}),
        chunk({"content": "answer", "tool_calls": [NS(index=0, id="call", extra_content={
            "google": {"thought_signature": "sig"}}, function=NS(name="lookup", arguments='{"q":'))]}),
        chunk({"tool_calls": [NS(index=0, id=None, function=NS(name=None, arguments='"x"}'))]},
              "tool_calls"),
        NS(model="actual", choices=[], usage=NS(prompt_tokens=50, completion_tokens=9)),
    ])
    client = Client(response=chunks)
    text = []
    result = OpenAICompatibleProvider(name="kimi", client=client).chat(
        "kimi-k2.5", CHAT, NS(text=text.append))
    assert text == ["answer"] and chunks.closed
    assert result.turn.tool_calls[0].args == {"q": "x"}
    assert result.turn.echo["reasoning_content"] == "secret"
    assert result.turn.echo["tool_extra_content"]["call"]["google"]["thought_signature"] == "sig"
    assert result.input_tokens == 50 and result.output_tokens == 9


@pytest.mark.parametrize(("finish", "refusal", "error"), [
    ("length", None, ProviderEmptyOutputError),
    ("content_filter", None, ProviderRefusalError),
    ("stop", "refused", ProviderRefusalError),
])
def test_paid_output_failures_keep_usage(finish, refusal, error):
    client = Client(response=NS(model="actual", choices=[NS(finish_reason=finish,
        message=NS(content="partial", refusal=refusal))], usage=NS(prompt_tokens=10, completion_tokens=3)))
    with pytest.raises(error) as caught:
        OpenAICompatibleProvider(client=client).complete("model", REQ, SCHEMA)
    assert caught.value.input_tokens == 10


def test_broken_stream_is_visible_and_closed():
    chunks = Chunks([chunk({"content": "partial"})])
    with pytest.raises(ProviderTransientError):
        OpenAICompatibleProvider(client=Client(response=chunks)).chat("model", CHAT, NS(text=lambda x: None))
    assert chunks.closed


def test_total_deadline_and_rejected_credentials():
    class Slow(Client):
        def create(self, **kwargs):
            time.sleep(0.15)
            return super().create(**kwargs)

    with pytest.raises(ProviderTimeoutError):
        OpenAICompatibleProvider(client=Slow(), timeout_for=lambda p: 0.01).complete("m", REQ, SCHEMA)
    error = type("AuthenticationError", (Exception,), {"status_code": 401})("sensitive request")
    with pytest.raises(ProviderRejectedError) as caught:
        OpenAICompatibleProvider(client=Client(error=error)).complete("m", REQ, SCHEMA)
    assert "sensitive" not in str(caught.value)


def test_kimi_rejects_fake_effort_and_thinking_conflicts_before_network():
    client = Client()
    provider = OpenAICompatibleProvider(name="kimi", client=client)
    with pytest.raises(ValueError):
        provider.chat("kimi-k2.5", CHAT.model_copy(update={"params": {"reasoning_effort": "medium"}}))
    with pytest.raises(ValueError):
        provider.chat("kimi-k2.5", CHAT.model_copy(update={"params": {
            "reasoning_effort": "high", "thinking": False}}))
    assert client.kwargs is None


def test_current_kimi_k3_native_effort_strict_output_and_cache_write_accounting():
    client = Client(response=NS(model="kimi-k3.1-pro", choices=[NS(finish_reason="stop",
        message=NS(content='{"answer":"ok"}', refusal=None))], usage=NS(
            prompt_tokens=100, completion_tokens=8,
            prompt_tokens_details=NS(cached_tokens=40, cache_write_tokens=30))))
    provider = OpenAICompatibleProvider(name="kimi", client=client)
    result = provider.complete("kimi-k3.1-pro", REQ.model_copy(update={"params": {
        "reasoning_effort": "low"}}), SCHEMA)
    assert client.kwargs["reasoning_effort"] == "low"
    assert "extra_body" not in client.kwargs and "max_completion_tokens" in client.kwargs
    assert client.kwargs["response_format"]["json_schema"]["strict"] is True
    assert result.input_tokens == 30 and result.cache_read_tokens == 40
    assert result.cache_write_tokens == 30
    with pytest.raises(ValueError, match="always thinks"):
        provider.chat("kimi-k3.1-pro", CHAT.model_copy(update={"params": {"thinking": False}}))


def test_sdk_constructor_uses_configured_endpoint_key_and_disables_retries(monkeypatch):
    import openai

    kwargs = []
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: kwargs.append(kw) or Client())
    OpenAICompatibleProvider(name="kimi", env={"MOONSHOT_API_KEY": "test-only",
                                             "MOONSHOT_BASE_URL": "https://example.invalid/v1"})
    assert kwargs[0]["base_url"] == "https://example.invalid/v1"
    assert kwargs[0]["max_retries"] == 0 and kwargs[0]["api_key"] == "test-only"
    with pytest.raises(ValueError, match="base URL"):
        OpenAICompatibleProvider(name="openai_compatible", env={})


@pytest.mark.parametrize(("model", "level", "thinking", "native"), [
    ("glm-5.3", "low", "enabled", True),
    ("glm-5.3", "max", "enabled", True),
    ("glm-5.2", "none", "disabled", False),
    ("glm-5.2", "high", "enabled", True),
    ("glm-4.7", "none", "disabled", False),
    ("glm-4.7", "high", "enabled", False),
])
def test_glm_native_effort_is_distinct_from_older_thinking_switch(model, level, thinking, native):
    client = Client()
    provider = OpenAICompatibleProvider(name="glm", client=client)
    provider.complete(model, REQ.model_copy(update={"params": {"reasoning_effort": level}}), SCHEMA)
    assert client.kwargs["extra_body"] == {"thinking": {"type": thinking}}
    assert ("reasoning_effort" in client.kwargs) is native
    assert client.kwargs["response_format"] == {"type": "json_object"}
    assert "max_tokens" in client.kwargs


def test_glm_no_fake_disable_or_graded_effort_on_old_model():
    client = Client()
    provider = OpenAICompatibleProvider(name="glm", client=client)
    for model, params in (("glm-5.3", {"thinking": False}),
                          ("glm-5.3", {"reasoning_effort": "none"}),
                          ("glm-4.7", {"reasoning_effort": "medium"})):
        with pytest.raises(ValueError):
            provider.chat(model, CHAT.model_copy(update={"params": params}))
    assert client.kwargs is None


def test_glm_tool_stream_and_reasoning_echo():
    chunks = Chunks([
        chunk({"reasoning_content": "original reasoning"}),
        chunk({"tool_calls": [NS(index=0, id="call", function=NS(name="lookup", arguments="{}"))]},
              "tool_calls"),
    ])
    client = Client(response=chunks)
    provider = OpenAICompatibleProvider(name="glm", client=client)
    request = CHAT.model_copy(update={"tools": (ToolDef(name="lookup", description="find", parameters={}),),
                                      "params": {"reasoning_effort": "low"}})
    turn = provider.chat("glm-5.3", request, NS(text=lambda text: None)).turn
    assert client.kwargs["extra_body"]["tool_stream"] is True
    assert "stream_options" not in client.kwargs
    assert turn.echo == {"reasoning_content": "original reasoning"}
    again = CHAT.model_copy(update={"messages": (*CHAT.messages, ChatMessage(
        role="assistant", tool_calls=turn.tool_calls, echo=turn.echo)), "tool_choice": "none",
        "tools": request.tools})
    client.response = None
    provider.chat("glm-5.3", again)
    assert client.kwargs["messages"][-1]["reasoning_content"] == "original reasoning"
    assert "tools" not in client.kwargs and "tool_choice" not in client.kwargs


@pytest.mark.parametrize(("finish", "error"), [
    ("sensitive", ProviderRefusalError),
    ("model_context_window_exceeded", ProviderEmptyOutputError),
    ("network_error", ProviderTransientError),
])
def test_glm_special_finish_reasons_fail_visibly(finish, error):
    client = Client(response=NS(model="glm-5.3", choices=[NS(finish_reason=finish,
        message=NS(content="partial", refusal=None))], usage=NS(prompt_tokens=10, completion_tokens=3)))
    with pytest.raises(error) as caught:
        OpenAICompatibleProvider(name="glm", client=client).complete("glm-5.3", REQ, SCHEMA)
    assert caught.value.input_tokens == 10


@pytest.mark.parametrize("name", ["openai", "gemini", "kimi"])
def test_nested_dynamic_maps_preserve_facts_through_gateway_validation(tmp_path, name):
    class Answer(BaseModel):
        facts: dict[str, dict[str, str]]

    answer = {"facts": {"来源甲": {"fact": "事实", "extra": "保留"}}}
    client = Client(response=NS(model="actual", choices=[NS(finish_reason="stop", message=NS(
        content=json.dumps(answer, ensure_ascii=False), refusal=None))], usage=None))
    calls = []
    create = client.chat.completions.create
    client.chat.completions.create = lambda **kw: calls.append(kw) or create(**kw)
    ref = ModelRef(provider=name, model="kimi-k3.1-pro" if name == "kimi" else "model")
    gateway = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                      CallLog(tmp_path / "calls.jsonl"), mode="record",
                      providers={name: OpenAICompatibleProvider(name=name, client=client)})
    result, _ = gateway.call("generator", REQ, Answer)
    assert result.model_dump() == answer
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}
    supplied = json.loads(calls[0]["messages"][1]["content"].split("JSON Schema:\n")[1])
    assert supplied == Answer.model_json_schema()


@pytest.mark.parametrize("schema", [
    {"type": "object", "additionalProperties": True},
    {"type": "object", "properties": {"facts": {"type": "object", "additionalProperties": {
        "type": "string"}}}, "required": ["facts"]},
    {"type": "array", "items": {"type": "string"}},
    {"anyOf": [SCHEMA, {"type": "null"}]},
    {"type": "object", "properties": {"optional": {"type": "string", "default": "kept"}}},
])
def test_nonrepresentable_schema_uses_json_object_without_mutation(schema):
    original = copy.deepcopy(schema)
    client = Client()
    OpenAICompatibleProvider(client=client).complete("model", REQ, schema)
    assert schema == original
    assert client.kwargs["response_format"] == {"type": "json_object"}
    assert json.dumps(schema, ensure_ascii=False) in client.kwargs["messages"][1]["content"]


def test_memory_then_mixed_image_and_text_attachments_keep_order():
    request = CHAT.model_copy(update={"messages": (
        ChatMessage(role="user", origin="memory", text="历史事实"),
        ChatMessage(role="user", text="当前问题", parts=(TextPart(text="图前说明"), ImagePart(
            media_type="image/png", sha256="a" * 64, data_b64="abc"), TextPart(text="图后说明"))),
    )})
    messages = compatible_messages(request, reasoning=False)
    assert len(messages) == 2
    content = messages[1]["content"]
    assert "<历史资料>\n历史事实\n</历史资料>" in content[0]["text"]
    assert content[1] == {"type": "text", "text": "图前说明"}
    assert content[2]["type"] == "image_url"
    assert content[3:] == [{"type": "text", "text": "图后说明"}, {"type": "text", "text": "当前问题"}]
