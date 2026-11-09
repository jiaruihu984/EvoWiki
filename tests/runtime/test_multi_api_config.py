import pytest

from evowiki.llm.catalog import PROVIDERS
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.openai_compatible import OpenAICompatibleProvider
from evowiki.runtime.llm import (
    RUNTIME_ENV_KEYS,
    check_runtime_provider,
    provider_config_problem,
    runtime_deep_providers,
    runtime_gateway_config,
    runtime_providers,
)
from evowiki.runtime.usage import runtime_deep_config, runtime_tier_configs
from tests.runtime.fake_sdks import install_fake_sdks


def provider_env(name):
    spec = PROVIDERS[name]
    env = {spec.key_env: "fake-key"}
    if name == "openai_compatible":
        env.update({spec.base_url_env: "https://example.invalid/v1", spec.model_env: "private-model"})
    return env


@pytest.mark.parametrize("name", PROVIDERS)
def test_startup_keeps_explicit_selection_without_credentials_for_local_setup(name):
    env = provider_env(name)
    check_runtime_provider(env)
    assert runtime_gateway_config(env).generator.provider == name
    check_runtime_provider({**env, "EVOWIKI_RUNTIME_PROVIDER": name})
    missing = {**env, PROVIDERS[name].key_env: "", "EVOWIKI_RUNTIME_PROVIDER": name}
    check_runtime_provider(missing)
    assert runtime_gateway_config(missing).generator.provider == name
    assert provider_config_problem(runtime_gateway_config(missing), ())
    if name != "anthropic":
        assert runtime_gateway_config({**env, "ANTHROPIC_API_KEY": "fake"}).generator.provider == "anthropic"


@pytest.mark.parametrize("name", ["openai", "gemini", "kimi", "glm", "openai_compatible"])
def test_new_provider_gateway_tiers_writer_and_endpoint_are_consistent(monkeypatch, name):
    made = install_fake_sdks(monkeypatch)
    spec = PROVIDERS[name]
    env = {**provider_env(name), spec.base_url_env: "https://example.invalid/v1"}
    config = runtime_gateway_config(env)
    model = env.get(spec.model_env) or spec.default_model
    assert config.generator == config.verifier
    assert config.generator.model == model and config.single_provider
    assert config.generator.reasoning_effort is None  # initial gateway lets the API choose
    tiers = runtime_tier_configs(env)
    for selected in (*tiers.values(), runtime_deep_config(env)):
        assert selected.generator.provider == name and selected.generator.model == model
        assert selected.generator == selected.verifier
        assert selected.generator.reasoning_effort == ("default" if name == "openai_compatible" else "low")
    for factories in (runtime_providers, runtime_deep_providers):
        got = factories(env)
        assert set(got) == {name}
        provider = got[name]
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.name == name
        assert provider._timeout_for("deep.write") == 300
        assert provider._timeout_for("assistant.turn") == 180
        assert provider._http_client_factory is not None
        assert made["deepseek"]["base_url"] == "https://example.invalid/v1"
        assert made["deepseek"]["api_key"] == "fake-key"
        assert made["deepseek"]["max_retries"] == 0
    assert RUNTIME_ENV_KEYS[name] == spec.key_env
    assert spec.key_env in provider_config_problem(config, ())


def test_custom_requires_complete_configuration_without_breaking_other_providers(monkeypatch):
    install_fake_sdks(monkeypatch)
    env = {"EVOWIKI_COMPATIBLE_API_KEY": "fake", "OPENAI_API_KEY": "fake"}
    assert runtime_gateway_config(env).generator.provider == "openai"
    assert set(runtime_providers(env)) == {"openai"}
    with pytest.raises(ValueError, match="EVOWIKI_COMPATIBLE_MODEL"):
        check_runtime_provider({**env, "EVOWIKI_RUNTIME_PROVIDER": "openai_compatible"})
    with pytest.raises(ValueError, match="EVOWIKI_COMPATIBLE_BASE_URL"):
        check_runtime_provider({**env, "EVOWIKI_RUNTIME_PROVIDER": "openai_compatible",
                                "EVOWIKI_COMPATIBLE_MODEL": "model"})


def test_existing_provider_base_url_overrides_reach_clients(monkeypatch):
    made = install_fake_sdks(monkeypatch)
    runtime_providers({"ANTHROPIC_API_KEY": "fake", "DEEPSEEK_API_KEY": "fake",
                       "ANTHROPIC_BASE_URL": "https://anth.invalid",
                       "DEEPSEEK_BASE_URL": "https://ds.invalid"})
    assert made["anthropic"]["base_url"] == "https://anth.invalid"
    assert made["deepseek"]["base_url"] == "https://ds.invalid"


def test_unknown_provider_diagnostic_does_not_raise_key_error():
    ref = ModelRef(provider="private", model="model")
    config = GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True)
    assert "credentials for private" in provider_config_problem(config, ())
