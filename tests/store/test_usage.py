from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from evowiki.llm.usage import Account, BudgetExceededError, Usage
from evowiki.llm.usage_control import DEFAULT_TIERS, GROUPS
from evowiki.store.usage import (
    PgUsageMeter,
    day_start,
    get_settings,
    month_start,
    spend_by_group,
    total_cost,
    update_settings,
    usage_tz,
)
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
PAGE = Account(user_id="u1", group="page")
CURATOR = Account(user_id="u1", group="curator")


def U(cost, inp=1000, out=200, model="g-1"):
    return Usage(provider="gen", model=model, input_tokens=inp, output_tokens=out,
                 cost_usd=cost)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def meter(pool, clock):
    make_user(pool, "u1")
    make_user(pool, "u2")
    return PgUsageMeter(pool, clock=clock, tz=UTC)


def test_record_and_spend_by_group(pool, meter):
    meter.record(PAGE, "page.write", U(0.0014), replayed=False)
    meter.record(PAGE, "page.write", U(0.0014), replayed=False)
    meter.record(PAGE, "page.write", U(None, 0, 0), replayed=True)
    meter.record(PAGE, "page.verify", U(None, 50, 60, model="unpriced"), replayed=False)
    meter.record(CURATOR, "curate", U(0.0001, 10, 20), replayed=False)
    with pool.connection() as conn:
        s = spend_by_group(conn, "u1", day_start(NOW, UTC))
        assert set(s) == set(GROUPS)
        page = s["page"]
        assert page.cost_usd == pytest.approx(0.0028)
        assert (page.input_tokens, page.output_tokens) == (2050, 460)
        assert (page.calls, page.replayed_calls, page.unpriced_calls) == (3, 1, 1)
        assert s["curator"].calls == 1 and s["assistant"].calls == 0
        assert total_cost(conn, "u1", day_start(NOW, UTC)) == pytest.approx(0.0029)
        assert total_cost(conn, "u2", day_start(NOW, UTC)) == 0.0


def test_cache_tokens_round_trip_and_sum_only_paid_rows(pool, meter):
    cached = Usage(provider="anthropic", model="claude-sonnet-5-5", input_tokens=500,
                   output_tokens=400, cache_read_tokens=30_000, cache_write_tokens=2000,
                   cost_usd=0.016)
    meter.record(PAGE, "page.write", cached, replayed=False)
    meter.record(PAGE, "page.write", cached, replayed=False)
    meter.record(PAGE, "page.write", cached.model_copy(update={"cost_usd": None}), replayed=True)
    meter.record(PAGE, "page.verify", U(0.001), replayed=False)   # no cache tokens: 0 / 0
    with pool.connection() as conn:
        rows = conn.execute("select cache_read_tokens, cache_write_tokens from usage_record "
                            "order by id").fetchall()
        assert [(r["cache_read_tokens"], r["cache_write_tokens"]) for r in rows] == [
            (30_000, 2000), (30_000, 2000), (30_000, 2000), (0, 0)]
        page = spend_by_group(conn, "u1", day_start(NOW, UTC))["page"]
        assert (page.input_tokens, page.cache_read_tokens, page.cache_write_tokens) == (
            2000, 60_000, 4000)
        assert page.calls == 3 and page.replayed_calls == 1
        assert spend_by_group(conn, "u1", day_start(NOW, UTC))["curator"].cache_read_tokens == 0


def test_rows_without_cache_columns_default_to_zero(pool):
    with pool.connection() as conn:
        conn.execute("insert into usage_record (user_id, grp, purpose, provider, model, "
                     "input_tokens, output_tokens, cost_usd, replayed, ts) values "
                     "('u1', 'page', 'p', 'gen', 'g-1', 1, 1, 0.0, false, %s)", (NOW,))
        page = spend_by_group(conn, "u1", day_start(NOW, UTC))["page"]
        assert (page.cache_read_tokens, page.cache_write_tokens) == (0, 0)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into usage_record (user_id, grp, purpose, provider, model, "
                         "input_tokens, output_tokens, cache_read_tokens, cost_usd, replayed, "
                         "ts) values ('u1', 'page', 'p', 'gen', 'g-1', 1, 1, -1, 0.0, false, %s)",
                         (NOW,))


