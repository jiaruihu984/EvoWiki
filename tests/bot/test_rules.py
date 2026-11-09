import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from evowiki.bot import rules as R
from evowiki.bot.facts import (
    DueReview,
    GoalFact,
    OpenQuestion,
    SignalFact,
    SourceMatchFact,
    UserFacts,
)
from evowiki.bot.rules import RULES, RuleInput, _chat, evaluate
from evowiki.bot.settings import DEFAULT_SETTINGS

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)


def facts(**kw):
    base = {"user_id": "u", "wrong_answers": (), "due_review": DueReview(0, (), None), "goals": (),
                "open_questions": (), "signals": (), "names": {"c1.interpret": "导数·解释"}}
    return UserFacts(**{**base, **kw})


def run(f, new=(), mode="event"):
    return evaluate(RuleInput(f, frozenset(new), DEFAULT_SETTINGS, NOW, NOW.date(), mode))


def test_review_due():
    f = facts(due_review=DueReview(4, ("c1.a",), "p1"))
    [i] = run(f, mode="daily")[0]
    assert (i.reason, i.source_key, i.priority, i.page_id) == (
        "今天有 4 张卡片到期", "review_due:2026-10-14", 50, "p1")
    assert i.payload == {"count": 4, "cells": ["c1.a"], "page_id": "p1"}
    assert run(facts(), mode="daily")[0] == []


def test_goal_idle():
    g = GoalFact("g1", "学微积分", "p9", NOW - timedelta(days=3, hours=5), NOW - timedelta(days=30))
    fresh = GoalFact("g2", "新", None, None, NOW - timedelta(days=1))
    [i] = run(facts(goals=(g, fresh)), mode="daily")[0]
    assert i.reason == "目标「学微积分」已经 3 天没打开相关页面"
    assert (i.dedup_key, i.source_key, i.priority, i.page_id) == (
        "goal_idle:g1", "goal_idle:g1:2026-10-14", 40, "p9")
    assert i.payload == {"path_id": "g1", "title": "学微积分", "page_id": "p9", "days": 3}
    old = GoalFact("g3", "老", None, None, NOW - timedelta(days=4))
    assert len(run(facts(goals=(old,)), mode="daily")[0]) == 1


def sig(i, p=-1, kind=None, written=False):
    return SignalFact(f"s{i}", NOW, kind or ("section_deeper" if p < 0 else "section_know"), p,
                      written=written)


def test_strategy_negative():
    f = facts(signals=(sig(0, 1), sig(1), sig(2), sig(3)))
    [i] = run(f, {"s3"})[0]
    assert (i.route, i.source_key, i.evidence, i.priority, i.rule_version) == (
        "rsi", "strategy:s3", ("s1", "s2", "s3"), 30, 2)
    assert i.payload == {"signals": ["s1", "s2", "s3"], "written_only": False}
    assert i.dedup_key == "strategy_negative"
    assert run(f, {"s0"})[0] == []
    assert run(facts(signals=(sig(1), sig(2), sig(3, 1))), {"s3"})[0] == []
    assert run(facts(signals=(sig(2), sig(3))), {"s3"})[0] == []


def test_strategy_negative_counts_only_teaching_signals():
    """v2: wrong answers are the wrong-answer rule's and RSI-A's business, not the explanation's."""
    wrong = tuple(sig(i, kind="check_answer") for i in range(3))
    assert run(facts(signals=wrong), {"s2"})[0] == []
    # answers between the explanation signals neither count nor break the streak
    mixed = (sig(1), sig(2, kind="review_grade"), sig(3, kind="check_unclear"),
             sig(4, kind="block_result"), sig(5, kind="card_feedback"))
    [i] = run(facts(signals=mixed), {"s5"})[0]
    assert i.evidence == ("s1", "s3", "s5")
    # a 「我懂了」 is a teaching signal: it breaks the streak
    assert run(facts(signals=(sig(1), sig(2), sig(3, 1), sig(4))), {"s4"})[0] == []


def test_strategy_negative_written_only():
    f = facts(signals=tuple(sig(i, kind="page_feedback", written=True) for i in range(3)))
    [i] = run(f, {"s2"})[0]
    assert i.payload["written_only"] is True
    f = facts(signals=(sig(0, kind="page_feedback", written=True), sig(1), sig(2)))
    assert run(f, {"s2"})[0][0].payload["written_only"] is False


def test_open_question():
    old = OpenQuestion("s1", "p1", "x" * 80, NOW - timedelta(days=3), False)
    seen = OpenQuestion("s2", "p2", "y", NOW - timedelta(days=9), True)
    young = OpenQuestion("s3", "p3", "z", NOW - timedelta(days=1), False)
    [i] = run(facts(open_questions=(old, seen, young)), mode="daily")[0]
    assert i.reason == "上次在这一页还没懂：" + "x" * 60
    assert (i.dedup_key, i.source_key, i.priority, i.page_id) == (
        "open_q:s1", "open_q:s1:2026-10-14", 45, "p1")
    assert i.payload == {"page_id": "p1", "text": "x" * 80, "session_id": "s1"}


