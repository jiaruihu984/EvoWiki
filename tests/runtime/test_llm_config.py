import builtins

import pytest

from evowiki.runtime.llm import DEEPSEEK_NOTHINK, runtime_gateway_config, runtime_providers
from tests.runtime.fake_sdks import install_fake_sdks


def test_runtime_config_defaults_and_overrides():
    cfg = runtime_gateway_config({})
    assert (cfg.generator.provider, cfg.generator.model) == ("anthropic", "claude-sonnet-5-5")
    assert cfg.verifier.provider == "deepseek"
    assert runtime_gateway_config({"EVOWIKI_RUNTIME_MODEL": "m"}).generator.model == "m"


@pytest.mark.parametrize(("keys", "provider", "verifier"), [
    ({"DEEPSEEK_API_KEY": "fake"}, "deepseek", "deepseek"),
    ({"ANTHROPIC_API_KEY": "fake"}, "anthropic", "anthropic"),
    ({"ANTHROPIC_API_KEY": "fake", "DEEPSEEK_API_KEY": "fake"}, "anthropic", "deepseek"),
])
def test_auto_model_selection_is_consistent_with_the_page_writer(keys, provider, verifier):
    from evowiki.runtime.usage import runtime_deep_config, runtime_tier_configs

    for env in (keys, {**keys, "EVOWIKI_RUNTIME_PROVIDER": "auto"}):
        config = runtime_gateway_config(env)
        assert config.generator.provider == provider and config.verifier.provider == verifier
        deep = runtime_deep_config(env)     # the page writer: DeepSeek without thinking
        nothink = {"deepseek": DEEPSEEK_NOTHINK}
        assert deep.generator.provider == nothink.get(provider, provider)
        assert deep.verifier.provider == nothink.get(verifier, verifier)
        assert runtime_tier_configs(env)["good"] == config


def test_explicit_provider_also_selects_the_page_writer():
    from evowiki.runtime.usage import runtime_deep_config, runtime_deep_fallback_config

    env = {"ANTHROPIC_API_KEY": "fake", "DEEPSEEK_API_KEY": "fake", "EVOWIKI_RUNTIME_PROVIDER": "deepseek"}
    assert runtime_gateway_config(env).generator.provider == "deepseek"
    assert runtime_deep_config(env).generator.provider == DEEPSEEK_NOTHINK
    assert runtime_deep_fallback_config(env) is None


def test_providers_need_keys_and_skip_missing_sdks(monkeypatch):
    assert runtime_providers({}) == {}
    real_import = builtins.__import__

    def no_sdk(name, *a, **kw):
        if name in ("anthropic", "openai"):
            raise ImportError(name)
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_sdk)
    assert runtime_providers({"ANTHROPIC_API_KEY": "x", "DEEPSEEK_API_KEY": "y"}) == {}


def test_runtime_provider_anthropic_is_default_and_explicit():
    for env in ({}, {"EVOWIKI_RUNTIME_PROVIDER": "anthropic"}):
        cfg = runtime_gateway_config(env)
        assert (cfg.generator.provider, cfg.verifier.provider) == ("anthropic", "deepseek")
        assert not cfg.allow_single_provider


def test_runtime_provider_deepseek_is_single_provider_mode():
    cfg = runtime_gateway_config({"EVOWIKI_RUNTIME_PROVIDER": "deepseek"})
    assert cfg.generator == cfg.verifier
    assert (cfg.generator.provider, cfg.generator.model) == ("deepseek", "deepseek-v4-pro")
    assert cfg.allow_single_provider and cfg.single_provider
    cfg = runtime_gateway_config({"EVOWIKI_RUNTIME_PROVIDER": "deepseek",
                                  "EVOWIKI_DEEPSEEK_MODEL": "m"})
    assert cfg.generator.model == cfg.verifier.model == "m"


def test_runtime_provider_rejects_unknown_value():
    with pytest.raises(ValueError, match="EVOWIKI_RUNTIME_PROVIDER"):
        runtime_gateway_config({"EVOWIKI_RUNTIME_PROVIDER": "unknown"})


