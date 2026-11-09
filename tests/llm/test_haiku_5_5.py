"""Claude Haiku 5.5 (claude-api skill, Migrating to Claude Haiku 5.5): its rate cards, no server-side
fallback, thinking really off at the configured effort, refusals fail closed with their category."""

from types import SimpleNamespace

import pytest

from evowiki.llm.chat import ChatMessage, ChatRequest
from evowiki.llm.exposure import ToolExposure, exposure_for
from evowiki.llm.models import TEMPERATURE_PARAM, THINKING_PARAM, LLMRequest
from evowiki.llm.prices import load_prices
from evowiki.llm.providers import AnthropicProvider, ProviderRefusalError, complete_system

MODEL = "claude-haiku-5-5"
SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}}}


class _Stub:
    def __init__(self, stop_reason="end_turn", stop_details=None):
        self.kwargs = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._stop, self._details = stop_reason, stop_details

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason=self._stop, stop_details=self._details, model=MODEL,
            content=[SimpleNamespace(type="text", text='{"value": 1}')],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def _req(**params):
    return LLMRequest(purpose="p", prompt_version="v1", subject="s", system="SYS", prompt="P",
                      params=params)


def _chat(**params):
    return ChatRequest(purpose="p", prompt_version="v1", subject="s", system="SYS",
                       messages=(ChatMessage(role="user", text="hi"),), params=params)


@pytest.mark.parametrize("provider", ["anthropic", "anthropic-think"])
def test_both_rate_cards_split_by_the_whole_prompt(provider):
    t = load_prices()
    # exactly 100,000 is still the low card
    assert t.cost(provider, MODEL, 100_000, 1_000_000) == pytest.approx(0.01 + 0.50)
    # cached tokens count toward the length; every rate moves: 0.50, 2.50, 0.05, 0.625, 1.00
    assert t.cost(provider, MODEL, 1_000, 1_000_000, 60_000, 40_000, 10_000) == pytest.approx(
        (1_000 * 0.50 + 1_000_000 * 2.50 + 60_000 * 0.05 + 30_000 * 0.625 + 10_000 * 1.00) / 1e6)
    assert t.cost(provider, MODEL, 1_000, 1_000, 500, 200, 100) == pytest.approx(
        (1_000 * 0.10 + 1_000 * 0.50 + 500 * 0.01 + 100 * 0.125 + 100 * 0.20) / 1e6)


def test_no_fallback_parameter_and_no_sampling():
    stub = _Stub()
    AnthropicProvider(client=stub).complete(MODEL, _req(), SCHEMA)
    assert not {"fallbacks", "betas", "thinking", "temperature", "top_p", "top_k"} & set(stub.kwargs)
    assert stub.kwargs["output_config"]["effort"] == "medium"
    stub = _Stub()
    AnthropicProvider(client=stub).chat(MODEL, _chat())
    assert not {"fallbacks", "temperature"} & set(stub.kwargs)
    # a replayed thinking block whose prefix changed is dropped by the API, not a 400
    assert stub.kwargs["thinking"] == {"type": "adaptive", "block_binding": {
        "prefix_mismatch_behavior": "drop_block"}}
    assert stub.kwargs["betas"] == ["thinking-binding-controls-2026-08-01"]


def test_a_load_in_a_batch_gives_its_tools_in_the_same_turn():
    assert exposure_for("anthropic", MODEL, thinking=True, env={}) == ToolExposure.mid_turn_swap
    assert exposure_for("anthropic", "claude-sonnet-5-5", thinking=True, env={}) == ToolExposure.two_phase


def test_thinking_off_strips_replayed_thinking_blocks():
    history = [{"type": "thinking", "thinking": "", "signature": "sig"},
               {"type": "tool_use", "id": "t1", "name": "n", "input": {}}]
    request = _chat(**{THINKING_PARAM: False}).model_copy(update={"messages": (
        ChatMessage(role="user", text="hi"),
        ChatMessage(role="assistant", text="", echo=history),
        ChatMessage(role="tool", text="r", tool_call_id="t1"))})
    stub = _Stub()
    AnthropicProvider(client=stub).chat(MODEL, request)
    assert [b["type"] for b in stub.kwargs["messages"][1]["content"]] == ["tool_use"]
    assert stub.kwargs["thinking"] == {"type": "disabled"} and "betas" not in stub.kwargs


@pytest.mark.parametrize("effort,sent", [("medium", "medium"), ("low", "low"), ("high", "high"),
                                         ("xhigh", "high"), ("max", "high")])
def test_thinking_off_is_disabled_at_the_configured_effort_up_to_high(effort, sent):
    params = {THINKING_PARAM: False, TEMPERATURE_PARAM: 0}
    stub = _Stub()
    AnthropicProvider(client=stub, effort=effort).complete(MODEL, _req(**params), SCHEMA)
    assert stub.kwargs["thinking"] == {"type": "disabled"}
    assert stub.kwargs["output_config"]["effort"] == sent
    assert "temperature" not in stub.kwargs
    stub = _Stub()
    AnthropicProvider(client=stub, effort=effort).chat(MODEL, _chat(**params))
    assert stub.kwargs["thinking"] == {"type": "disabled"}
    assert stub.kwargs["output_config"]["effort"] == sent


def test_sonnet_keeps_low_effort_as_its_thinking_off_and_its_fallback():
    stub = _Stub()
    AnthropicProvider(client=stub).complete("claude-sonnet-5-5", _req(**{THINKING_PARAM: False}),
                                            SCHEMA)
    assert stub.kwargs["output_config"]["effort"] == "low" and "thinking" not in stub.kwargs
    assert stub.kwargs["fallbacks"] == "default"


@pytest.mark.parametrize("call", ["complete", "chat"])
def test_refusal_names_its_category(call):
    p = AnthropicProvider(client=_Stub("refusal", SimpleNamespace(category="frontier_llm")))
    with pytest.raises(ProviderRefusalError, match=r"p/s \(frontier_llm\)") as got:
        p.complete(MODEL, _req(), SCHEMA) if call == "complete" else p.chat(MODEL, _chat())
    assert got.value.status == "refusal" and got.value.input_tokens == 10


def test_a_512_token_stable_system_prompt_is_marked():
    system = complete_system(MODEL, "goals.intake", "词 " * 2000)
    assert system[0]["cache_control"]["ttl"] == "1h"
