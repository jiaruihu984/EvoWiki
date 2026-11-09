from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import PriceTable
from evowiki.llm.usage import Account, NullMeter
from evowiki.llm.usage_control import (
    DEFAULT_PURPOSE_TIERS,
    THINKING_PURPOSES,
    UsageControl,
    tier_for,
)


def test_pin_applies_without_a_user_choice():
    assert tier_for(Account(user_id="u", group="assistant", tier_pin="fast"),
                    "assistant.turn", None) == "fast"


def test_users_explicit_choice_beats_the_pin():
    acct = Account(user_id="u", group="assistant", tier_pin="fast")
    assert tier_for(acct, "assistant.turn", "good") == "good"


def test_pin_beats_the_purpose_default():
    acct = Account(user_id="u", group="other", tier_pin="good")
    assert DEFAULT_PURPOSE_TIERS["sources.toc"] == "fast"
    assert tier_for(acct, "sources.toc", None) == "good"


def test_planning_and_splitting_always_think():
    # 规划和拆分必须用思考模型 (2026-10-23): the user's fast choice for the group, or a pin, does not apply
    for purpose in ("goals.intake", "goals.enter", "goals.path", "graph.group_naming"):
        assert purpose in THINKING_PURPOSES
        assert tier_for(Account(user_id="u", group="diagnostic", tier_pin="fast"), purpose, "fast") == "good"
    # not planning: diagnosis questions and chat answers still follow the user's choice
    assert tier_for(Account(user_id="u", group="diagnostic"), "goals.probe", "fast") == "fast"
    assert tier_for(Account(user_id="u", group="assistant"), "sources.toc", None) == "fast"


def test_no_pin_is_todays_result():
    acct = Account(user_id="u", group="other")
    assert tier_for(acct, "sources.toc", None) == "fast"         # listed purpose default
    assert tier_for(acct, "unlisted.purpose", None) == "good"    # group default
    assert tier_for(acct, None, None) == "good"


def test_tier_configs_none_ignores_the_pin():
    default = GatewayConfig(generator=ModelRef(provider="deepseek", model="m"),
                            verifier=ModelRef(provider="anthropic", model="m"))
    control = UsageControl(meter=NullMeter(), prices=PriceTable({}), tier_configs=None)
    acct = Account(user_id="u", group="assistant", tier_pin="fast")
    assert control.config_for(acct, default, "assistant.turn") is default
