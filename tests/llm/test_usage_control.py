import pytest

from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.usage import Account, NullMeter, Usage
from evowiki.llm.usage_control import (
    DEFAULT_PURPOSE_TIERS,
    DEFAULT_TIERS,
    GROUPS,
    TIERS,
    DefaultTiers,
    UsageControl,
)

GOOD = GatewayConfig(generator=ModelRef(provider="gen", model="g-1"),
                     verifier=ModelRef(provider="ver", model="v-1"))
FAST = GatewayConfig(generator=ModelRef(provider="gen", model="g-fast"),
                     verifier=ModelRef(provider="ver", model="v-fast"))
PRICES = PriceTable({"gen:g-1": Price(1.0, 2.0), "gen:g-1-0901": Price(3.0, 3.0),
                     "gen:c-1": Price(1.0, 2.0, 0.1, 1.25)})


class Fixed:
    def __init__(self, tier):
        self.tier = tier

    def chosen_tier(self, account):
        return self.tier


def test_default_tiers_cover_every_group():
    assert set(DEFAULT_TIERS) == set(GROUPS) and set(DEFAULT_TIERS.values()) <= set(TIERS)
    assert DEFAULT_TIERS["page"] == DEFAULT_TIERS["assistant"] == "good"
    assert DEFAULT_TIERS["curator"] == DEFAULT_TIERS["review"] == "fast"


def test_without_tier_configs_every_account_uses_the_gateway_config():
    uc = UsageControl(NullMeter(), PRICES)
    assert uc.config_for(Account(user_id="u", group="curator"), GOOD) is GOOD


def test_tier_configs_route_by_tier():
    uc = UsageControl(NullMeter(), PRICES, tier_configs={"fast": FAST, "good": GOOD})
    assert uc.config_for(Account(user_id="u", group="curator"), GOOD) == FAST
    assert uc.config_for(Account(user_id="u", group="page"), GOOD) == GOOD
    uc = UsageControl(NullMeter(), PRICES, tier_configs={"fast": FAST, "good": GOOD},
                      tiers=Fixed("fast"))
    assert uc.config_for(Account(user_id="u", group="page"), GOOD) == FAST


def test_tier_configs_must_cover_both_tiers():
    with pytest.raises(ValueError, match="fast"):
        UsageControl(NullMeter(), PRICES, tier_configs={"good": GOOD})


def test_usage_of_prices_served_model_version_first_then_configured_model():
    uc = UsageControl(NullMeter(), PRICES)
    served = uc.usage_of("gen", "g-1", "g-1-0901", 1000, 1000)
    assert served.model_copy(update={"cost_usd": None}) == Usage(
        provider="gen", model="g-1", input_tokens=1000, output_tokens=1000)
    assert served.cost_usd == pytest.approx(0.006)
    assert uc.usage_of("gen", "g-1", "unknown", 1000, 200).cost_usd == pytest.approx(0.0014)
    assert uc.usage_of("gen", "g-1", None, 1000, 200).cost_usd == pytest.approx(0.0014)


def test_usage_of_unpriced_or_unreported_has_no_cost():
    uc = UsageControl(NullMeter(), PRICES)
    assert uc.usage_of("ver", "v-1", None, 5, 6) == Usage(
        provider="ver", model="v-1", input_tokens=5, output_tokens=6, cost_usd=None)
    assert uc.usage_of("gen", "g-1", None, None, 7) == Usage(
        provider="gen", model="g-1", input_tokens=0, output_tokens=7, cost_usd=None)


def test_purpose_tiers_follow_r1():
    good = {p for p, t in DEFAULT_PURPOSE_TIERS.items() if t == "good"}
    fast = {p for p, t in DEFAULT_PURPOSE_TIERS.items() if t == "fast"}
    # planning / splitting purposes are not here: THINKING_PURPOSES routes them (always good)
    assert good == set()
    assert fast == {"sources.toc", "sources.summary", "sources.glossary",
                    "sources.exam", "sources.exam_attach"}


