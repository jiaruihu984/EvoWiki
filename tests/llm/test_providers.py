import os
from types import SimpleNamespace

import pytest

from evowiki.llm.models import (
    TEMPERATURE_PARAM,
    THINKING_PARAM,
    GatewayConfig,
    LLMRequest,
    ModelRef,
)
from evowiki.llm.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    ProviderEmptyOutputError,
    ProviderRefusalError,
    deepseek_max_tokens,
    missing_env_vars,
    providers_from_env,
    strict_schema,
)

REQ = LLMRequest(purpose="p", prompt_version="v1", subject="s", system="SYS", prompt="PROMPT")
SCHEMA = {"type": "object", "properties": {"value": {"type": "integer", "default": 3},
                                           "inner": {"$ref": "#/$defs/In"}},
          "$defs": {"In": {"type": "object", "properties": {"x": {"type": "string"}}}}}


def test_strict_schema_closes_objects_and_drops_defaults():
    s = strict_schema(SCHEMA)
    assert s["additionalProperties"] is False and s["required"] == ["value", "inner"]
    assert "default" not in s["properties"]["value"]
    assert s["$defs"]["In"]["additionalProperties"] is False
    assert "default" in SCHEMA["properties"]["value"]  # input untouched


def test_strict_schema_drops_constraints_anthropic_rejects():
    s = strict_schema({"type": "object", "properties": {
        "steps": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 9},
                  "minItems": 2, "maxItems": 12},
        "h": {"type": "integer", "minimum": 400, "maximum": 1600},
        "id": {"type": "string", "pattern": "^[a-z]+$"}}})
    steps = s["properties"]["steps"]
    assert "maxItems" not in steps and steps["minItems"] == 1
    assert steps["items"] == {"type": "string"}
    assert s["properties"]["h"] == {"type": "integer"} and s["properties"]["id"] == {"type": "string"}


class _AnthropicStub:
    def __init__(self, stop_reason="end_turn", text='{"value": 1}'):
        self.kwargs = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._stop = stop_reason
        self._text = text

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason=self._stop, model="claude-opus-5-5",
            content=[SimpleNamespace(type="thinking", thinking=""),
                     *([SimpleNamespace(type="text", text=self._text)]
                       if self._text is not None else [])],
            usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000),
        )


def test_anthropic_provider_request_shape_and_cost():
    stub = _AnthropicStub()
    resp = AnthropicProvider(client=stub).complete("claude-opus-5-5", REQ, SCHEMA)
    assert resp.raw_output == '{"value": 1}' and resp.model_version == "claude-opus-5-5"
    assert resp.cost == pytest.approx(4.0 + 2.0)
    kw = stub.kwargs
    assert kw["system"] == "SYS" and kw["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert kw["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert kw["output_config"]["effort"] == "medium"
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]


def test_anthropic_provider_leaves_effort_out_for_haiku_4_5():
    stub = _AnthropicStub()
    AnthropicProvider(client=stub).complete("claude-haiku-4-5", REQ, SCHEMA)
    assert "effort" not in stub.kwargs["output_config"]
    assert "format" in stub.kwargs["output_config"]


def test_anthropic_refusal_raises():
    with pytest.raises(ProviderRefusalError):
        AnthropicProvider(client=_AnthropicStub("refusal")).complete("claude-opus-5-5", REQ,
                                                                      SCHEMA)


@pytest.mark.parametrize("stub", [_AnthropicStub("max_tokens", text='{"val'),
                                  _AnthropicStub(text=None)], ids=["max_tokens", "no-text"])
def test_anthropic_truncated_or_textless_output_raises(stub):
    with pytest.raises(ProviderEmptyOutputError, match="p/s"):
        AnthropicProvider(client=stub).complete("claude-opus-5-5", REQ, SCHEMA)


def _deepseek_stub(content, seen, finish_reason="stop"):
    def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(model="deepseek-v4-pro", choices=[
            SimpleNamespace(message=SimpleNamespace(content=content),
                            finish_reason=finish_reason)])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_deepseek_provider_request_shape():
    seen = {}
    resp = DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen)).complete(
        "deepseek-v4-pro", REQ, SCHEMA)
    assert resp.raw_output == '{"value": 2}' and resp.model_version == "deepseek-v4-pro"
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["messages"][0] == {"role": "system", "content": "SYS"}
    assert "json" in seen["messages"][1]["content"].lower()
    assert "JSON Schema" in seen["messages"][1]["content"]
    assert seen["max_tokens"] == 32768


