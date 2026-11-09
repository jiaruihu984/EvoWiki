"""deep@v14 / standard@v9: an interactive has three uses (看懂 / 练熟 / 辨析), and the plan answers
`practice` (what the learner must remember or be able to do, and how he practises it on the page)
before listing the interactives. Fake gateway only."""

import pytest
from pydantic import ValidationError

from evowiki.deep import agent, skill
from evowiki.deep.agent import PlanAnswers
from evowiki.deep.contracts import RefinePlan
from evowiki.deep.evoui import TIERS_RULE
from evowiki.deep.tools import WriteBlockArgs
from evowiki.teach.standard import CHECKLIST, section
from tests.deep.fakes import PLAN, DeepScript, call, finish, turn
from tests.deep.fakes import section as sec
from tests.deep.test_writer import env, outline_input, writer  # noqa: F401 - fixture
from tests.wikiapp.test_page import organized

PRACTICE = "要记住：あ行和か行 10 个假名 → 闪卡接分页测验，先回想再核对"
CARDS = {"type": "evoui", "title": "あ行和か行", "question": "练熟：看到假名能马上读出来吗？",
         "state": {}, "derived": {},
         "body": {"kind": "box", "children": [
             {"kind": "caption", "text": "先在心里读出来，再翻面核对。"},
             {"kind": "flashcards", "id": "kana", "cards": [
                 {"front": "あ", "back": "a"}, {"front": "か", "back": "ka"},
                 {"front": "き", "back": "ki"}]},
             {"kind": "reveal", "mode": "after", "after": "kana", "children": [
                 {"kind": "quiz", "id": "q", "layout": "paged", "questions": [
                     {"q": "き 怎么读？", "options": ["ka", "ki"], "correct": 1, "why": "き = ki"}]}]}]},
         "fallback": "あ a、か ka、き ki：先看假名读出来，再核对。"}


def test_every_rule_that_decides_on_an_interactive_names_the_three_uses():
    # deep@v32: the three uses are said in the skill's entry and its interaction reference
    for text in (agent.WRITE_SYSTEM, skill.ref("interaction"), TIERS_RULE):
        for use in ("看懂", "练熟", "辨析"):
            assert use in text, (use, text[:40])
    assert "动手练习" in section("depth") and "练习" in section("visual_question")
    assert "练习" in next(c.text for c in CHECKLIST if c.id == "useful")
    assert "练熟" in WriteBlockArgs.model_fields["why"].description


def test_the_writer_is_no_longer_limited_to_comprehension_obstacles():
    w = agent.WRITE_SYSTEM
    assert "只做规划 visual_questions 里列了的" not in w
    assert "规划中已经确定要做的交互应当完成" in skill.ref("interaction")
    assert "规划漏了一个确实需要的" not in w                       # T4: no "as you go"
    assert "没有理解障碍要靠动手解决，就不做交互" not in TIERS_RULE
    assert "三样都没有，就不做交互" in TIERS_RULE
    assert "要会做的步骤" in skill.ref("interaction")


def test_plan_must_answer_practice_and_old_plans_still_parse():
    assert "不用当场练：" in PlanAnswers.model_fields["practice"].description
    assert "练熟" in PlanAnswers.model_fields["visual_questions"].description
    with pytest.raises(ValidationError):
        PlanAnswers.model_validate({k: v for k, v in PLAN.items() if k != "practice"})
    old = RefinePlan(known="k", observable="o", one_at_a_time=("a",), contrast="c", spoken_answer="s")
    assert old.practice == ""


def test_planned_practice_reaches_the_writer_and_its_block_stays_on_the_page(env):  # noqa: F811
    pid = organized(env)
    plan = {**PLAN, "practice": PRACTICE,
            "visual_questions": ["用处：练熟｜回答的学习问题：看到假名能马上读出来吗？｜为什么文字不够：只读记不住"]}
    body = "先认 10 个。\n\n{{block:kana}}\n"
    ds = DeepScript().plan(plan).write(
        turn(sec("s1", role="organizer", depth="quick")),
        turn(sec("s2", body=body, depth="interactive")),
        turn(call("write_block", block_id="kana", section_id="s2", why="练熟：认读 10 个假名",
                  block=CARDS)),
        turn(sec("s3", role="recap", depth="quick")), turn(finish()))
    res = writer(ds).write(outline_input(env, pid))
    # the plan is the writer's own plan_page call: it stays in its history, and on the draft
    history = [r for r in ds.requests if r.purpose == "deep.write"][1].messages
    assert history[1].tool_calls[0].args["plan"]["practice"] == PRACTICE
    assert res.outcome.draft.plan.practice == PRACTICE
    assert [b.block_id for b in res.outcome.draft.blocks] == ["kana"]
    assert res.outcome.checks["kana"].ok and not res.outcome.dropped


def test_a_plan_without_practice_is_refused_to_the_model(env):  # noqa: F811
    pid = organized(env)
    ds = DeepScript().plan({k: v for k, v in PLAN.items() if k != "practice"}).plan().write(
        turn(sec("s1", role="organizer", depth="quick")), turn(sec("s2")),
        turn(sec("s3", role="recap", depth="quick")), turn(finish()))
    res = writer(ds).write(outline_input(env, pid))
    second = [r for r in ds.requests if r.purpose == "deep.write"][1]
    err = next(m.text for m in second.messages if m.role == "tool" and m.is_error)
    assert "practice" in err and res.outcome.draft.plan.practice == PLAN["practice"]
