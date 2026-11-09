"""The weekly goal review inside the daily check (B5 spec §1.1 / §1.4): once per ISO week on or
after the review weekday, snapshots written with the intents, cards per situation, a quiet
normal week."""

from datetime import date, datetime, timedelta

import pytest

from evowiki.bot import pipeline
from evowiki.bot.pipeline import dispatch, run_daily
from evowiki.bot.schedule import Slot
from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.bot.why import recent
from evowiki.store import bot as store
from evowiki.store import bot_goals
from evowiki.wikiapp.repo import Commit
from tests.bot.test_goal_week import NOW, make_repo, seed, wrong

_n = iter(range(100_000))
MONDAY = date(2026, 10, 19)


@pytest.fixture
def repo(pool):
    return make_repo(pool)


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


def slot(d: date) -> Slot:
    return Slot(d, datetime.combine(d, NOW.time(), tzinfo=NOW.tzinfo) - timedelta(days=0))


def daily(pool, d=MONDAY, now=NOW):
    return run_daily(pool, "u1", slot(d), now=now, new_id=new_id)


def triggers(pool, kind="goal_review"):
    with pool.connection() as conn:
        return [t for t in store.triggers_since(conn, "u1", NOW - timedelta(days=30))
                if t.kind == kind]


def reviews(pool, week_start):
    with pool.connection() as conn:
        rows = conn.execute("select path_id, situation, trigger_id from bot_goal_review "
                            "where user_id = 'u1' and week_start = %s", (week_start,)).fetchall()
    return [(r["path_id"], r["situation"], r["trigger_id"]) for r in rows]


def weak_c0(repo):
    repo.commit(repo.load("u1"), Commit(aspect=[
        wrong("c0.interpret", "review:pg_x:s1", NOW - timedelta(days=1))]))


def test_monday_review_makes_a_card_and_a_snapshot_once(pool, repo):
    seed(repo)
    weak_c0(repo)
    ids = daily(pool)
    [t] = triggers(pool)
    assert t.trigger_id in ids and t.payload["situation"] == "prereq_gap"
    assert reviews(pool, MONDAY) == [("path-k", "prereq_gap", t.trigger_id)]
    d = dispatch(pool, t.trigger_id, now=NOW, new_id=new_id)
    assert d.outcome == "pass"
    with pool.connection() as conn:
        [card] = store.suggestions_of_trigger(conn, t.trigger_id)
    assert (card.kind, card.title, card.action, card.priority) == (
        "goal_review", "先补「概念0」再往下", {"type": "open_goal", "path_id": "path-k"}, 60)
    assert "要用到「概念0」" in card.why
    # the same week again: nothing new
    assert [i for i in daily(pool, now=NOW + timedelta(hours=1)) if i in
            {x.trigger_id for x in triggers(pool)}] == []
    assert len(triggers(pool)) == 1


def test_a_normal_week_is_one_quiet_line(pool, repo):
    seed(repo)
    daily(pool)
    [t] = triggers(pool)
    assert t.payload == {"situation": "normal", "paths": ["path-k"], "week": "2026-W43"}
    d = dispatch(pool, t.trigger_id, now=NOW, new_id=new_id)
    assert (d.outcome, d.suggestion_id) == ("pass", None)
    with pool.connection() as conn:
        assert store.suggestions_of_trigger(conn, t.trigger_id) == []
    [a] = [a for a in recent(pool, "u1", now=NOW) if a.kind == "goal_review"]
    assert (a.result, a.title, a.gate_detail) == (
        "reviewed", "每周回顾：1 个目标都正常", "每周回顾：1 个目标这周都正常")
    assert reviews(pool, MONDAY) == [("path-k", "normal", t.trigger_id)]


def test_tuesday_catches_up_and_a_week_switched_off_is_skipped(pool, repo):
    seed(repo)
    tuesday = MONDAY + timedelta(days=1)
    daily(pool, tuesday, NOW + timedelta(days=1))
    assert [r[0] for r in reviews(pool, MONDAY)] == ["path-k"]
    # the week before was never reviewed (computer off): it is not caught up
    assert reviews(pool, MONDAY - timedelta(days=7)) == []