def test_replayed_rows_are_never_billed_even_with_a_cost(pool, meter):
    meter.record(PAGE, "p", U(5.0), replayed=True)
    with pool.connection() as conn:
        assert total_cost(conn, "u1", day_start(NOW, UTC)) == 0.0


def test_check_passes_without_limits(meter):
    meter.record(PAGE, "p", U(100.0), replayed=False)
    meter.check(PAGE)


def test_daily_limit_blocks_once_reached(pool, meter):
    with pool.connection() as conn:
        update_settings(conn, "u1", now=NOW, daily_limit_usd=0.002)
    meter.record(PAGE, "p", U(0.0014), replayed=False)
    meter.check(PAGE)
    meter.record(CURATOR, "c", U(0.0014), replayed=False)  # limits are per user, all groups
    with pytest.raises(BudgetExceededError) as info:
        meter.check(PAGE)
    assert info.value.period == "day" and info.value.limit_usd == 0.002
    assert info.value.account == PAGE


def test_zero_limit_blocks_everything(pool, meter):
    with pool.connection() as conn:
        update_settings(conn, "u1", now=NOW, monthly_limit_usd=0.0)
    with pytest.raises(BudgetExceededError) as info:
        meter.check(PAGE)
    assert info.value.period == "month"


def test_yesterday_counts_for_month_not_day(pool, meter, clock):
    clock.t = NOW - timedelta(days=1)
    meter.record(PAGE, "p", U(1.0), replayed=False)
    clock.t = NOW
    with pool.connection() as conn:
        update_settings(conn, "u1", now=NOW, daily_limit_usd=0.5, monthly_limit_usd=1.5)
    meter.check(PAGE)
    meter.record(PAGE, "p", U(0.6), replayed=False)
    with pytest.raises(BudgetExceededError) as info:
        meter.check(PAGE)
    assert info.value.period == "day"


def test_month_limit(pool, meter, clock):
    clock.t = NOW - timedelta(days=5)
    meter.record(PAGE, "p", U(2.0), replayed=False)
    clock.t = NOW
    with pool.connection() as conn:
        update_settings(conn, "u1", now=NOW, daily_limit_usd=10.0, monthly_limit_usd=2.0)
    with pytest.raises(BudgetExceededError) as info:
        meter.check(PAGE)
    assert info.value.period == "month"


def test_day_boundary_follows_timezone(pool):
    shanghai = ZoneInfo("Asia/Shanghai")
    early = datetime(2026, 10, 7, 17, 0, tzinfo=UTC)  # 2026-10-08 01:00 in Shanghai
    PgUsageMeter(pool, clock=Clock(early), tz=shanghai).record(PAGE, "p", U(1.0),
                                                                replayed=False)
    with pool.connection() as conn:
        assert total_cost(conn, "u1", day_start(NOW, shanghai)) == pytest.approx(1.0)
        assert total_cost(conn, "u1", day_start(NOW, UTC)) == 0.0


def test_period_starts():
    shanghai = ZoneInfo("Asia/Shanghai")
    assert day_start(NOW, UTC) == datetime(2026, 10, 8, tzinfo=UTC)
    assert day_start(NOW, shanghai) == datetime(2026, 10, 8, tzinfo=shanghai)
    assert month_start(NOW, UTC) == datetime(2026, 10, 1, tzinfo=UTC)
    assert month_start(datetime(2026, 9, 30, 20, 0, tzinfo=UTC), shanghai) == datetime(
        2026, 10, 1, tzinfo=shanghai)


def test_usage_tz_from_env():
    assert usage_tz({"EVOWIKI_USAGE_TZ": "Asia/Shanghai"}) == ZoneInfo("Asia/Shanghai")
    assert usage_tz({}) is not None


