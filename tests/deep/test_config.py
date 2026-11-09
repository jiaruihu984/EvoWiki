"""The deep tier (plan rev 3.3 §10.2): follows the runtime gateway's provider; every model it can use is priced."""

import pytest

from evowiki.llm.prices import load_prices
from evowiki.llm.usage_control import check_prices
from evowiki.runtime.llm import DEEPSEEK_NOTHINK, runtime_deep_providers, runtime_providers
from evowiki.runtime.usage import runtime_deep_config


def test_claude_writes_and_deepseek_reviews_when_both_keys_exist():
    c = runtime_deep_config({"ANTHROPIC_API_KEY": "a", "DEEPSEEK_API_KEY": "d"})
    assert (c.generator.provider, c.generator.model) == ("anthropic", "claude-sonnet-5-5")
    assert (c.verifier.provider, c.verifier.model) == (DEEPSEEK_NOTHINK, "deepseek-v4-pro")
    assert not c.single_provider


def test_claude_only_reviews_with_claude():
    c = runtime_deep_config({"ANTHROPIC_API_KEY": "a"})
    assert c.generator == c.verifier and c.generator.provider == "anthropic"


def test_without_anthropic_key_deepseek_writes_without_thinking():
    # user ruling 2026-10-28: 「不要开思考，写页面全部开最低就够了吧」
    c = runtime_deep_config({"DEEPSEEK_API_KEY": "d"})
    assert c.generator == c.verifier and (c.generator.provider, c.generator.model) == (
        DEEPSEEK_NOTHINK, "deepseek-v4-pro")


@pytest.mark.parametrize("env", [{}, {"ANTHROPIC_API_KEY": "a"},
                                 {"ANTHROPIC_API_KEY": "a", "DEEPSEEK_API_KEY": "d"}])
def test_every_deep_model_is_priced(env):
    check_prices([runtime_deep_config(env)], load_prices())


def test_an_unpriced_deep_model_fails_the_startup_check():
    with pytest.raises(ValueError, match="anthropic:claude-unknown"):
        check_prices([runtime_deep_config({"ANTHROPIC_API_KEY": "a",
                                           "EVOWIKI_DEEP_MODEL": "claude-unknown"})],
                     load_prices())


def test_deep_providers_follow_the_keys():
    assert runtime_deep_providers({}) == {}
    pytest.importorskip("openai")
    got = runtime_deep_providers({"DEEPSEEK_API_KEY": "d"})
    assert set(got) == {"deepseek", DEEPSEEK_NOTHINK}
    # the writer: thinking off in its chat turns too (plan_page included)
    w = got[DEEPSEEK_NOTHINK]
    assert w._thinking is False and w._chat_thinking is False
    # kept for a pack that picks the good tier for a deep step (H10)
    assert got["deepseek"]._thinking is True and got["deepseek"]._chat_thinking is None


def test_claude_writes_pages_at_the_lowest_effort():
    pytest.importorskip("anthropic")
    got = runtime_deep_providers({"ANTHROPIC_API_KEY": "a"})
    assert got["anthropic"]._effort == "low"
    assert runtime_deep_providers({"ANTHROPIC_API_KEY": "a", "EVOWIKI_DEEP_EFFORT": "high"}
                                  )["anthropic"]._effort == "high"


def test_deepseek_mode_thinks_at_the_default_effort():
    pytest.importorskip("openai")
    got = runtime_providers({"DEEPSEEK_API_KEY": "d", "EVOWIKI_RUNTIME_PROVIDER": "deepseek"})
    assert got["deepseek"]._effort_for("assistant.turn") is None
    assert got[DEEPSEEK_NOTHINK]._thinking is False
    # 快速 does not think in its chat turns either (the composer picker's fast level)
    assert got[DEEPSEEK_NOTHINK]._chat_thinking is False
    assert got["deepseek"]._chat_thinking is None          # 思考: the API's default, thinking on


def test_anthropic_quick_level_is_the_lowest_effort():
    pytest.importorskip("anthropic")
    got = runtime_providers({"ANTHROPIC_API_KEY": "a"})
    assert got["anthropic"]._effort == "low"                # 快速 (the composer's fast level)
    assert got["anthropic-think"]._effort == "high"         # 思考