def test_evaluate_reports_raising_rule(monkeypatch):
    def boom(inp):
        raise ValueError("bad")
    rules = tuple(replace(r, fn=boom) if r.rule_id == "strategy_negative" else r for r in RULES)
    monkeypatch.setattr(R, "RULES", rules)
    f = facts(signals=(sig(1), sig(2), sig(3)), source_matches=(match(),))
    out, errs = run(f, {"s3", "sm1"})
    assert [i.rule_id for i in out] == ["source_added"]
    assert errs == [("strategy_negative", "ValueError: bad")]


def match(eid="sm1", pages=(("p1", "特征值"), ("p2", "对角化"), ("p3", "谱定理"))):
    return SourceMatchFact(eid, "src1", "线性代数讲义", pages)


def test_source_added_one_intent_per_new_match():
    assert [r.mode for r in RULES if r.rule_id == "source_added"] == ["event"]
    out, errs = run(facts(source_matches=(match(),)), {"sm1"})
    [i] = out
    assert errs == [] and i.kind == "source_added"
    assert (i.rule_id, i.rule_version, i.source_key, i.dedup_key, i.route) == (
        "source_added", 1, "source_added:src1", "source_added:src1", "direct")
    assert (i.evidence, i.priority, i.placement, i.page_id) == (("sm1",), 40, "today", None)
    assert i.payload == {"source_id": "src1", "title": "线性代数讲义", "pages": [
        {"page_id": "p1", "title": "特征值"}, {"page_id": "p2", "title": "对角化"},
        {"page_id": "p3", "title": "谱定理"}]}
    assert i.title == "《线性代数讲义》讲到了 3 页已有的内容"
    assert i.reason == "《线性代数讲义》讲到了 3 页已有的内容：特征值、对角化、谱定理。要按它更新吗？"


def test_source_added_needs_a_new_event_and_pages():
    assert run(facts(source_matches=(match(),)), {"other"})[0] == []
    assert run(facts(source_matches=(match(pages=()),)), {"sm1"})[0] == []


def test_payloads_json_able():
    f = facts(signals=(sig(1), sig(2), sig(3)),
              due_review=DueReview(1, ("c",), None),
              goals=(GoalFact("g", "t", None, None, NOW - timedelta(days=9)),),
              open_questions=(OpenQuestion("s", "p", "t", NOW - timedelta(days=9), False),))
    for mode in ("event", "daily"):
        out, errs = run(f, {"s3"}, mode)
        assert out and not errs
        for i in out:
            json.dumps(i.payload)


# --- titles (what the intent is about) and the page-chip rule ---------------------------------

def test_intent_titles():
    f = facts(due_review=DueReview(4, ("c1.interpret",), "pg1"),
              goals=(GoalFact("pa", "学统计", "pg1", None, NOW - timedelta(days=9)),
                     GoalFact("pb", "学概率", None, None, NOW - timedelta(days=9))),
              open_questions=(OpenQuestion("as1", "pg1", "标准差", NOW - timedelta(days=5), False),),
              names={"c1.interpret": "导数（解释）", "pg1": "均值"},
              labels={"pg1": "均值（2/5）"})
    daily = {i.kind: i for i in run(f, mode="daily")[0]}
    assert daily["review_due"].title == "复习 4 张卡片"
    assert daily["open_question"].title == "标准差"
    goals = [i for i in run(f, mode="daily")[0] if i.kind == "goal_idle"]
    assert sorted(g.title for g in goals) == ["学概率", "学统计"]


def test_strategy_title():
    f = facts(signals=tuple(SignalFact(f"s{i}", NOW, "expand", -1) for i in range(3)))
    [i] = run(f, {"s2"})[0]
    assert i.title == "讲解方式调整"


def page_in(**kw):
    from evowiki.bot.facts_types import PageFacts
    base = {"page_id": "pg1", "label": "均值（1/3）", "open_text": None}
    return RuleInput(facts(), frozenset(), DEFAULT_SETTINGS, NOW, NOW.date(), "page",
                     PageFacts(**{**base, **kw}))


def test_the_unregistered_chat_rule_still_works_when_called_directly():
    [i] = _chat(page_in(open_text="标准差的含义"))
    assert (i.kind, i.route, i.placement, i.page_id) == (
        "chat", "direct", "page", "pg1")
    assert i.source_key == f"chat:pg1:{NOW.date()}" and i.title == "均值（1/3）的 1 条小建议"
    assert i.payload == {"chips": [{"text": "上次还没懂：标准差的含义，能再讲讲吗？"}]}


def test_chat_rule_is_not_registered_so_no_suggestions_are_made():
    """Removed from the UI 2026-10-21: the code stays, the rule is out of RULES."""
    assert _chat(page_in()) == []
    assert evaluate(page_in(open_text="标准差的含义")) == ([], [])
    assert [r.rule_id for r in RULES if r.mode == "page"] == []