def test_settings_defaults_and_partial_updates(pool):
    with pool.connection() as conn:
        s = get_settings(conn, "u1")
        assert s.daily_limit_usd is None and s.monthly_limit_usd is None
        assert dict(s.tiers) == dict(DEFAULT_TIERS)
        update_settings(conn, "u1", now=NOW, daily_limit_usd=1.0, monthly_limit_usd=20.0)
        s = update_settings(conn, "u1", now=NOW, tiers={"page": "fast"})
        assert (s.daily_limit_usd, s.monthly_limit_usd) == (1.0, 20.0)
        assert s.tiers["page"] == "fast" and s.tiers["curator"] == "fast"
        s = update_settings(conn, "u1", now=NOW, daily_limit_usd=None, tiers={"curator": "good"})
        assert s.daily_limit_usd is None and s.monthly_limit_usd == 20.0
        assert s.tiers["page"] == "fast" and s.tiers["curator"] == "good"
        assert get_settings(conn, "u1") == s
        assert dict(get_settings(conn, "u2").tiers) == dict(DEFAULT_TIERS)


@pytest.mark.parametrize("kw, match", [
    ({"daily_limit_usd": -1.0}, "limit"),
    ({"monthly_limit_usd": float("inf")}, "limit"),
    ({"daily_limit_usd": True}, "limit"),
    ({"tiers": {"nope": "fast"}}, "group"),
    ({"tiers": {"page": "cheap"}}, "tier"),
])
def test_bad_settings_fail_closed(pool, kw, match):
    with pool.connection() as conn, pytest.raises(ValueError, match=match):
        update_settings(conn, "u1", now=NOW, **kw)


def test_tier_for_reads_user_setting(pool, meter):
    assert meter.tier_for(PAGE) == "good"
    with pool.connection() as conn:
        update_settings(conn, "u1", now=NOW, tiers={"page": "fast"})
    assert meter.tier_for(PAGE) == "fast"
    assert meter.tier_for(Account(user_id="u2", group="page")) == "good"


def test_chosen_tier_is_only_the_users_explicit_choice(pool, meter):
    assert meter.chosen_tier(PAGE) is None                      # defaults are not a choice
    with pool.connection() as conn:
        update_settings(conn, "u1", now=NOW, tiers={"page": "good"})
    assert meter.chosen_tier(PAGE) == "good"
    assert meter.chosen_tier(Account(user_id="u1", group="curator")) is None
    assert meter.chosen_tier(Account(user_id="u2", group="page")) is None


@pytest.mark.parametrize("sql", ["update usage_record set cost_usd = 0",
                                 "delete from usage_record",
                                 "truncate usage_record"])
def test_usage_record_is_append_only(db_url, meter, sql):
    meter.record(PAGE, "p", U(1.0), replayed=False)
    with psycopg.connect(db_url) as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute(sql)


def test_proactive_group_is_recorded_and_the_check_constraint_allows_it(pool, meter):
    assert "proactive" in GROUPS and DEFAULT_TIERS["proactive"] == "good"
    meter.record(Account(user_id="u1", group="proactive"), "bot_probe", U(0.002), replayed=False)
    with pool.connection() as conn:
        assert spend_by_group(conn, "u1", day_start(NOW, UTC))["proactive"].cost_usd == \
            pytest.approx(0.002)
        names = [r["conname"] for r in conn.execute(
            "select conname from pg_constraint where conrelid = 'usage_record'::regclass "
            "and contype = 'c'")]
    assert "usage_record_grp_check" in names


def test_rsi_group_is_recorded_and_the_check_constraint_allows_it(pool, meter):
    """The harness round's own calls (rsi.h.*: improver, judge) are metered in group "rsi"."""
    from evowiki.wikiapp.llm import purpose_group
    assert "rsi" in GROUPS and DEFAULT_TIERS["rsi"] == "good"
    assert purpose_group("rsi.h.improve") == purpose_group("rsi.h.judge") == "rsi"
    meter.record(Account(user_id="u1", group="rsi"), "rsi.h.improve", U(0.003), replayed=False)
    with pool.connection() as conn:
        assert spend_by_group(conn, "u1", day_start(NOW, UTC))["rsi"].cost_usd == \
            pytest.approx(0.003)
