import pytest

from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import Price, PriceTable, load_prices
from evowiki.llm.usage_control import check_prices
from evowiki.runtime.usage import runtime_tier_configs

GOOD = GatewayConfig(generator=ModelRef(provider="gen", model="g-1"),
                     verifier=ModelRef(provider="ver", model="v-1"))
FAST = GatewayConfig(generator=ModelRef(provider="gen", model="g-fast"),
                     verifier=ModelRef(provider="ver", model="v-1"))


def test_all_priced_passes():
    prices = PriceTable({k: Price(1.0, 1.0) for k in ("gen:g-1", "ver:v-1", "gen:g-fast")})
    assert check_prices([GOOD, FAST], prices) is None


def test_missing_models_listed_once_sorted():
    prices = PriceTable({"gen:g-1": Price(1.0, 2.0)})
    with pytest.raises(ValueError, match="no price for") as e:
        check_prices([GOOD, FAST], prices)
    msg = str(e.value)
    assert "gen:g-fast, ver:v-1 in the price table" in msg
    assert msg.count("ver:v-1") == 1
    assert "gen:g-1" not in msg


@pytest.mark.parametrize("env", [{}, {"EVOWIKI_RUNTIME_PROVIDER": "deepseek"}])
def test_shipped_defaults_are_priced(env):
    check_prices(runtime_tier_configs(env).values(), load_prices())


def test_env_override_to_unpriced_model_refuses():
    env = {"EVOWIKI_RUNTIME_PROVIDER": "deepseek", "EVOWIKI_DEEPSEEK_FAST_MODEL": "deepseek-x"}
    with pytest.raises(ValueError, match="deepseek-nothink:deepseek-x"):
        check_prices(runtime_tier_configs(env).values(), load_prices())


def test_both_deepseek_tiers_are_priced_under_their_own_provider_names():
    tiers = runtime_tier_configs({"EVOWIKI_RUNTIME_PROVIDER": "deepseek"})
    prices = load_prices()
    good, fast = tiers["good"].generator, tiers["fast"].generator
    assert good.provider != fast.provider
    assert prices.price(good.provider, good.model) is not None
    assert prices.price(fast.provider, fast.model) is not None
    # an env override of the fast model to pro is priced too
    assert prices.price(fast.provider, "deepseek-v4-pro") is not None