def test_deepseek_finish_reason_length_is_truncated():
    with pytest.raises(ProviderEmptyOutputError) as ei:
        DeepSeekProvider(client=_deepseek_stub('{"edges": [{"a": "x', {}, "length"),
                         env={}).complete("deepseek-v4-pro", REQ, SCHEMA)
    e = ei.value
    assert e.status == "truncated" and e.raw_output == '{"edges": [{"a": "x'
    assert e.model_version == "deepseek-v4-pro" and e.cost is None
    assert "p/s" in str(e)


def test_deepseek_max_tokens_env_override():
    seen = {}
    DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen),
                     env={"EVOWIKI_DEEPSEEK_MAX_TOKENS": "4096"}).complete(
        "deepseek-v4-pro", REQ, SCHEMA)
    assert seen["max_tokens"] == 4096
    assert deepseek_max_tokens({}) == 32768
    assert deepseek_max_tokens({"EVOWIKI_DEEPSEEK_MAX_TOKENS": " 1000 "}) == 1000


def test_deepseek_max_tokens_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("EVOWIKI_DEEPSEEK_MAX_TOKENS", "2048")
    seen = {}
    DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen)).complete(
        "deepseek-v4-pro", REQ, SCHEMA)
    assert seen["max_tokens"] == 2048


@pytest.mark.parametrize("bad", ["abc", "1.5", "0", "-3"])
def test_deepseek_max_tokens_bad_value_fails_closed(bad):
    with pytest.raises(ValueError, match="EVOWIKI_DEEPSEEK_MAX_TOKENS"):
        DeepSeekProvider(client=_deepseek_stub("{}", {}),
                         env={"EVOWIKI_DEEPSEEK_MAX_TOKENS": bad})


def test_deepseek_empty_output_raises():
    with pytest.raises(ProviderEmptyOutputError):
        DeepSeekProvider(client=_deepseek_stub("", {})).complete("deepseek-v4-pro", REQ, SCHEMA)


def test_providers_from_env_only_builds_configured(monkeypatch):
    built = []
    monkeypatch.setattr("evowiki.llm.providers.AnthropicProvider",
                        lambda: built.append("anthropic") or "A")
    monkeypatch.setattr("evowiki.llm.providers.DeepSeekProvider",
                        lambda: built.append("deepseek") or "D")
    assert providers_from_env({"ANTHROPIC_API_KEY": "x", "DEEPSEEK_API_KEY": ""}) == {
        "anthropic": "A"}
    assert built == ["anthropic"]


def test_missing_env_vars_reports_names_only():
    cfg = GatewayConfig(generator=ModelRef(provider="anthropic", model="claude-opus-5-5"),
                        verifier=ModelRef(provider="deepseek", model="deepseek-v4-pro"))
    assert missing_env_vars(cfg, {}) == ["ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"]
    assert missing_env_vars(cfg, {"DEEPSEEK_API_KEY": "k"}) == ["ANTHROPIC_API_KEY"]


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="live: needs ANTHROPIC_API_KEY")
def test_live_anthropic_smoke():
    resp = AnthropicProvider().complete(
        "claude-opus-5-5",
        LLMRequest(purpose="smoke", prompt_version="v1", subject="s",
                   system="Reply with JSON only.", prompt='Return {"value": 1}.'),
        {"type": "object", "properties": {"value": {"type": "integer"}}})
    assert '"value"' in resp.raw_output


def test_anthropic_refusal_carries_raw_output_and_cost():
    with pytest.raises(ProviderRefusalError) as ei:
        AnthropicProvider(client=_AnthropicStub("refusal", text="I can't")).complete(
            "claude-opus-5-5", REQ, SCHEMA)
    e = ei.value
    assert e.status == "refusal" and e.raw_output == "I can't"
    assert e.model_version == "claude-opus-5-5" and e.cost == pytest.approx(6.0)


