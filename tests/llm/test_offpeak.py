"""DeepSeek's off-peak window (spec 2026-10-21-offpeak-writes §1) and off-peak pricing: fixed
times, no clock read, no model call."""

from datetime import UTC, date, datetime, timedelta

import pytest

from evowiki.llm.gateway import Gateway
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import GatewayConfig, ModelRef, ProviderResponse
from evowiki.llm.offpeak import BEIJING, is_off_peak, next_peak, off_peak_through
from evowiki.llm.prices import Price, PriceTable, load_prices
from evowiki.llm.usage_control import UsageControl
from tests.llm.test_gateway_usage import PAGE, Answer, SpyMeter, _req


def bj(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=BEIJING)


# 2026-10-19 is a Monday, 10-24 a Saturday
@pytest.mark.parametrize(("at", "off"), [
    (bj(2026, 10, 19, 8, 59), True),
    (bj(2026, 10, 19, 9, 0), False),
    (bj(2026, 10, 19, 11, 59), False),
    (bj(2026, 10, 19, 12, 0), True),      # lunch
    (bj(2026, 10, 19, 13, 59), True),
    (bj(2026, 10, 19, 14, 0), False),
    (bj(2026, 10, 23, 17, 59), False),    # Friday
    (bj(2026, 10, 19, 18, 0), True),
    (bj(2026, 10, 24, 10, 0), True),      # Saturday
    (bj(2026, 10, 25, 15, 0), True),      # Sunday
    (datetime(2026, 10, 19, 1, 30, tzinfo=UTC), False),   # 09:30 Beijing
    (datetime(2026, 10, 19, 4, 30, tzinfo=UTC), True),    # 12:30 Beijing
])
def test_peak_is_weekdays_9_to_12_and_14_to_18_beijing(at, off):
    assert is_off_peak(at) is off


def test_a_listed_holiday_is_off_peak_all_day():
    assert not is_off_peak(bj(2026, 10, 19, 10))
    assert is_off_peak(bj(2026, 10, 19, 10), holidays={date(2026, 10, 19)})


def test_next_peak():
    assert next_peak(bj(2026, 10, 19, 10)) == bj(2026, 10, 19, 10)          # in a peak hour
    assert next_peak(bj(2026, 10, 19, 12, 30)) == bj(2026, 10, 19, 14)
    assert next_peak(bj(2026, 10, 19, 19)) == bj(2026, 10, 20, 9)
    assert next_peak(bj(2026, 10, 23, 19)) == bj(2026, 10, 26, 9)           # over the weekend
    assert next_peak(bj(2026, 10, 23, 19), holidays={date(2026, 10, 26)}) == bj(2026, 10, 27, 9)
    utc = next_peak(datetime(2026, 10, 19, 0, 0, tzinfo=UTC))
    assert utc == datetime(2026, 10, 19, 1, 0, tzinfo=UTC) and utc.tzinfo is UTC


def test_off_peak_through_needs_the_whole_span():
    assert off_peak_through(bj(2026, 10, 19, 8, 30), bj(2026, 10, 19, 9, 0))   # ends as peak starts
    assert not off_peak_through(bj(2026, 10, 19, 8, 50), bj(2026, 10, 19, 9, 1))
    assert not off_peak_through(bj(2026, 10, 19, 9, 30), bj(2026, 10, 19, 9, 31))
    assert off_peak_through(bj(2026, 10, 23, 19), bj(2026, 10, 25, 23))       # a weekend
    with pytest.raises(ValueError):
        off_peak_through(bj(2026, 10, 19, 9), bj(2026, 10, 19, 8))
    with pytest.raises(ValueError, match="naive"):
        is_off_peak(datetime(2026, 10, 19, 9))  # noqa: DTZ001  (the naive time is the point)


# ---- pricing ----------------------------------------------------------------------------------

