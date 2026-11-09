"""Gateway.call(account=...) metering (plan rev 3.2 §9.4)."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from evowiki.llm.gateway import Gateway, LLMParseError, MissingRecordingError
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef, ProviderResponse
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.providers import ProviderEmptyOutputError
from evowiki.llm.scripted import ScriptedProvider
from evowiki.llm.usage import Account, BudgetExceededError, Usage
from evowiki.llm.usage_control import UsageControl

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
GOOD = GatewayConfig(generator=ModelRef(provider="gen", model="g-1"),
                     verifier=ModelRef(provider="ver", model="v-1"))
FAST = GatewayConfig(generator=ModelRef(provider="gen", model="g-fast"),
                     verifier=ModelRef(provider="ver", model="v-fast"))
PRICES = PriceTable({"gen:g-1": Price(1.0, 2.0, 0.1, 1.25), "gen:g-fast": Price(0.1, 0.2)})
PAGE = Account(user_id="u1", group="page")
CURATOR = Account(user_id="u1", group="curator")


class Answer(BaseModel):
    value: int


def _req(**over) -> LLMRequest:
    base = {"purpose": "unit", "prompt_version": "unit.v1", "subject": "x", "system": "sys",
            "prompt": "p"}
    return LLMRequest(**(base | over))


class SpyMeter:
    def __init__(self, over_limit: bool = False):
        self.events: list[tuple] = []
        self.over_limit = over_limit

    def check(self, account):
        self.events.append(("check", account))
        if self.over_limit:
            raise BudgetExceededError(account, "day", 1.0)

    def record(self, account, purpose, usage, *, replayed):
        self.events.append(("record", account, purpose, usage, replayed))

    def records(self):
        return [e[1:] for e in self.events if e[0] == "record"]


class TokenProvider:
    """Plays a script of outputs (str) or exceptions; every response reports 1000/200 tokens."""

    def __init__(self, script):
        self.script = list(script)
        self.models: list[str] = []

    def complete(self, model, request, json_schema):
        self.models.append(model)
        out = self.script.pop(0)
        if isinstance(out, Exception):
            raise out
        return ProviderResponse(raw_output=out, model_version=model, cost=None,
                                input_tokens=1000, output_tokens=200)


def _gw(tmp_path, provider, meter, *, tiers=None, mode="record", log=None):
    return Gateway(GOOD, log or CallLog(tmp_path / "calls.jsonl"), mode=mode,
                   providers={"gen": provider, "ver": TokenProvider([])},
                   clock=lambda: T0, attempts=AttemptLog(tmp_path / "att.jsonl"),
                   usage=UsageControl(meter, PRICES, tier_configs=tiers))


def test_no_account_is_byte_identical_and_never_meters(tmp_path, monkeypatch):
    # latency is measured with perf_counter; pin it so the two logs can be compared byte for byte
    monkeypatch.setattr("evowiki.llm.gateway.time", SimpleNamespace(perf_counter=lambda: 0.0))
    meter = SpyMeter()
    plain_dir, metered_dir = tmp_path / "a", tmp_path / "b"
    plain_dir.mkdir()
    metered_dir.mkdir()

    def providers():
        return {"gen": ScriptedProvider("gen", lambda m, r: '{"value": 1}'),
                "ver": ScriptedProvider("ver", lambda m, r: '{"value": 2}')}

    plain = Gateway(GOOD, CallLog(plain_dir / "c.jsonl"), mode="record", providers=providers(),
                    clock=lambda: T0, attempts=AttemptLog(plain_dir / "a.jsonl"))
    metered = Gateway(GOOD, CallLog(metered_dir / "c.jsonl"), mode="record",
                      providers=providers(), clock=lambda: T0,
                      attempts=AttemptLog(metered_dir / "a.jsonl"),
                      usage=UsageControl(meter, PRICES, tier_configs={"fast": FAST,
                                                                      "good": GOOD}))
    outs = []
    for gw in (plain, metered):
        outs.append([gw.call("generator", _req(), Answer), gw.call("generator", _req(), Answer),
                     gw.call("verifier", _req(subject="y"), Answer)])
    assert outs[0] == outs[1]
    assert (plain_dir / "c.jsonl").read_bytes() == (metered_dir / "c.jsonl").read_bytes()
    assert meter.events == []


def test_account_without_usage_control_fails_closed(tmp_path):
    p = TokenProvider(['{"value": 1}'])
    gw = Gateway(GOOD, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"gen": p})
    with pytest.raises(ValueError, match="usage"):
        gw.call("generator", _req(), Answer, account=PAGE)
    assert p.models == []


def test_paid_call_is_checked_then_billed(tmp_path):
    meter, p = SpyMeter(), TokenProvider(['{"value": 1}'])
    out, _ = _gw(tmp_path, p, meter).call("generator", _req(), Answer, account=PAGE)
    assert out == Answer(value=1)
    assert meter.events[0] == ("check", PAGE)
    [(acct, purpose, usage, replayed)] = meter.records()
    assert (acct, purpose, replayed) == (PAGE, "unit", False)
    # pydantic Usage rejects pytest.approx as a field value: compare cost separately
    assert usage.model_dump(exclude={"cost_usd", "call_id"}) == {
        "provider": "gen", "model": "g-1", "input_tokens": 1000, "output_tokens": 200,
        "cache_read_tokens": 0, "cache_write_tokens": 0}
    assert usage.cost_usd == pytest.approx(0.0014)


def test_over_budget_raises_before_any_provider_call(tmp_path):
    meter, p = SpyMeter(over_limit=True), TokenProvider(['{"value": 1}'])
    gw = _gw(tmp_path, p, meter)
    with pytest.raises(BudgetExceededError):
        gw.call("generator", _req(), Answer, account=PAGE)
    assert p.models == [] and meter.records() == []
    assert not (tmp_path / "calls.jsonl").exists() or (tmp_path / "calls.jsonl").read_text() == ""


def test_cache_hit_is_recorded_as_replayed_and_not_checked(tmp_path):
    meter, p = SpyMeter(), TokenProvider(['{"value": 1}'])
    gw = _gw(tmp_path, p, meter)
    gw.call("generator", _req(), Answer, account=PAGE)
    meter.events.clear()
    gw.call("generator", _req(), Answer, account=PAGE)
    [(_, acct, purpose, usage, replayed)] = meter.events
    assert (acct, purpose, replayed) == (PAGE, "unit", True)
    assert usage.model_copy(update={"call_id": None}) == Usage(
        provider="gen", model="g-1", input_tokens=0, output_tokens=0, cost_usd=None)
    assert usage.call_id is not None and "_hit" in usage.call_id    # the logical call
    assert len(p.models) == 1


def test_replay_mode_hit_is_replayed_and_miss_is_not_metered(tmp_path):
    log = CallLog(tmp_path / "calls.jsonl")
    _gw(tmp_path, TokenProvider(['{"value": 1}']), SpyMeter(), log=log).call(
        "generator", _req(), Answer, account=PAGE)
    meter = SpyMeter()
    gw = _gw(tmp_path, TokenProvider([]), meter, mode="replay", log=log)
    gw.call("generator", _req(), Answer, account=PAGE)
    assert [r[-1] for r in meter.records()] == [True]
    meter.events.clear()
    with pytest.raises(MissingRecordingError):
        gw.call("generator", _req(subject="new"), Answer, account=PAGE)
    assert meter.events == []


def test_every_paid_retry_is_billed(tmp_path):
    meter = SpyMeter()
    p = TokenProvider(["not json",
                       ProviderEmptyOutputError("cut", status="truncated", raw_output='{"v',
                                                model_version="g-1", input_tokens=10,
                                                output_tokens=5),
                       '{"value": 3}'])
    out, _ = _gw(tmp_path, p, meter).call("generator", _req(), Answer, account=PAGE)
    assert out == Answer(value=3)
    recs = meter.records()
    assert [(r[2].input_tokens, r[2].output_tokens, r[3]) for r in recs] == [
        (1000, 200, False), (10, 5, False), (1000, 200, False)]
    assert [e[0] for e in meter.events].count("check") == 1  # a started call finishes


def test_failed_call_still_records_what_was_paid(tmp_path):
    meter, p = SpyMeter(), TokenProvider(["x", "x", "x"])
    with pytest.raises(LLMParseError):
        _gw(tmp_path, p, meter).call("generator", _req(), Answer, account=PAGE)
    assert len(meter.records()) == 3


def test_unreachable_provider_is_not_billed(tmp_path):
    meter, p = SpyMeter(), TokenProvider([ConnectionError("down")])
    with pytest.raises(ConnectionError):
        _gw(tmp_path, p, meter).call("generator", _req(), Answer, account=PAGE)
    assert meter.records() == []


def test_tier_picks_the_model(tmp_path):
    meter, p = SpyMeter(), TokenProvider(['{"value": 1}', '{"value": 2}'])
    gw = _gw(tmp_path, p, meter, tiers={"fast": FAST, "good": GOOD})
    _, fast_call = gw.call("generator", _req(), Answer, account=CURATOR)
    _, good_call = gw.call("generator", _req(), Answer, account=PAGE)
    assert p.models == ["g-fast", "g-1"]
    assert (fast_call.model, good_call.model) == ("g-fast", "g-1")
    assert [r[2].cost_usd for r in meter.records()] == [pytest.approx(0.00014),
                                                         pytest.approx(0.0014)]


def test_tier_follows_the_request_purpose(tmp_path):
    meter, p = SpyMeter(), TokenProvider(['{"value": 1}', '{"value": 2}'])
    gw = _gw(tmp_path, p, meter, tiers={"fast": FAST, "good": GOOD})
    gw.call("generator", _req(purpose="graph.group_naming"), Answer, account=CURATOR)   # purpose: good
    gw.call("generator", _req(purpose="sources.toc"), Answer, account=PAGE)     # purpose: fast
    assert p.models == ["g-1", "g-fast"]


class CacheProvider:
    """Every response reports 1000 / 200 tokens plus 30000 cache read / 2000 cache write."""

    def __init__(self, script):
        self.script = list(script)

    def complete(self, model, request, json_schema):
        out = self.script.pop(0)
        if isinstance(out, Exception):
            raise out
        return ProviderResponse(raw_output=out, model_version=model, cost=None,
                                input_tokens=1000, output_tokens=200,
                                cache_read_tokens=30_000, cache_write_tokens=2000)


CACHED_COST = (1000 * 1.0 + 200 * 2.0 + 30_000 * 0.1 + 2000 * 1.25) / 1e6   # 0.0069


def test_cache_tokens_reach_the_meter_and_are_priced(tmp_path):
    meter = SpyMeter()
    _gw(tmp_path, CacheProvider(['{"value": 1}']), meter).call("generator", _req(), Answer,
                                                               account=PAGE)
    [(_, _, usage, replayed)] = meter.records()
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens,
            replayed) == (1000, 30_000, 2000, False)
    assert usage.cost_usd == pytest.approx(CACHED_COST)


def test_replayed_call_records_zero_cache_tokens(tmp_path):
    meter = SpyMeter()
    gw = _gw(tmp_path, CacheProvider(['{"value": 1}']), meter)
    gw.call("generator", _req(), Answer, account=PAGE)
    meter.events.clear()
    gw.call("generator", _req(), Answer, account=PAGE)
    [(_, _, usage, replayed)] = meter.records()
    assert replayed and usage.model_copy(update={"call_id": None}) == Usage(
        provider="gen", model="g-1", input_tokens=0, output_tokens=0)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (0, 0)


def test_a_paid_failure_carries_its_cache_tokens(tmp_path):
    meter = SpyMeter()
    cut = ProviderEmptyOutputError("cut", status="truncated", raw_output='{"v',
                                   model_version="g-1", input_tokens=10, output_tokens=5,
                                   cache_read_tokens=30_000, cache_write_tokens=0)
    out, _ = _gw(tmp_path, CacheProvider([cut, '{"value": 2}']), meter).call(
        "generator", _req(), Answer, account=PAGE)
    assert out == Answer(value=2)
    first, second = (r[2] for r in meter.records())
    assert (first.input_tokens, first.cache_read_tokens, first.cache_write_tokens) == (
        10, 30_000, 0)
    assert first.cost_usd == pytest.approx((10 * 1.0 + 5 * 2.0 + 30_000 * 0.1) / 1e6)
    assert second.cost_usd == pytest.approx(CACHED_COST)


def test_a_paid_failure_without_cache_counts_records_zero(tmp_path):
    meter = SpyMeter()
    cut = ProviderEmptyOutputError("cut", status="refusal", input_tokens=10, output_tokens=5)
    with pytest.raises(ProviderEmptyOutputError):
        _gw(tmp_path, CacheProvider([cut]), meter).call("generator", _req(), Answer,
                                                        account=PAGE)
    [(_, _, usage, _)] = meter.records()
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (0, 0)
    assert usage.cost_usd == pytest.approx(20e-6)


def test_llm_call_and_attempt_carry_the_priced_cost(tmp_path):
    meter = SpyMeter()
    gw = _gw(tmp_path, CacheProvider(['{"value": 1}']), meter)
    _, call = gw.call("generator", _req(), Answer, account=PAGE)
    [(_, _, usage, _)] = meter.records()
    assert call.cost == pytest.approx(CACHED_COST) == pytest.approx(usage.cost_usd)
    assert [c.cost for c in CallLog(tmp_path / "calls.jsonl").calls()] == [pytest.approx(CACHED_COST)]
    [att] = AttemptLog(tmp_path / "att.jsonl").attempts()
    assert att.cost == pytest.approx(CACHED_COST)


def test_unpriced_model_keeps_cost_none(tmp_path):
    meter = SpyMeter()
    cfg = GatewayConfig(generator=ModelRef(provider="gen", model="unknown"),
                        verifier=ModelRef(provider="ver", model="v-1"))
    gw = Gateway(cfg, CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"gen": TokenProvider(['{"value": 1}']), "ver": TokenProvider([])},
                 clock=lambda: T0, usage=UsageControl(meter, PRICES))
    _, call = gw.call("generator", _req(), Answer, account=PAGE)
    assert call.cost is None


def test_paid_failed_attempt_carries_the_priced_cost(tmp_path):
    meter = SpyMeter()
    gw = _gw(tmp_path, TokenProvider(["not json", '{"value": 1}']), meter)
    gw.call("generator", _req(), Answer, account=PAGE)
    costs = [a.cost for a in AttemptLog(tmp_path / "att.jsonl").attempts()]
    assert costs == [pytest.approx(0.0014)] * 2
