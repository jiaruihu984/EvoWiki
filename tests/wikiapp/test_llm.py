import pytest
from pydantic import BaseModel

from evowiki.belief.aspects import AspectConfig
from evowiki.belief.theta import ThetaInfer
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.prices import PriceTable
from evowiki.llm.scripted import ScriptedProvider
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl
from evowiki.wikiapp.llm import (
    ASPECT_CONFIG_V1,
    JSON_PROMPT_VERSION,
    CountingGateway,
    GatewayJsonLLM,
    LLMUnavailable,
    purpose_group,
    require,
)
from tests.runtime.helpers import T0
from tests.runtime.llm_helpers import MemoryCallLog
from tests.wikiapp.script import Script


class Out(BaseModel):
    x: int


def test_verifier_goes_to_deepseek_and_generator_to_anthropic():
    s = Script().push("goals.enter", {"x": 1}).push("other.check", {"x": 2})
    llm = GatewayJsonLLM(s.gateway(), subject="u1")
    assert llm("generator", "goals.enter", "p1", Out) == Out(x=1)
    assert llm("verifier", "other.check", "p2", Out) == Out(x=2)
    assert s.roles("goals.enter") == ["anthropic"]
    assert s.roles("other.check") == ["deepseek"]
    req = s.requests("goals.enter")[0]
    assert req.prompt_version == JSON_PROMPT_VERSION["goals.enter"] == "enter@v5"
    assert req.subject == "u1" and req.system == "只回复 JSON。" and req.prompt == "p1"
    assert "goals.diagnostic.verify" not in JSON_PROMPT_VERSION      # no verification call
    assert "goals.diagnostic.generate" not in JSON_PROMPT_VERSION    # no item generator


def test_unknown_purpose_uses_v1_prompt_version():
    s = Script().push("something.else", {"x": 3})
    assert GatewayJsonLLM(s.gateway(), subject="u")("generator", "something.else", "p",
                                                     Out) == Out(x=3)
    assert s.requests("something.else")[0].prompt_version == "v1"


def test_counting_gateway_counts_logical_calls_including_log_hits():
    s = Script().push("sources.toc", {"x": 1})
    cg = CountingGateway(s.gateway())
    llm = GatewayJsonLLM(cg, subject="u1")
    assert cg.calls == 0
    llm("generator", "sources.toc", "same", Out)
    llm("generator", "sources.toc", "same", Out)          # answered from the call log
    assert cg.calls == 2 and s.calls("sources.toc") == 1
    req = LLMRequest(purpose="sources.toc", prompt_version=JSON_PROMPT_VERSION["sources.toc"], subject="u1",
                     system="只回复 JSON。", prompt="same")
    out, _ = cg.call("generator", req, Out)
    assert out == Out(x=1) and cg.calls == 3


def test_unscripted_call_fails_loudly():
    llm = GatewayJsonLLM(Script().gateway(), subject="u1")
    with pytest.raises(AssertionError, match="unscripted"):
        llm("generator", "wiki.feedback_understand", "p", Out)


def test_require():
    with pytest.raises(LLMUnavailable):
        require(None)
    assert issubclass(LLMUnavailable, RuntimeError)
    gw = Script().gateway()
    cg = require(gw)
    assert isinstance(cg, CountingGateway) and cg.inner is gw and cg.calls == 0
    assert require(cg) is cg                             # never double-wrapped


def test_aspect_config_v1_is_the_default_theta():
    assert ASPECT_CONFIG_V1 == AspectConfig.from_theta(ThetaInfer.default())


GOOD = GatewayConfig(generator=ModelRef(provider="good", model="g"),
                     verifier=ModelRef(provider="good", model="g"), allow_single_provider=True)
FAST = GatewayConfig(generator=ModelRef(provider="fast", model="f"),
                     verifier=ModelRef(provider="fast", model="f"), allow_single_provider=True)


class SpyMeter:
    def __init__(self):
        self.accounts: list[tuple[Account, str]] = []

    def check(self, account):
        return None

    def record(self, account, purpose, usage, *, replayed):
        self.accounts.append((account, purpose))


class Choice:
    def __init__(self, chosen):
        self.chosen = chosen

    def chosen_tier(self, account):
        return self.chosen.get(account.group)


def _tiered(meter, tiers=None):
    served: list[tuple[str, str]] = []

    def answer(name):
        def respond(model, request):
            served.append((request.purpose, name))
            return '{"x": 1}'
        return respond

    kw = {} if tiers is None else {"tiers": tiers}
    gw = Gateway(GOOD, MemoryCallLog(), mode="record", clock=lambda: T0,
                 providers={n: ScriptedProvider(n, answer(n)) for n in ("good", "fast")},
                 usage=UsageControl(meter, PriceTable({}),
                                    tier_configs={"fast": FAST, "good": GOOD}, **kw))
    return gw, served


ALL_PURPOSES = {"wiki.feedback_understand": ("page", "good"),
                "graph.group_naming": ("curator", "good"),
                "goals.intake": ("diagnostic", "good"),     # planning always thinks
                "goals.enter": ("diagnostic", "good")}     # planning always thinks


def test_purpose_groups():
    assert {p: purpose_group(p) for p in ALL_PURPOSES} == {
        p: g for p, (g, _) in ALL_PURPOSES.items()}
    assert purpose_group("goals.path") == "page"
    assert purpose_group("something.else") == "other"


def test_wiki_purposes_land_on_their_tier_with_the_users_account():
    meter = SpyMeter()
    gw, served = _tiered(meter)
    cg = CountingGateway(gw, user_id="u1")
    for i, purpose in enumerate(ALL_PURPOSES):
        GatewayJsonLLM(cg, subject="s")("generator", purpose, f"p{i}", Out)
    assert served == [(p, t) for p, (_, t) in ALL_PURPOSES.items()]
    assert meter.accounts == [(Account(user_id="u1", group=g), p)
                              for p, (g, _) in ALL_PURPOSES.items()]
    assert cg.calls == len(ALL_PURPOSES)


def test_users_group_tier_overrides_the_purpose_default():
    gw, served = _tiered(SpyMeter(), Choice({"page": "fast", "diagnostic": "good"}))
    llm = GatewayJsonLLM(CountingGateway(gw, user_id="u1"), subject="s")
    for purpose in ("wiki.feedback_understand", "goals.path", "goals.intake"):
        llm("generator", purpose, "p", Out)
    # the group choice moves the feedback reading (page); planning / splitting thinks whatever
    # the choice (规划和拆分必须用思考模型)
    assert served == [("wiki.feedback_understand", "fast"), ("goals.path", "good"),
                      ("goals.intake", "good")]


def test_without_usage_control_no_account_is_passed():
    s = Script().push("sources.toc", {"x": 1})
    cg = CountingGateway(s.gateway(), user_id="u1")    # would raise if an account were passed
    assert GatewayJsonLLM(cg, subject="u1")("generator", "sources.toc", "p", Out) == Out(x=1)
    assert s.roles("sources.toc") == ["anthropic"]


def test_require_passes_the_user():
    gw, served = _tiered(SpyMeter())
    cg = require(gw, "u9")
    assert cg.user_id == "u9"
    GatewayJsonLLM(cg, subject="s")("generator", "sources.toc", "p", Out)
    assert served == [("sources.toc", "fast")]


def test_metered_gateway_without_a_user_fails_closed():
    gw, served = _tiered(SpyMeter())
    with pytest.raises(ValueError, match="user"):
        GatewayJsonLLM(CountingGateway(gw), subject="s")("generator", "sources.toc", "p", Out)
    assert served == []