def test_every_deepseek_entry_is_half_price_off_peak_and_claude_is_not():
    t = load_prices()
    for provider in ("deepseek", "deepseek-nothink"):
        for model in ("deepseek-v4-pro", "deepseek-v4-flash"):
            assert t.price(provider, model).off_peak_factor == 0.5
            peak = t.cost(provider, model, 5_000, 1_000, 55_000)
            assert t.cost(provider, model, 5_000, 1_000, 55_000, off_peak=True) == \
                pytest.approx(peak / 2)
    assert t.price("anthropic", "claude-sonnet-5-5").off_peak_factor is None
    assert t.cost("anthropic", "claude-sonnet-5-5", 1000, 100, off_peak=True) == \
        t.cost("anthropic", "claude-sonnet-5-5", 1000, 100)


@pytest.mark.parametrize("value", ["0", "1.5", "-0.5", "true"])
def test_a_bad_off_peak_factor_fails_closed(tmp_path, value):
    p = tmp_path / "prices.toml"
    p.write_text(f'[models."gen:g"]\ninput_per_mtok = 1\noutput_per_mtok = 1\n'
                 f'off_peak_factor = {value}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="off_peak_factor"):
        load_prices(p)


TABLE = PriceTable({"deepseek:ds": Price(1.0, 2.0, off_peak_factor=0.5)})


def test_usage_is_off_peak_only_when_the_whole_call_is():
    u = UsageControl(SpyMeter(), TABLE)
    full = 0.0014                                       # 1000 in + 200 out at the peak rate
    calm, busy = bj(2026, 10, 19, 20), bj(2026, 10, 19, 10)
    assert u.usage_of("deepseek", "ds", None, 1000, 200).cost_usd == pytest.approx(full)
    assert u.usage_of("deepseek", "ds", None, 1000, 200, started_at=calm,
                      ended_at=calm + timedelta(minutes=2)).cost_usd == pytest.approx(full / 2)
    assert u.usage_of("deepseek", "ds", None, 1000, 200, started_at=busy,
                      ended_at=busy).cost_usd == pytest.approx(full)
    straddle = bj(2026, 10, 19, 8, 59)
    assert u.usage_of("deepseek", "ds", None, 1000, 200, started_at=straddle,
                      ended_at=straddle + timedelta(minutes=2)).cost_usd == pytest.approx(full)


class TimedProvider:
    """Answers with 1000 / 200 tokens; the call takes `minutes` on the test's clock."""

    def __init__(self, clock: list, minutes: int):
        self.clock, self.minutes = clock, minutes

    def complete(self, model, request, json_schema):
        self.clock[0] += timedelta(minutes=self.minutes)
        return ProviderResponse(raw_output='{"value": 1}', model_version=model, cost=None,
                                input_tokens=1000, output_tokens=200)


@pytest.mark.parametrize(("start", "minutes", "want"), [
    (bj(2026, 10, 24, 10), 3, 0.0007),          # Saturday: off-peak
    (bj(2026, 10, 19, 10), 3, 0.0014),          # Monday 10:00: peak
    (bj(2026, 10, 19, 8, 58), 3, 0.0014),       # runs into 09:00: peak
])
def test_the_gateway_bills_a_call_by_when_it_ran(tmp_path, start, minutes, want):
    clock = [start]
    meter = SpyMeter()
    cfg = GatewayConfig(generator=ModelRef(provider="deepseek", model="ds"),
                        verifier=ModelRef(provider="deepseek", model="ds"),
                        allow_single_provider=True)
    gw = Gateway(cfg, CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"deepseek": TimedProvider(clock, minutes)},
                 attempts=AttemptLog(tmp_path / "a.jsonl"), clock=lambda: clock[0],
                 usage=UsageControl(meter, TABLE, clock=lambda: clock[0]))
    gw.call("generator", _req(), Answer, account=PAGE)
    [(_acct, _purpose, usage, _replayed)] = meter.records()
    assert usage.cost_usd == pytest.approx(want)


def test_prc_public_holidays_are_off_peak_by_default():
    assert is_off_peak(bj(2026, 10, 6, 10))  # Tue, 调休 rest day
    assert not is_off_peak(bj(2026, 10, 6, 10), holidays=())  # explicit: none known
    assert not is_off_peak(bj(2026, 10, 8, 10))  # first working day after
    assert next_peak(bj(2026, 10, 2, 19)) == bj(2026, 10, 8, 9)