def test_purpose_default_beats_group_default():
    uc = UsageControl(NullMeter(), PRICES, tier_configs={"fast": FAST, "good": GOOD})
    assert uc.config_for(Account(user_id="u", group="curator"), GOOD, "graph.group_naming") == GOOD
    assert uc.config_for(Account(user_id="u", group="assistant"), GOOD, "sources.toc") == FAST
    assert uc.config_for(Account(user_id="u", group="page"), GOOD, "goals.intake") == GOOD
    assert uc.config_for(Account(user_id="u", group="page"), GOOD,
                         "wiki.feedback_understand") == GOOD
    # unlisted purpose (or none): the group default
    assert uc.config_for(Account(user_id="u", group="curator"), GOOD, "x.other") == FAST
    assert uc.config_for(Account(user_id="u", group="other"), GOOD, "x.other") == GOOD
    assert uc.config_for(Account(user_id="u", group="curator"), GOOD) == FAST


def test_user_group_choice_beats_purpose_default():
    tiers = {"fast": FAST, "good": GOOD}
    page = Account(user_id="u", group="page")
    uc = UsageControl(NullMeter(), PRICES, tier_configs=tiers, tiers=Fixed("fast"))
    assert uc.config_for(page, GOOD, "wiki.feedback_understand") == FAST
    uc = UsageControl(NullMeter(), PRICES, tier_configs=tiers, tiers=Fixed("good"))
    assert uc.config_for(page, GOOD, "goals.intake") == GOOD
    uc = UsageControl(NullMeter(), PRICES, tier_configs=tiers, tiers=Fixed(None))
    assert uc.config_for(page, GOOD, "sources.toc") == FAST
    # planning / splitting thinks even when the user chose fast for the group
    uc = UsageControl(NullMeter(), PRICES, tier_configs=tiers, tiers=Fixed("fast"))
    assert uc.config_for(page, GOOD, "goals.intake") == GOOD


def test_default_tier_source_has_no_user_choice():
    assert DefaultTiers().chosen_tier(Account(user_id="u", group="page")) is None


def test_usage_of_prices_and_keeps_cache_tokens():
    uc = UsageControl(NullMeter(), PRICES)
    u = uc.usage_of("gen", "c-1", None, 1000, 200, 10_000, 2000)
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (
        1000, 200, 10_000, 2000)
    assert u.cost_usd == pytest.approx((1000 * 1 + 200 * 2 + 10_000 * 0.1 + 2000 * 1.25) / 1e6)
    # a model without cache rates: cache tokens at its input rate
    assert uc.usage_of("gen", "g-1", None, 1000, 200, 10_000, 2000).cost_usd == pytest.approx(
        (13_000 * 1 + 200 * 2) / 1e6)


def test_usage_of_unreported_cache_counts_are_zero():
    uc = UsageControl(NullMeter(), PRICES)
    u = uc.usage_of("gen", "c-1", None, 1000, 200, None, None)
    assert (u.cache_read_tokens, u.cache_write_tokens) == (0, 0)
    assert u.cost_usd == pytest.approx(0.0014)
    assert uc.usage_of("gen", "c-1", None, 1000, 200) == u          # defaults: 0 / 0
    # unreported input/output: cost None, cache counts still recorded
    v = uc.usage_of("gen", "c-1", None, None, 200, 500, 0)
    assert (v.input_tokens, v.cache_read_tokens, v.cost_usd) == (0, 500, None)


class Picks:
    """A TierSource + ModelSource: a fixed tier choice and model pick for every account."""

    def __init__(self, model, tier=None):
        self.model, self.tier = model, tier

    def chosen_tier(self, account):
        return self.tier

    def chosen_model(self, account):
        return self.model


CLAUDE = GatewayConfig(generator=ModelRef(provider="anthropic", model="claude-sonnet-5-5"),
                       verifier=ModelRef(provider="ver", model="v-1"))