def test_provider_clients_never_retry_and_carry_the_deep_ceiling(monkeypatch):
    from evowiki.llm.providers import DEEPSEEK_BASE_URL, AnthropicProvider, DeepSeekProvider
    from evowiki.runtime.llm import (
        RUNTIME_RECORD_MAX_ATTEMPTS,
        RUNTIME_TIMEOUT_DEEP_S,
    )

    made = install_fake_sdks(monkeypatch)  # works with or without the `llm` extra
    # the pipeline's max_tokens override must not leak into the runtime
    got = runtime_providers({"ANTHROPIC_API_KEY": "k-a", "DEEPSEEK_API_KEY": "k-d",
                             "EVOWIKI_DEEPSEEK_MAX_TOKENS": "65536"})
    assert isinstance(got["anthropic"], AnthropicProvider)
    assert isinstance(got["deepseek"], DeepSeekProvider)
    assert got["anthropic"]._client == "client:anthropic"
    assert got["deepseek"]._client == "client:deepseek"
    for kw in made.values():
        assert kw["timeout"] == RUNTIME_TIMEOUT_DEEP_S
        assert kw["max_retries"] == 0
    assert made["anthropic"]["api_key"] == "k-a"
    assert made["deepseek"] == {"api_key": "k-d", "base_url": DEEPSEEK_BASE_URL,
                                "timeout": RUNTIME_TIMEOUT_DEEP_S, "max_retries": 0}
    assert got["deepseek"]._max_tokens == 8192
    assert RUNTIME_RECORD_MAX_ATTEMPTS == 1


@pytest.mark.parametrize("purpose, tier, expected", [
    ("organize", "fast", 60.0), ("organize", "good", 180.0),
    ("extract.claims", "good", 180.0), ("extract.claims", "fast", 60.0),
    ("deep.plan", "fast", 300.0), ("deep.refine", "good", 300.0),
    ("deeper.x", "good", 180.0), ("xdeep.x", "fast", 60.0),
])
def test_runtime_timeout_table(purpose, tier, expected):
    from evowiki.runtime.llm import runtime_timeout

    assert runtime_timeout(purpose, tier) == expected


def test_runtime_providers_get_a_tiered_per_request_timeout(monkeypatch):
    install_fake_sdks(monkeypatch)
    anth = runtime_providers({"ANTHROPIC_API_KEY": "k-a", "DEEPSEEK_API_KEY": "k-d"})
    assert anth["anthropic"]._timeout_for("organize") == 180.0
    assert anth["deepseek"]._timeout_for("organize") == 180.0   # verifier = good
    assert anth["deepseek"]._timeout_for("deep.plan") == 300.0
    ds = runtime_providers({"DEEPSEEK_API_KEY": "k-d", "EVOWIKI_RUNTIME_PROVIDER": "deepseek"})
    assert ds["deepseek"]._timeout_for("organize") == 180.0
    assert ds["deepseek-nothink"]._timeout_for("organize") == 60.0
    assert ds["deepseek-nothink"]._timeout_for("deep.plan") == 300.0


def test_missing_runtime_providers_names_roles_and_suggests_a_fix():
    from evowiki.runtime.llm import missing_runtime_providers, provider_config_problem

    cfg = runtime_gateway_config({})
    assert missing_runtime_providers(cfg, {"deepseek"}) == ["anthropic"]
    assert missing_runtime_providers(cfg, {"anthropic", "deepseek"}) == []
    msg = provider_config_problem(cfg, {"deepseek"})
    assert "EVOWIKI_RUNTIME_PROVIDER=deepseek" in msg and "ANTHROPIC_API_KEY" in msg
    assert provider_config_problem(cfg, {"anthropic", "deepseek"}) is None
    ds = runtime_gateway_config({"EVOWIKI_RUNTIME_PROVIDER": "deepseek"})
    msg = provider_config_problem(ds, set())
    assert "DEEPSEEK_API_KEY" in msg and "EVOWIKI_RUNTIME_PROVIDER=deepseek" not in msg


