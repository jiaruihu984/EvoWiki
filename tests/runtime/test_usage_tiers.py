from evowiki.llm.models import ModelRef
from evowiki.runtime.llm import DEEPSEEK_NOTHINK, runtime_gateway_config
from evowiki.runtime.usage import runtime_tier_configs


def test_anthropic_mode_fast_tier_is_haiku_plus_flash():
    tiers = runtime_tier_configs({})
    assert tiers["good"] == runtime_gateway_config({})
    assert tiers["fast"].generator == ModelRef(provider="anthropic", model="claude-haiku-4-5")
    assert tiers["fast"].verifier == ModelRef(provider="deepseek", model="deepseek-v4-flash")
    assert not tiers["fast"].single_provider


def test_deepseek_mode_fast_tier_is_the_same_model_with_thinking_off():
    env = {"EVOWIKI_RUNTIME_PROVIDER": "deepseek"}
    tiers = runtime_tier_configs(env)
    assert tiers["good"] == runtime_gateway_config(env)
    assert tiers["good"].generator == ModelRef(provider="deepseek", model="deepseek-v4-pro")
    # only the thinking mode differs; the non-thinking provider name keeps the two tiers apart
    # in CallLog, usage and prices
    nothink = ModelRef(provider=DEEPSEEK_NOTHINK, model="deepseek-v4-pro")
    assert tiers["fast"].generator == tiers["fast"].verifier == nothink
    assert tiers["fast"].allow_single_provider


def test_deepseek_mode_fast_tier_follows_the_good_model():
    env = {"EVOWIKI_RUNTIME_PROVIDER": "deepseek", "EVOWIKI_DEEPSEEK_MODEL": "ds-m"}
    tiers = runtime_tier_configs(env)
    assert tiers["fast"].generator == ModelRef(provider=DEEPSEEK_NOTHINK, model="ds-m")


def test_fast_models_are_env_overridable():
    tiers = runtime_tier_configs({"EVOWIKI_RUNTIME_FAST_MODEL": "claude-x",
                                  "EVOWIKI_DEEPSEEK_FAST_MODEL": "ds-y"})
    assert tiers["fast"].generator.model == "claude-x"
    assert tiers["fast"].verifier == ModelRef(provider="deepseek", model="ds-y")
    tiers = runtime_tier_configs({"EVOWIKI_RUNTIME_PROVIDER": "deepseek",
                                  "EVOWIKI_DEEPSEEK_FAST_MODEL": "ds-y"})
    assert tiers["fast"].generator == ModelRef(provider=DEEPSEEK_NOTHINK, model="ds-y")
