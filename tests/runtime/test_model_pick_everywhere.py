"""The per-user concrete model and effort route all product work without provider calls."""

import pytest

from evowiki.deep.llm import DeepLLM
from evowiki.llm.prices import PriceTable
from evowiki.llm.usage import Account, NullMeter
from evowiki.llm.usage_control import GROUPS, THINKING_PURPOSES, UsageControl
from evowiki.runtime.llm import runtime_gateway_config
from evowiki.runtime.models import default_model, runtime_model_configs, runtime_writer_configs
from evowiki.runtime.usage import runtime_deep_config
from tests.runtime.llm_helpers import MemoryCallLog

ENV = {"ANTHROPIC_API_KEY": "fake-a", "DEEPSEEK_API_KEY": "fake-d", "OPENAI_API_KEY": "fake-o"}
TASKS = ("assistant.turn", "goals.intake", "goals.probe", "goals.path", "graph.group_naming",
         "sources.toc", "memory.extract", "bot.reflect", *THINKING_PURPOSES)
PICKS = (("deepseek/deepseek-v4-pro", "deepseek", "deepseek-v4-pro"),
         ("anthropic/claude-opus-5-5", "anthropic", "claude-opus-5-5"),
         ("openai/gpt-5.2", "openai", "gpt-5.2"))


class Picks:
    def __init__(self, model, reasoning=None):
        self.model, self.reasoning = model, reasoning

    def chosen_tier(self, account):
        return "fast"

    def chosen_model(self, account):
        return self.model(account.user_id) if callable(self.model) else self.model

    def chosen_reasoning(self, account):
        return self.reasoning(account.user_id) if callable(self.reasoning) else self.reasoning


@pytest.mark.parametrize("mode", ["anthropic", "deepseek", "openai"])
@pytest.mark.parametrize("picked,provider,model", PICKS)
@pytest.mark.parametrize("effort", ["low", "high"])
def test_every_group_and_purpose_follows_exact_pick_and_effort(mode, picked, provider, model, effort):
    env = {**ENV, "EVOWIKI_RUNTIME_PROVIDER": mode}
    config = runtime_gateway_config(env)
    uc = UsageControl(NullMeter(), PriceTable({}), tiers=Picks(picked, effort),
                      model_configs=runtime_model_configs(env), default_model=default_model(config))
    for group in GROUPS:
        for purpose in TASKS:
            got = uc.config_for(Account(user_id="u", group=group), config, purpose)
            assert (got.generator.provider, got.generator.model, got.generator.reasoning_effort) == (provider, model, effort)
            assert got.verifier == got.generator


def test_user_without_pick_gets_server_model_and_default_reasoning():
    env = {**ENV, "EVOWIKI_RUNTIME_PROVIDER": "deepseek"}
    config = runtime_gateway_config(env)
    configs = runtime_model_configs(env)
    uc = UsageControl(NullMeter(), PriceTable({}), tiers=Picks(None),
                      model_configs=configs, default_model=default_model(config))
    assert uc.config_for(Account(user_id="u", group="page"), config, "goals.path") == configs[default_model(config)]["good"]


def test_picks_and_reasoning_are_isolated_between_users():
    configs = runtime_model_configs(ENV)
    selections = {"one": PICKS[0][0], "two": PICKS[1][0]}
    efforts = {"one": "none", "two": "max"}
    uc = UsageControl(NullMeter(), PriceTable({}), tiers=Picks(selections.get, efforts.get), model_configs=configs)
    for user, selection in selections.items():
        got = uc.config_for(Account(user_id=user, group="assistant"), runtime_gateway_config(ENV), "assistant.turn")
        assert f"{got.generator.provider}/{got.generator.model}" == selection
        assert got.generator.reasoning_effort == efforts[user]


def test_planning_retains_minimum_thinking_without_changing_the_selected_model():
    configs = runtime_model_configs(ENV)
    uc = UsageControl(NullMeter(), PriceTable({}), tiers=Picks(PICKS[0][0], "none"), model_configs=configs)
    account = Account(user_id="u", group="assistant")
    chat = uc.config_for(account, runtime_gateway_config(ENV), "assistant.turn").generator
    plan = uc.config_for(account, runtime_gateway_config(ENV), "goals.intake").generator
    assert (chat.provider, chat.model) == (plan.provider, plan.model)
    assert chat.reasoning_effort == "none" and plan.reasoning_effort == "low"


@pytest.mark.parametrize("configs", [runtime_model_configs(ENV), {}])
def test_persisted_removed_choice_fails_closed_for_product_calls(configs):
    uc = UsageControl(NullMeter(), PriceTable({}), tiers=Picks("openai/removed"),
                      model_configs=configs, default_model=PICKS[0][0])
    with pytest.raises(ValueError, match="所选模型"):
        uc.config_for(Account(user_id="u", group="assistant"), runtime_gateway_config(ENV), "assistant.turn")


def _deep(pick, reasoning=None):
    configs, writers = runtime_model_configs(ENV), runtime_writer_configs(ENV)
    providers = dict.fromkeys(("anthropic", "deepseek", "openai"), object())
    return DeepLLM(config=runtime_deep_config(ENV), providers=providers, log=MemoryCallLog(),
                   prices=PriceTable({}), meter=NullMeter(), fallback=writers[PICKS[0][0]],
                   tiers=configs[PICKS[1][0]], pick=pick, writers=writers, model_tiers=configs,
                   reasoning=reasoning)


@pytest.mark.parametrize("picked,provider,model", PICKS)
def test_writer_and_all_step_tiers_follow_exact_pick_without_cross_vendor_fallback(picked, provider, model):
    llm = _deep(lambda uid: picked, lambda uid: "high")
    roster, _ = llm.roster(None, "u")
    assert set(roster) == {"deep", "fast", "good"}
    for gateways in roster.values():
        assert len(gateways) == 1
        ref = gateways[0].config.generator
        assert (ref.provider, ref.model, ref.reasoning_effort) == (provider, model, "high")
    assert llm.model_for("u") == f"{provider}:{model}"


def test_writer_without_explicit_pick_retains_server_fallback():
    llm = _deep(lambda uid: None)
    gateways, _ = llm.gateways(None, "u")
    assert [g.config.generator.provider for g in gateways] == ["anthropic", "deepseek"]
    assert gateways[0].config == llm.config


def test_removed_writer_choice_cannot_silently_use_default_writer_or_tier():
    llm = _deep(lambda uid: "openai/removed")
    with pytest.raises(ValueError, match="所选模型"):
        llm.gateways(None, "u")
    from evowiki.llm.budget import JobMeter
    with pytest.raises(ValueError, match="所选模型"):
        llm.tier_gateway("good", JobMeter(NullMeter(), None), "u")