def test_deepseek_mode_builds_a_thinking_and_a_non_thinking_provider(monkeypatch):
    from evowiki.llm.providers import DeepSeekProvider
    from evowiki.runtime.llm import (
        DEEPSEEK_NOTHINK,
        RUNTIME_DEEPSEEK_GOOD_MAX_TOKENS,
        RUNTIME_DEEPSEEK_MAX_TOKENS,
    )

    install_fake_sdks(monkeypatch)
    got = runtime_providers({"DEEPSEEK_API_KEY": "k-d", "EVOWIKI_RUNTIME_PROVIDER": "deepseek"})
    assert set(got) == {"deepseek", DEEPSEEK_NOTHINK}
    good, fast = got["deepseek"], got[DEEPSEEK_NOTHINK]
    assert isinstance(good, DeepSeekProvider) and isinstance(fast, DeepSeekProvider)
    assert (good._thinking, good._max_tokens) == (True, RUNTIME_DEEPSEEK_GOOD_MAX_TOKENS)
    assert (fast._thinking, fast._max_tokens) == (False, RUNTIME_DEEPSEEK_MAX_TOKENS)
    assert (RUNTIME_DEEPSEEK_GOOD_MAX_TOKENS, RUNTIME_DEEPSEEK_MAX_TOKENS) == (16384, 8192)


def test_anthropic_mode_deepseek_verifier_is_unchanged(monkeypatch):
    install_fake_sdks(monkeypatch)
    got = runtime_providers({"ANTHROPIC_API_KEY": "k-a", "DEEPSEEK_API_KEY": "k-d"})
    # deepseek-nothink and anthropic-think too: the assistant composer's models and levels (runtime.models)
    assert set(got) == {"anthropic", "anthropic-think", "deepseek", "deepseek-nothink"}
    assert (got["anthropic"]._effort, got["anthropic-think"]._effort) == ("low", "high")
    assert (got["deepseek"]._thinking, got["deepseek"]._max_tokens) == (None, 8192)
    assert (got["deepseek-nothink"]._thinking, got["deepseek-nothink"]._max_tokens) == (False, 8192)


def test_the_strict_flag_reaches_every_runtime_deepseek_provider(monkeypatch):
    import pytest

    from evowiki.llm.providers import DEEPSEEK_BETA_BASE_URL
    from evowiki.runtime.llm import runtime_deep_providers

    class Client:
        def __init__(self, base_url="main"):
            self.base_url = base_url

        def with_options(self, **kw):
            return Client(kw["base_url"])

    monkeypatch.setattr("evowiki.runtime.llm._deepseek_client", lambda env: Client())
    env = {"DEEPSEEK_API_KEY": "k-d", "EVOWIKI_RUNTIME_PROVIDER": "deepseek"}
    off = runtime_providers(env)
    assert not any(p._strict for p in off.values())
    on = {**env, "EVOWIKI_DEEPSEEK_STRICT": "1"}
    got = [*runtime_providers(on).values(), *runtime_deep_providers(on).values()]
    assert len(got) == 4 and all(p._strict for p in got)
    assert {p._strict_client.base_url for p in got} == {DEEPSEEK_BETA_BASE_URL}
    with pytest.raises(ValueError, match="EVOWIKI_DEEPSEEK_STRICT"):
        runtime_providers({**env, "EVOWIKI_DEEPSEEK_STRICT": "yes please"})


def test_non_thinking_provider_needs_the_deepseek_key():
    from evowiki.runtime.llm import DEEPSEEK_NOTHINK, provider_config_problem
    from evowiki.runtime.usage import runtime_tier_configs

    fast = runtime_tier_configs({"EVOWIKI_RUNTIME_PROVIDER": "deepseek"})["fast"]
    assert provider_config_problem(fast, {"deepseek", DEEPSEEK_NOTHINK}) is None
    assert "DEEPSEEK_API_KEY" in provider_config_problem(fast, {"deepseek"})
