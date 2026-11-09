"""Gateway.route_provider: where a call would go, without making it."""

from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import PriceTable
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl

CLAUDE = GatewayConfig(generator=ModelRef(provider="anthropic", model="c"),
                       verifier=ModelRef(provider="deepseek", model="d"))
DEEPSEEK = GatewayConfig(generator=ModelRef(provider="deepseek", model="d"),
                         verifier=ModelRef(provider="deepseek", model="d"),
                         allow_single_provider=True)
USER = Account(user_id="u1", group="assistant")


class Tiers:
    def __init__(self, chosen):
        self.chosen = chosen

    def chosen_tier(self, account):
        return self.chosen


def test_without_usage_control_the_config_decides(tmp_path):
    gw = Gateway(CLAUDE, CallLog(tmp_path / "c.jsonl"))
    assert gw.route_provider("generator") == "anthropic"
    assert gw.route_provider("verifier") == "deepseek"
    assert gw.route_provider("generator", USER, "assistant.turn") == "anthropic"


def test_with_tiers_the_account_s_tier_decides(tmp_path):
    for chosen, want in (("good", "anthropic"), ("fast", "deepseek")):
        usage = UsageControl(meter=None, prices=PriceTable({}),   # type: ignore[arg-type]
                             tier_configs={"fast": DEEPSEEK, "good": CLAUDE},
                             tiers=Tiers(chosen))
        gw = Gateway(DEEPSEEK, CallLog(tmp_path / "c.jsonl"), usage=usage)
        assert gw.route_provider("generator", USER, "assistant.turn") == want
        assert gw.route_provider("generator") == "deepseek"      # no account: no tier


def test_route_ref_and_route_provider_agree_with_the_call_route(tmp_path):
    """`route_ref` (the provider and model a call would use) agrees with `route_provider` and with
    the route a real call takes (`_route`), unmetered and metered, for several accounts,
    purposes and tier pins."""
    accounts = (USER, Account(user_id="u1", group="rsi"),
                Account(user_id="u1", group="rsi", tier_pin="good"),
                Account(user_id="u1", group="assistant", tier_pin="fast"),
                Account(user_id="u2", group="page", tier_pin="good"))
    purposes = (None, "assistant.turn", "deep.write", "harness.judge")
    gateways = [Gateway(CLAUDE, CallLog(tmp_path / "c.jsonl"))]
    for chosen in ("good", "fast"):
        usage = UsageControl(meter=None, prices=PriceTable({}),   # type: ignore[arg-type]
                             tier_configs={"fast": DEEPSEEK, "good": CLAUDE},
                             tiers=Tiers(chosen))
        gateways.append(Gateway(DEEPSEEK, CallLog(tmp_path / "c.jsonl"), usage=usage))
    seen = set()
    for gw in gateways:
        metered = gw._usage is not None
        for role in ("generator", "verifier"):
            for purpose in purposes:
                for account in (None, *accounts):
                    ref = gw.route_ref(role, account, purpose)
                    assert ref.provider == gw.route_provider(role, account, purpose)
                    if metered or account is None:      # an unmetered call takes no account
                        assert gw._route(role, account, purpose)[1] == ref
                    seen.add(ref.provider)
    assert seen == {"anthropic", "deepseek"}
