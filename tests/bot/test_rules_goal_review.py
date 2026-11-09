"""The weekly goal review rule (B5 spec §1.3): pure, facts in, intents out."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from evowiki.bot.facts import DueReview, UserFacts
from evowiki.bot.facts_types import GoalWeek, StuckPage, WeakPrereq
from evowiki.bot.rules import RuleInput, evaluate, iso_week, situation
from evowiki.bot.settings import DEFAULT_SETTINGS

NOW = datetime(2026, 10, 19, 0, 30, tzinfo=UTC)
TODAY = date(2026, 10, 19)


def week(**kw) -> GoalWeek:
    base = {"path_id": "pa", "title": "学统计", "pages_shown": 8, "pages_done": 2, "done_this_week": 2,
                "new_pages_this_week": 1, "next_key": "pa-p5", "next_page_id": None, "next_title": "第5页",
                "next_position": 5, "active_days": 3, "review_due": 0, "review_cap": 10, "wrong_cells": (),
                "weak_prereqs": (), "known_share": 0.1, "idle_declines": 0, "days_since_open": 1,
                "reading_seconds": None, "stuck": None}
    return GoalWeek(**{**base, **kw})


def run(*weeks, settings=DEFAULT_SETTINGS):
    facts = UserFacts(user_id="u", wrong_answers=(), due_review=DueReview(0, (), None), goals=(),
                      open_questions=(), signals=(), names={})
    return evaluate(RuleInput(facts, frozenset(), settings, NOW, TODAY, "weekly",
                              weeks=tuple(weeks)))


GAP = WeakPrereq(node_id="c0", name="标准差", for_title="第5页", taught="page",
                 page_key="pa-p7", page_title="第7页 标准差", before_key="pa-p5")


def test_iso_week():
    assert iso_week(date(2026, 10, 19)) == "2026-W43"
    assert iso_week(date(2026, 10, 18)) == "2026-W42"        # Sunday: the week before


def test_a_weak_prerequisite_taught_later_moves_it_first():
    [i], errs = run(week(weak_prereqs=(GAP,)))
    assert errs == []
    assert (i.kind, i.route, i.priority, i.dedup_key, i.source_key) == (
        "goal_review", "direct", 60, "goal_review:pa", "goal_review:pa:2026-W43")
    assert i.title == "先补「标准差」再往下"
    assert i.reason == "本周读完 2 页（共 8 页）；「第5页」要用到「标准差」，它还没打牢"
    assert i.payload["situation"] == "prereq_gap"
    assert (i.payload["page_key"], i.payload["before_key"]) == ("pa-p7", "pa-p5")


def test_a_missing_prerequisite_is_added_and_one_the_user_fixed_is_not():
    miss = replace(GAP, taught="missing", page_key=None, page_title=None)
    assert run(week(weak_prereqs=(miss,)))[0][0].payload["situation"] == "prereq_missing"
    fixed = replace(GAP, taught="fixed")
    done = replace(GAP, taught="done")
    ordered = replace(GAP, taught="ordered")
    [i] = run(week(weak_prereqs=(fixed, done, ordered)))[0]
    assert i.payload["situation"] == "normal"


@pytest.mark.parametrize(("kw", "sit", "title"), [
    ({"reading_seconds": 4000, "stuck": StuckPage("pg_5", "第5页", 3700)}, "stuck_page",
     "「第5页」读了 61 分钟还没学完，换个讲法？"),
    ({"review_due": 20, "new_pages_this_week": 2}, "review_debt", "这周先复习，再读新页"),
    ({"known_share": 0.6}, "mostly_known", "后面大半你已经会了，重新规划？"),
    ({"days_since_open": 15, "idle_declines": 2}, "want_park", "先把「学统计」搁置？"),
])
def test_each_situation(kw, sit, title):
    [i] = run(week(**kw))[0]
    assert (i.payload["situation"], i.title) == (sit, title)


def test_stuck_needs_known_reading_time_and_the_threshold():
    stuck = StuckPage("pg_5", "第5页", 3700)
    assert situation(week(stuck=stuck, reading_seconds=None), DEFAULT_SETTINGS)[0] == "normal"
    assert situation(week(stuck=replace(stuck, seconds=3500), reading_seconds=3500),
                     DEFAULT_SETTINGS)[0] == "normal"
    [i] = run(week(stuck=stuck, reading_seconds=4000))[0]
    assert i.page_id == "pg_5" and i.reason.startswith("本周读完 2 页（共 8 页），读了 66 分钟")


def test_thresholds_that_are_not_met():
    s = DEFAULT_SETTINGS
    assert situation(week(review_due=20, new_pages_this_week=1), s)[0] == "normal"
    assert situation(week(review_due=19, new_pages_this_week=3), s)[0] == "normal"
    assert situation(week(known_share=0.6, pages_done=7), s)[0] == "normal"     # one page left
    assert situation(week(days_since_open=15, idle_declines=1), s)[0] == "normal"


def test_priority_order():
    g = week(weak_prereqs=(GAP,), review_due=30, new_pages_this_week=3, known_share=0.9)
    assert situation(g, DEFAULT_SETTINGS)[0] == "prereq_gap"
    assert situation(replace(g, weak_prereqs=()), DEFAULT_SETTINGS)[0] == "review_debt"


def test_normal_goals_make_one_quiet_intent():
    out, _ = run(week(path_id="a"), week(path_id="b"), week(path_id="c", known_share=0.7))
    normal = [i for i in out if i.payload["situation"] == "normal"]
    assert len(out) == 2 and len(normal) == 1
    [n] = normal
    assert (n.source_key, n.title, n.payload["paths"]) == (
        "goal_review:normal:2026-W43", "每周回顾：2 个目标都正常", ["a", "b"])
    assert run()[0] == []
