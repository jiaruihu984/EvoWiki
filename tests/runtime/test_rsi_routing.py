from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from evowiki.forge.binding import ForgeConverse
from evowiki.llm.chat import ChatRequest, ChatResponse, ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef, ProviderResponse
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl
from evowiki.rules import guard
from evowiki.runtime import llm as runtime_llm
from evowiki.runtime.usage import DEFAULT_IMPROVER_MODEL, runtime_improver_ref
from evowiki.teach.contracts import TeachingSpec


class Answer(BaseModel):
    value: int


class Provider:
    def chat(self, model, request):
        return ChatResponse(turn=ChatTurn(text="done"), model_version=model,
                            input_tokens=100, output_tokens=10)

    def complete(self, model, request, json_schema):
        return ProviderResponse(raw_output='{"verdict":"no_conflict","value":1}',
                                model_version=model, input_tokens=100, output_tokens=10)


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setenv("EVOWIKI_IMPROVER_MODEL", "opus-test")
    ref = ModelRef(provider="anthropic", model="haiku-test")
    cfg = GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True)
    meter = SimpleNamespace(check=lambda a: None, record=lambda *a, **kw: None)
    usage = UsageControl(meter, PriceTable({"anthropic-think:opus-test": Price(1, 2)}),
                         tier_configs={"fast": cfg, "good": cfg},
                         model_configs={"product": {"fast": cfg, "good": cfg}},
                         default_model="product")
    return Gateway(cfg, CallLog(tmp_path / "calls.jsonl"), mode="record", usage=usage,
                   providers={"anthropic": Provider(), "anthropic-think": Provider()})


@pytest.mark.parametrize("purpose", ["forge.tool", "forge.patch"])
def test_rsi_forge_uses_improver_even_with_product_tier_and_picker(gateway, purpose):
    req = ChatRequest(purpose=purpose, prompt_version="test", subject="candidate",
                      system="", messages=[{"role": "user", "text": "build"}])
    _, call = ForgeConverse(gateway, Account(user_id="u", group="rsi", tier_pin="good"))(req)
    assert (call.provider, call.model) == ("anthropic-think", "opus-test")
    assert call.cost is not None and call.cost > 0
    # The product gateway itself still routes product calls to the user's model.
    assert gateway.route_ref("generator", Account(user_id="u", group="assistant")).model == "haiku-test"


def test_harness_rule_guard_uses_improver_and_records_its_model(gateway, monkeypatch):
    state = SimpleNamespace(rule_decisions=(), rejected_guidelines=())
    repo = SimpleNamespace(load=lambda uid: SimpleNamespace(state=state))
    monkeypatch.setattr("evowiki.wikiapp.teachspec.effective_spec", lambda s: TeachingSpec(version=1, items=()))
    monkeypatch.setattr(guard, "harness_candidates", lambda d: [guard.Candidate("新的规则")])
    seen = []

    def check(spec, decisions, candidates, *, judge, **kw):
        if candidates:
            seen.append(judge.conflict("新的规则", "any", None, ()).model)
        return guard.PASS

    monkeypatch.setattr(guard, "check_many", check)
    assert guard.harness_guard(repo, gateway)("u", SimpleNamespace(spec_ops=())).passed
    assert seen == ["anthropic-think/opus-test"]


def test_pinned_gateway_keeps_logging_and_does_not_change_judge(gateway):
    ref = ModelRef(provider="anthropic-think", model="opus-test")
    pinned = gateway.with_model(ref)
    req = LLMRequest(purpose="teach.rule_conflict", prompt_version="test", subject="u", system="", prompt="x")
    _, call = pinned.call("generator", req, Answer, account=Account(user_id="u", group="other"))
    assert call.model == "opus-test" and call.user_id == "u"
    assert gateway._log.get(call.input_hash).id == call.id
    from evowiki.harness.judge import judge_gateway

    judge_ref = ModelRef(provider="deepseek", model="judge-test")
    llm = SimpleNamespace(log=gateway._log, meter=gateway._usage.meter,
                          prices=gateway._usage.prices, clock=None, attempts=None, requests=None)
    judge = judge_gateway(llm, judge_ref, providers={"deepseek": Provider()})
    _, judged = judge.call("generator", req.model_copy(update={"purpose": "rsi.h.judge"}), Answer,
                          account=Account(user_id="u", group="rsi"))
    assert (judged.provider, judged.model) == ("deepseek", "judge-test")


def test_improver_default_override_and_missing_key():
    assert runtime_improver_ref({"ANTHROPIC_API_KEY": "fake"}).model == DEFAULT_IMPROVER_MODEL
    assert runtime_improver_ref({"ANTHROPIC_API_KEY": "fake", "EVOWIKI_IMPROVER_MODEL": "custom"}).model == "custom"
    assert runtime_improver_ref({}) is None


def test_product_forge_keeps_its_model(gateway):
    req = ChatRequest(purpose="forge.tool", prompt_version="test", subject="product",
                      system="", messages=[{"role": "user", "text": "build"}])
    _, call = ForgeConverse(gateway, Account(user_id="u", group="page"))(req)
    assert (call.provider, call.model) == ("anthropic", "haiku-test")


def test_deep_tier_has_thinking_provider_for_rsi_coder(monkeypatch):
    monkeypatch.setattr(runtime_llm, "_anthropic_client", lambda env: object())
    monkeypatch.setattr(runtime_llm, "AnthropicProvider", lambda **kw: SimpleNamespace(**kw))
    providers = runtime_llm.runtime_deep_providers({"ANTHROPIC_API_KEY": "fake"})
    assert providers["anthropic-think"].effort == "high"
    assert providers["anthropic"].effort == runtime_llm.DEEP_EFFORT