def test_review_weekday_setting(pool, repo):
    seed(repo)
    with pool.connection() as conn:
        store.put_settings(conn, "u1", DEFAULT_SETTINGS.model_copy(update={"review_weekday": 2}),
                           NOW)
    daily(pool)
    assert triggers(pool) == []                                   # Monday < Wednesday
    daily(pool, MONDAY + timedelta(days=2), NOW + timedelta(days=2))
    assert len(triggers(pool)) == 1


def test_time_zone_week_boundary(pool, repo):
    """Sunday 23:30 local belongs to the week before; Monday 00:30 starts the new one."""
    seed(repo)
    with pool.connection() as conn:
        bot_goals.put_review(conn, "u1", week_start=MONDAY - timedelta(days=7), path_id="path-k",
                             situation="normal", facts={}, trigger_id=None, at=NOW)
    sunday_2330 = datetime(2026, 10, 18, 15, 30, tzinfo=NOW.tzinfo)   # 23:30 Asia/Shanghai
    daily(pool, MONDAY - timedelta(days=1), sunday_2330)
    assert triggers(pool) == []
    monday_0030 = datetime(2026, 10, 18, 16, 30, tzinfo=NOW.tzinfo)   # 00:30 Asia/Shanghai
    daily(pool, MONDAY, monday_0030)
    assert len(triggers(pool)) == 1 and len(reviews(pool, MONDAY)) == 1


def test_a_failing_weekly_review_keeps_the_days_intents(pool, repo, monkeypatch):
    """Review fix 2: the weekly part runs in a savepoint; its error is logged, the daily intents
    commit, and the next daily check of the week tries the review again."""
    seed(repo)
    def boom(*a, **kw):
        raise RuntimeError("graph unreadable")
    monkeypatch.setattr(pipeline, "load_goal_week", boom)
    ids = daily(pool, MONDAY, NOW + timedelta(days=5))       # the goal is idle by then
    assert ids and triggers(pool) == [] and reviews(pool, MONDAY) == []
    assert [t.kind for t in triggers(pool, "goal_idle")] == ["goal_idle"]
    monkeypatch.undo()
    daily(pool, MONDAY + timedelta(days=1), NOW + timedelta(days=1))
    assert len(triggers(pool)) == 1


def test_a_backfilled_slot_of_last_week_is_not_reviewed_this_week(pool, repo):
    """Review fix 7: Sunday's slot run on Monday morning (catch-up) does not review last week."""
    seed(repo)
    daily(pool, MONDAY - timedelta(days=1), NOW)
    assert triggers(pool) == [] and reviews(pool, MONDAY - timedelta(days=7)) == []


def test_parked_goal_is_not_reviewed(pool, repo):
    seed(repo)
    with pool.connection() as conn:
        bot_goals.park(conn, "u1", "path-k", NOW - timedelta(days=1))
    daily(pool)
    assert triggers(pool) == [] and reviews(pool, MONDAY) == []
    assert triggers(pool, "goal_idle") == []


@pytest.mark.parametrize(("sit", "action"), [
    ("stuck_page", {"type": "open_page", "page_id": "pg_5",
                    "ask": "这一页我读了很久还是没弄懂，能换个讲法吗？"}),
    ("review_debt", {"type": "start_practice"}),
    ("mostly_known", {"type": "replan_goal", "path_id": "pa"}),
    ("want_park", {"type": "park_goal", "path_id": "pa"}),
])
def test_card_actions(sit, action):
    row = store.TriggerRow(
        trigger_id="t", user_id="u1", kind="goal_review", rule_id="goal_review", rule_version=1,
        source_key="k", dedup_key="goal_review:pa", route="direct", evidence=[],
        payload={"situation": sit, "path_id": "pa", "page_id": "pg_5"}, priority=60, reason="r",
        created_at=NOW, title="标题")
    card = pipeline.card_for(row, DEFAULT_SETTINGS, NOW)
    assert (card.title, card.action) == ("标题", action)