CLAUDE_FAST = GatewayConfig(generator=ModelRef(provider="anthropic", model="claude-haiku-4-5"),
                            verifier=ModelRef(provider="ver", model="v-fast"))
TIER_CONFIGS = {"fast": FAST, "good": GOOD}


def test_a_picked_model_routes_every_group_by_its_own_tier():
    models = {"claude": {"fast": CLAUDE_FAST, "good": CLAUDE}}
    uc = UsageControl(NullMeter(), PRICES, tier_configs=TIER_CONFIGS, tiers=Picks("claude"),
                      model_configs=models)
    assistant = Account(user_id="u", group="assistant")
    assert uc.config_for(assistant, GOOD) == CLAUDE                   # assistant: good tier
    assert uc.picked_model(assistant) == "claude"
    fast = UsageControl(NullMeter(), PRICES, tier_configs=TIER_CONFIGS,
                        tiers=Picks("claude", "fast"), model_configs=models)
    assert fast.config_for(assistant, GOOD) == CLAUDE_FAST             # the tier still applies
    # D27: every group follows the user's pick, each at its own tier (curator: fast by default)
    for group in ("page", "diagnostic", "review", "proactive", "rsi", "other"):
        assert uc.picked_model(Account(user_id="u", group=group)) == "claude"
    assert uc.config_for(Account(user_id="u", group="page"), GOOD) == CLAUDE
    assert uc.config_for(Account(user_id="u", group="curator"), GOOD) == CLAUDE_FAST


def test_a_removed_explicit_pick_fails_closed():
    ds = {"deepseek": {"fast": FAST, "good": GOOD}}
    a = Account(user_id="u", group="assistant")
    uc = UsageControl(NullMeter(), PRICES, tier_configs=TIER_CONFIGS, tiers=Picks("claude"),
                      model_configs=ds)
    with pytest.raises(ValueError, match="重新选择模型"):
        uc.config_for(a, GOOD)
    # without tier configs (anthropic mode) a pick still routes; no pick = the gateway config
    assert UsageControl(NullMeter(), PRICES, tiers=Picks("deepseek"),
                        model_configs=ds).config_for(a, CLAUDE) == GOOD
    assert UsageControl(NullMeter(), PRICES, tiers=Picks(None),
                        model_configs=ds).config_for(a, CLAUDE) is CLAUDE
    # a tier source without chosen_model (DefaultTiers) is never asked for one
    assert UsageControl(NullMeter(), PRICES, model_configs=ds).config_for(a, CLAUDE) is CLAUDE


def test_model_configs_must_cover_both_tiers():
    with pytest.raises(ValueError, match="model_configs"):
        UsageControl(NullMeter(), PRICES, model_configs={"claude": {"good": CLAUDE}})


def test_without_a_pick_the_default_model_routes_by_the_users_tier():
    models = {"claude": {"fast": CLAUDE_FAST, "good": CLAUDE}}
    a = Account(user_id="u", group="assistant")
    uc = UsageControl(NullMeter(), PRICES, tiers=Picks(None, "fast"), model_configs=models,
                      default_model="claude")
    assert uc.picked_model(a) == "claude" and uc.config_for(a, GOOD) == CLAUDE_FAST
    with pytest.raises(ValueError, match="默认模型"):
        UsageControl(NullMeter(), PRICES, tiers=Picks(None, "fast"), model_configs=models,
                     default_model="deepseek").config_for(a, GOOD)


def test_planning_purposes_keep_the_thinking_tier_whatever_level_was_picked():
    from evowiki.llm.usage_control import THINKING_PURPOSES

    models = {"claude": {"fast": CLAUDE_FAST, "good": CLAUDE}}
    uc = UsageControl(NullMeter(), PRICES, tiers=Picks("claude", "fast"), model_configs=models)
    a = Account(user_id="u", group="assistant")
    assert uc.config_for(a, GOOD, "assistant.turn") == CLAUDE_FAST
    for purpose in THINKING_PURPOSES:
        assert uc.config_for(a, GOOD, purpose) == CLAUDE
