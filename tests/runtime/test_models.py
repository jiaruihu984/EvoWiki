"""Concrete catalog capabilities and availability; no SDK or network calls."""

import pytest

from evowiki.llm.catalog import PROVIDERS, model_specs, reasoning_levels, supports_images
from evowiki.llm.usage_control import TIERS
from evowiki.runtime.llm import runtime_gateway_config
from evowiki.runtime.models import (
    BAD_KEY,
    NO_ACCESS,
    NO_BALANCE,
    NO_KEY,
    ModelChoice,
    default_model,
    model_states,
    refusal_reason,
    runtime_model_configs,
    runtime_writer_configs,
)

BOTH = {"ANTHROPIC_API_KEY": "fake-a", "DEEPSEEK_API_KEY": "fake-d"}


def rejected(msg):
    return {"error_type": "ProviderRejectedError", "error_message": msg}


def test_catalog_separates_provider_and_concrete_models_from_usage_tiers():
    specs = model_specs(BOTH)
    configs = runtime_model_configs(BOTH)
    assert set(configs) == {s.id for s in specs if s.provider in {"anthropic", "deepseek"}}
    assert len([s for s in specs if s.provider == "anthropic"]) >= 2
    assert len([s for s in specs if s.provider == "deepseek"]) >= 2
    for spec in specs:
        if spec.id not in configs:
            continue
        assert set(configs[spec.id]) == set(TIERS)
        for config in configs[spec.id].values():
            assert config.generator.provider == spec.provider
            assert config.generator.model == spec.model
            assert config.generator.reasoning_effort == spec.default_reasoning
            assert config.verifier == config.generator
        assert configs[spec.id]["fast"] == configs[spec.id]["good"]
    assert runtime_model_configs({}) == {}


def test_every_vendor_and_configured_custom_models_are_visible():
    env = {s.key_env: "fake" for s in PROVIDERS.values()}
    env.update(EVOWIKI_COMPATIBLE_BASE_URL="https://example.invalid/v1",
               EVOWIKI_COMPATIBLE_MODEL="org/custom", EVOWIKI_COMPATIBLE_MODELS="other, org/custom",
               EVOWIKI_OPENAI_MODELS="gpt-5.2-pro,private-model")
    specs = model_specs(env)
    assert {s.provider for s in specs} == set(PROVIDERS)
    assert len({s.id for s in specs}) == len(specs)
    assert {"openai/gpt-5.2-pro", "openai/private-model", "openai_compatible/org/custom",
            "openai_compatible/other"} <= {s.id for s in specs}
    assert set(runtime_model_configs(env)) == {s.id for s in specs}
    missing_url = {k: v for k, v in env.items() if k != "EVOWIKI_COMPATIBLE_BASE_URL"}
    assert not any(k.startswith("openai_compatible/") for k in runtime_model_configs(missing_url))


def test_unknown_models_only_offer_provider_default_reasoning_and_no_false_vision():
    for provider in PROVIDERS:
        assert reasoning_levels(provider, "private-unrecognized-model") == ("default",)
        assert not supports_images(provider, "private-unrecognized-model")
    assert reasoning_levels("openai", "gpt-5.2-pro") == ("medium", "high", "xhigh")
    assert "none" not in reasoning_levels("anthropic", "claude-opus-5-5")
    assert "max" not in reasoning_levels("gemini", "gemini-2.5-flash")
    assert not supports_images("deepseek", "deepseek-v4-pro")
    assert not supports_images("openai_compatible", "gpt-5.2")
    assert supports_images("openai_compatible", "custom", {"EVOWIKI_COMPATIBLE_VISION": "true"})


def test_default_is_canonical_and_explicit_removed_choice_is_preserved_for_rejection():
    config = runtime_gateway_config({**BOTH, "EVOWIKI_RUNTIME_PROVIDER": "deepseek"})
    assert default_model(config) == f"deepseek/{config.generator.model}"
    choice = ModelChoice(runtime_model_configs(BOTH), default_model(config), catalog=model_specs(BOTH))
    assert choice.current(None) == default_model(config)
    assert choice.current("removed/model") == "removed/model"
    assert choice.reasoning("removed/model", "high") is None
    assert choice.reasoning(default_model(config), "not-supported") == choice.spec(default_model(config)).default_reasoning


@pytest.mark.parametrize("message,reason", [
    ("HTTP 402", NO_BALANCE), ("HTTP 400 Your credit balance is too low", NO_BALANCE),
    ("HTTP 401 invalid x-api-key", BAD_KEY), ("HTTP 403", NO_ACCESS),
])
def test_refusal_reasons(message, reason):
    assert refusal_reason(rejected(message)) == reason


def test_transient_failures_do_not_disable_a_model():
    assert refusal_reason({"error_type": "ProviderBusyError", "error_message": "HTTP 429"}) == ""
    assert refusal_reason(None) == ""


def test_availability_keeps_missing_keys_and_matches_refusals_to_concrete_model():
    specs = model_specs(BOTH)
    configs = runtime_model_configs(BOTH)
    seen = []
    rejected_id = "deepseek/deepseek-v4-pro"

    def latest(providers, model):
        seen.append((providers, model))
        return rejected("HTTP 402") if f"{providers[0]}/{model}" == rejected_id else None

    states = {s.id: s for s in model_states(configs, latest, specs)}
    assert len(states) == len(specs)
    assert states[rejected_id].reason == NO_BALANCE
    assert states["deepseek/deepseek-v4-flash"].available
    assert states["openai/gpt-5.2"].reason == NO_KEY
    assert not states["openai/gpt-5.2"].available
    assert all(providers[0] in {"anthropic", "deepseek"} for providers, _ in seen)
    assert states["openai/gpt-5.2"].provider_name == "OpenAI"


def test_every_configured_model_has_the_same_concrete_writer():
    configs, writers = runtime_model_configs(BOTH), runtime_writer_configs(BOTH)
    assert set(writers) == set(configs)
    for key, writer in writers.items():
        assert writer == configs[key]["fast"] == configs[key]["good"]