def test_anthropic_truncation_and_no_text_carry_status_and_cost():
    with pytest.raises(ProviderEmptyOutputError) as ei:
        AnthropicProvider(client=_AnthropicStub("max_tokens", text='{"val')).complete(
            "claude-opus-5-5", REQ, SCHEMA)
    assert ei.value.status == "truncated" and ei.value.raw_output == '{"val'
    assert ei.value.cost == pytest.approx(6.0) and ei.value.model_version == "claude-opus-5-5"
    with pytest.raises(ProviderEmptyOutputError) as ei:
        AnthropicProvider(client=_AnthropicStub(text=None)).complete("claude-opus-5-5", REQ,
                                                                    SCHEMA)
    assert ei.value.status == "empty_output" and ei.value.raw_output is None
    assert ei.value.cost == pytest.approx(6.0)


def test_deepseek_empty_output_carries_status():
    with pytest.raises(ProviderEmptyOutputError) as ei:
        DeepSeekProvider(client=_deepseek_stub("  ", {})).complete("deepseek-v4-pro", REQ,
                                                                   SCHEMA)
    assert ei.value.status == "empty_output" and ei.value.raw_output == "  "
    assert ei.value.model_version == "deepseek-v4-pro" and ei.value.cost is None


def test_provider_errors_default_to_unknown_fields():
    e = ProviderRefusalError("x")
    assert e.raw_output is None and e.cost is None and e.model_version is None
    assert e.status == "refusal" and ProviderEmptyOutputError("y").status == "empty_output"


def test_deepseek_finish_reason_length_with_no_content_is_truncated():
    with pytest.raises(ProviderEmptyOutputError) as ei:
        DeepSeekProvider(client=_deepseek_stub(None, {}, "length"), env={}).complete(
            "deepseek-v4-pro", REQ, SCHEMA)
    assert ei.value.status == "truncated" and ei.value.raw_output is None
    assert ei.value.model_version == "deepseek-v4-pro"


def test_a_job_budget_cap_lowers_max_tokens_only_when_lower():
    from evowiki.llm.budget import OutputCap, capped
    seen = {}
    with capped(OutputCap(500)) as cap:
        DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen), max_tokens=8192).complete(
            "deepseek-v4-pro", REQ, SCHEMA)
    assert seen["max_tokens"] == 500 and cap is not None and cap.bound
    stub = _AnthropicStub()
    with capped(OutputCap(700)) as cap:
        AnthropicProvider(client=stub).complete("claude-opus-5-5", REQ, SCHEMA)
    assert stub.kwargs["max_tokens"] == 700 and cap is not None and cap.bound
    with capped(OutputCap(50_000)) as cap:          # above the provider's own: unchanged
        DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen), max_tokens=8192).complete(
            "deepseek-v4-pro", REQ, SCHEMA)
    assert seen["max_tokens"] == 8192 and cap is not None and not cap.bound


def test_deepseek_explicit_max_tokens_overrides_env():
    seen = {}
    DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen),
                     env={"EVOWIKI_DEEPSEEK_MAX_TOKENS": "bogus"}, max_tokens=8192).complete(
        "deepseek-v4-pro", REQ, SCHEMA)
    assert seen["max_tokens"] == 8192


@pytest.mark.parametrize("bad", [0, -5, True, 1.5])
def test_deepseek_explicit_max_tokens_must_be_positive_int(bad):
    with pytest.raises(ValueError, match="max_tokens"):
        DeepSeekProvider(client=_deepseek_stub("{}", {}), env={}, max_tokens=bad)


@pytest.mark.parametrize(("thinking", "body"), [(True, {"thinking": {"type": "enabled"}}),
                                                (False, {"thinking": {"type": "disabled"}})])
def test_deepseek_thinking_switch_goes_in_extra_body(thinking, body):
    # api-docs.deepseek.com/guides/thinking_mode: OpenAI format
    # extra_body={"thinking": {"type": "enabled" | "disabled"}}; thinking is on by default
    seen = {}
    DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen), env={},
                     thinking=thinking).complete("deepseek-v4-pro", REQ, SCHEMA)
    assert seen["extra_body"] == body


@pytest.mark.parametrize(("provider_thinking", "params", "sent"), [
    (None, {THINKING_PARAM: False, TEMPERATURE_PARAM: 0}, 0),   # the request turns thinking off
    (False, {TEMPERATURE_PARAM: 0.3}, 0.3),                     # the provider has it off
    (True, {TEMPERATURE_PARAM: 0}, None),                       # DeepSeek ignores it while thinking
    (None, {TEMPERATURE_PARAM: 0}, None),                       # on by API default
    (False, {}, None)])                                         # not asked for: the API's default
