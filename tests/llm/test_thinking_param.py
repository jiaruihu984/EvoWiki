from types import SimpleNamespace as NS

import pytest

from evowiki.llm.chat import ChatMessage, ChatRequest
from evowiki.llm.models import THINKING_PARAM, LLMRequest
from evowiki.llm.providers import AnthropicProvider, DeepSeekProvider

SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}}}


def _req(**params):
    return LLMRequest(purpose="p", prompt_version="v1", subject="s", system="SYS",
                      prompt="PROMPT", params=params)


def _chat_req(**params):
    return ChatRequest(purpose="assistant.turn", prompt_version="a@v1", subject="s",
                       system="SYS", messages=(ChatMessage(role="user", text="问"),),
                       params=params)


class _Anthropic:
    def __init__(self):
        self.kwargs = None
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.kwargs = kw
        return NS(stop_reason="end_turn", model="claude-sonnet-5-5",
                  content=[NS(type="text", text='{"value": 1}')],
                  usage=NS(input_tokens=1, output_tokens=1))


class _DeepSeek:
    def __init__(self):
        self.kwargs = None
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.kwargs = kw
        return NS(model="deepseek-v4-pro", usage=NS(prompt_tokens=1, completion_tokens=1),
                  choices=[NS(message=NS(content='{"value": 1}', tool_calls=None),
                              finish_reason="stop")])


def _effort(purpose):
    return "max"


def test_param_name():
    assert THINKING_PARAM == "thinking"


def test_anthropic_without_param_is_unchanged():
    for call in ("complete", "chat"):
        stubs = []
        for params in ({}, {"other": 1}):
            c = _Anthropic()
            p = AnthropicProvider(client=c)
            if call == "complete":
                p.complete("claude-sonnet-5-5", _req(**params), SCHEMA)
            else:
                p.chat("claude-sonnet-5-5", _chat_req(**params))
            stubs.append(c.kwargs)
        assert stubs[0] == stubs[1]
        assert stubs[0]["output_config"]["effort"] == "medium"


def test_anthropic_thinking_off_sends_low_effort():
    c = _Anthropic()
    AnthropicProvider(client=c).complete("claude-sonnet-5-5", _req(thinking=False), SCHEMA)
    assert c.kwargs["output_config"]["effort"] == "low"
    assert "thinking" not in c.kwargs
    c = _Anthropic()
    AnthropicProvider(client=c).chat("claude-sonnet-5-5", _chat_req(thinking=False))
    assert c.kwargs["output_config"]["effort"] == "low"
    c = _Anthropic()
    AnthropicProvider(client=c, effort="high").complete(
        "claude-sonnet-5-5", _req(thinking=True), SCHEMA)
    assert c.kwargs["output_config"]["effort"] == "high"


def test_deepseek_without_param_is_unchanged():
    for thinking in (None, True, False):
        a, b = _DeepSeek(), _DeepSeek()
        DeepSeekProvider(client=a, thinking=thinking, effort_for=_effort).complete(
            "m", _req(), SCHEMA)
        DeepSeekProvider(client=b, thinking=thinking, effort_for=_effort).complete(
            "m", _req(unrelated=1), SCHEMA)
        assert a.kwargs == b.kwargs
    c = _DeepSeek()
    DeepSeekProvider(client=c).chat("m", _chat_req())
    assert "extra_body" not in c.kwargs


def test_deepseek_param_false_overrides_thinking_instance():
    c = _DeepSeek()
    DeepSeekProvider(client=c, thinking=True, effort_for=_effort).complete(
        "m", _req(thinking=False), SCHEMA)
    assert c.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    c = _DeepSeek()
    DeepSeekProvider(client=c, thinking=True, effort_for=_effort).chat(
        "m", _chat_req(thinking=False))
    assert c.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_deepseek_chat_thinking_switch_is_sent_when_the_request_has_no_param():
    """The page writer's provider (chat_thinking=False): every chat turn says thinking disabled;
    without chat_thinking (the assistant's providers) chat sends no switch, as before."""
    c = _DeepSeek()
    DeepSeekProvider(client=c, thinking=False, chat_thinking=False,
                     effort_for=_effort).chat("m", _chat_req())
    assert c.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    c = _DeepSeek()
    DeepSeekProvider(client=c, thinking=False, chat_thinking=False,
                     effort_for=_effort).chat("m", _chat_req(thinking=True))
    assert c.kwargs["extra_body"]["thinking"] == {"type": "enabled"}     # an H10 pick still wins
    c = _DeepSeek()
    DeepSeekProvider(client=c, thinking=False).chat("m", _chat_req())
    assert "extra_body" not in c.kwargs
    with pytest.raises(ValueError, match="chat_thinking"):
        DeepSeekProvider(client=_DeepSeek(), chat_thinking="no")


def test_deepseek_param_true_overrides_thinking_off_instance():
    c = _DeepSeek()
    DeepSeekProvider(client=c, thinking=False, effort_for=_effort).complete(
        "m", _req(thinking=True), SCHEMA)
    assert c.kwargs["extra_body"] == {"thinking": {"type": "enabled"},
                                      "reasoning_effort": "max"}
    c = _DeepSeek()
    DeepSeekProvider(client=c, thinking=False, effort_for=_effort).chat(
        "m", _chat_req(thinking=True))
    assert c.kwargs["extra_body"] == {"thinking": {"type": "enabled"},
                                      "reasoning_effort": "max"}


def test_default_built_pipeline_clients_have_no_hidden_sdk_retries(monkeypatch):
    # providers_from_env builds AnthropicProvider() / DeepSeekProvider(): the SDK default of 2
    # retries would hide attempts from the gateway's audit
    anthropic = pytest.importorskip("anthropic")   # optional extra `llm`
    pytest.importorskip("openai")

    from evowiki.llm.providers import CLIENT_TIMEOUT_S

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test")
    a = AnthropicProvider()._client
    assert isinstance(a, anthropic.Anthropic)
    assert a.max_retries == 0 and a.timeout == CLIENT_TIMEOUT_S
    d = DeepSeekProvider(env={"DEEPSEEK_API_KEY": "k-test"})._client
    assert d.max_retries == 0 and d.timeout == CLIENT_TIMEOUT_S
