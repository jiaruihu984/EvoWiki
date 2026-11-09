"""The write queue's Bot side on Postgres (`PgIdleEnv`): the settings, the paused goals (B5), the
spending caps the learner set, and the 「显示没做的」 row a reached cap leaves."""
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.bot.settings import BotSettings
from evowiki.bot.why import recent
from evowiki.llm.usage import Account, Usage
from evowiki.store import bot as bot_store
from evowiki.store import bot_goals
from evowiki.store import usage as usage_store
from evowiki.wikiapp.write_queue import PgIdleEnv
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    n = iter(range(1000))
    return PgIdleEnv(pool, UTC, lambda p: f"{p}_{next(n)}")


def spend(pool, cost, at):
    with pool.connection() as conn:
        usage_store.insert_usage(conn, Account(user_id="u1", group="page"), "goals.organize",
                                 Usage(provider="p", model="m", input_tokens=1, output_tokens=1,
                                       cost_usd=cost), replayed=False, ts=at)


def test_settings_and_parked_goals_are_read_from_the_bot_tables(env, pool):
    assert env.settings("u1").kinds["idle_write"] is False and env.parked("u1") == set()
    with pool.connection() as conn:
        bot_store.put_settings(conn, "u1", BotSettings(kinds={"idle_write": True}), NOW)
        bot_goals.park(conn, "u1", "path-1", NOW)
    assert env.settings("u1").kinds["idle_write"] is True
    assert env.parked("u1") == {"path-1"}


def test_no_cap_means_no_stop_and_a_cap_stops_at_the_amount(env, pool):
    spend(pool, 5.0, NOW - timedelta(hours=1))
    assert env.cap_hit("u1", NOW) is None                       # no cap: the default
    with pool.connection() as conn:
        usage_store.update_settings(conn, "u1", now=NOW, daily_limit_usd=6.0)
    assert env.cap_hit("u1", NOW) is None
    spend(pool, 1.0, NOW - timedelta(minutes=5))
    assert env.cap_hit("u1", NOW) == "day"
    assert env.cap_hit("u1", NOW + timedelta(days=1)) is None   # a new day
    with pool.connection() as conn:
        usage_store.update_settings(conn, "u1", now=NOW, daily_limit_usd=None,
                                    monthly_limit_usd=6.0)
    assert env.cap_hit("u1", NOW + timedelta(days=1)) == "month"


def test_a_reached_cap_is_one_skipped_row_a_day_that_the_activity_list_shows(env, pool):
    env.note_skip("u1", "cap_reached", "今天的花费上限到了，空闲时不写页面", NOW)
    env.note_skip("u1", "cap_reached", "今天的花费上限到了，空闲时不写页面", NOW + timedelta(hours=2))
    rows = recent(pool, "u1", now=NOW + timedelta(hours=3), include_skipped=True)
    [row] = [r for r in rows if r.kind == "idle_write"]
    assert (row.result, row.gate_outcome, row.gate_reason) == ("skipped", "skip", "cap_reached")
    assert "上限" in (row.gate_detail or "")
    env.note_skip("u1", "cap_reached", "again", NOW + timedelta(days=1))      # the next day: a new row
    assert len([r for r in recent(pool, "u1", now=NOW + timedelta(days=1, hours=1),
                                  include_skipped=True) if r.kind == "idle_write"]) == 2