def test_deepseek_temperature_only_without_thinking(provider_thinking, params, sent):
    seen = {}
    DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen), env={},
                     thinking=provider_thinking).complete(
        "deepseek-v4-pro", REQ.model_copy(update={"params": params}), SCHEMA)
    assert seen.get("temperature") == sent


def test_deepseek_thinking_unset_sends_no_switch():
    seen = {}
    DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen), env={}).complete(
        "deepseek-v4-pro", REQ, SCHEMA)
    assert "extra_body" not in seen


def test_deepseek_effort_rides_with_the_thinking_switch():
    # same page: reasoning_effort low / high / max, default high; per purpose
    seen = {}
    DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen), env={}, thinking=True,
                     effort_for=lambda purpose: "low").complete("deepseek-v4-pro", REQ, SCHEMA)
    assert seen["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}


@pytest.mark.parametrize(("thinking", "effort", "body"), [
    (False, "low", {"thinking": {"type": "disabled"}}),       # no effort without thinking
    (True, None, {"thinking": {"type": "enabled"}}),          # a purpose with no effort: default
    (None, "low", {"reasoning_effort": "low"})])              # thinking on by API default
def test_deepseek_effort_only_when_thinking(thinking, effort, body):
    seen = {}
    DeepSeekProvider(client=_deepseek_stub('{"value": 2}', seen), env={}, thinking=thinking,
                     effort_for=lambda purpose: effort).complete("deepseek-v4-pro", REQ, SCHEMA)
    assert seen["extra_body"] == body


@pytest.mark.parametrize("bad", ["yes", 1, 0])
def test_deepseek_thinking_must_be_bool_or_none(bad):
    with pytest.raises(ValueError, match="thinking"):
        DeepSeekProvider(client=_deepseek_stub("{}", {}), env={}, thinking=bad)


def test_strict_schema_gives_every_untyped_field_a_scalar_type():
    """Anthropic structured outputs reject a schema node without a type (live 2026-10-14: the
    goal refiner's `refined_goal: Any`): such a node is sent as any JSON scalar. Every LLM output
    schema of the goal intake and the probe renders with no untyped node left."""
    from evowiki.goals import intake, probe
    from evowiki.llm.providers import _typeless, strict_schema

    def nodes(x):
        if isinstance(x, dict):
            yield x
            for v in x.values():
                yield from nodes(v)
        elif isinstance(x, list):
            for v in x:
                yield from nodes(v)

    schemas = [m for mod in (intake, probe) for m in vars(mod).values()
               if isinstance(m, type) and m.__name__.endswith("Out") and hasattr(m, "model_json_schema")]
    assert {"_OneLevelOut", "_EnterOut"} <= {m.__name__ for m in schemas}
    for m in schemas:
        out = strict_schema(m.model_json_schema())
        props = [n for n in nodes(out) if isinstance(n.get("properties"), dict)]
        for parent in props:
            for name, field in parent["properties"].items():
                assert not _typeless(field), (m.__name__, name)
        assert all(p.get("additionalProperties") is False for p in props), m.__name__
    assert strict_schema({"type": "object", "properties": {"x": {"title": "X"}}})["properties"]["x"] == {
        "title": "X", "anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"},
                                {"type": "null"}]}


@pytest.mark.parametrize("body", [None, SimpleNamespace(model="deepseek-v4-pro", choices=None),
                                  SimpleNamespace(model="deepseek-v4-pro", choices=[])],
                         ids=["null-body", "null-choices", "no-choices"])
def test_a_deepseek_answer_without_choices_is_a_transient_failure(body):
    """Live harness round 2026-10-28: a 200 with no choices crashed the replay with an
    AttributeError; it is one failed attempt, in both the JSON and the chat path."""
    from evowiki.llm.chat import ChatMessage, ChatRequest
    from evowiki.llm.providers import ProviderTransientError
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: body)))
    with pytest.raises(ProviderTransientError, match="no choices"):
        DeepSeekProvider(client=client).complete("deepseek-v4-pro", REQ, SCHEMA)
    req = ChatRequest(purpose="p", prompt_version="v", subject="s", system="SYS",
                      messages=(ChatMessage(role="user", text="hi"),))
    with pytest.raises(ProviderTransientError, match="no choices"):
        DeepSeekProvider(client=client).chat("deepseek-v4-pro", req)
