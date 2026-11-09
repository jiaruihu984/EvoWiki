"""review.followup_check (followup-check@v5) offline cases. Information flow (the product specification
2026-10-11-llm-information-flow.md): rule ASK — the confirmation question asks again about a
cell the learner just answered wrong, so it sees that evidence (the original question, what he
picked, why he says he missed it — 粗心 / 学会了 — and K for the cell) and never re-tests what
the answer already showed (the same question, reworded or reshuffled); rule NO_VERIFIER — one
generator call, the new item only structurally checked, no verifier call. The prompt carries the
teaching text of that cell only, no ids.
Rule ITEM_FLAWS — the prompt carries the item-writing rules (goals.itemflaws.ITEM_RULES_ZH, with
the distractor procedure); rule FOLLOWUP_NEW — same cell and same construct, a different stem and
different options, the original's distractors not reused (code: `near_duplicate`).
Harness: a valid question is scheduled, an unparseable / ungradable one fails closed (LLMFailed,
nothing recorded); a flawed or near-duplicate one gets one more call with the reasons (attempt 2),
flawed twice → LLMFailed with the reasons, nothing recorded; 看不懂 makes no call."""

import pytest

from evowiki.goals.itemflaws import ITEM_RULES_ZH
from evowiki.wikiapp.followup import CLAIM_ZH, PURPOSE
from evowiki.wikiapp.llm import LLM_FAILURES, LLMFailed
from evowiki.wikiapp.repo import MemoryRepo
from tests.wikiapp.script import Script
from tests.wikiapp.test_followup import CELL, NEW_Q, fu, item_of, script_question, wrong
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_page import organized


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def recorded(env):
    return len(env[0].aspect_log("u1")), len(env[0]._wiki["u1"])


def setup(env):
    pid = organized(env)
    wrong(env, pid)
    return pid


# --- (a) what the prompt must and must not contain ---------------------------------------------

def test_prompt_has_the_original_question_its_teaching_and_the_no_repeat_rule(env):
    pid = setup(env)
    item = item_of(env, pid)
    script_question(env)
    fu(env, pid, "slip")
    p = env[1].requests(PURPOSE)[0].prompt
    for s in ("在「z 分数」答错了一道关于「z 分数」的 interpret 的题", "z 告诉你离均值几个标准差",
              item.stem, *(f"{j}) {o}" for j, o in enumerate(item.options)),
              "新题不能和它一样，也不要只是换个说法或调换选项", "测的仍是同一个知识点，换一个角度或情境"):
        assert s in p, s
    for s in ("s1 正文", "s3 正文", pid, CELL, "c2."):
        assert s not in p, s


@pytest.mark.parametrize("choice, words", [("slip", "粗心"), ("learned", "学会")])
def test_prompt_sees_the_learners_answer_and_claim(env, choice, words):
    pid = setup(env)
    item = item_of(env, pid)
    picked = item.options[(item.answer + 1) % len(item.options)]
    script_question(env)
    fu(env, pid, choice)
    p = env[1].requests(PURPOSE)[0].prompt
    assert f"他选的：{picked}" in p and f"他说为什么答错：{CLAIM_ZH[choice]}" in p and words in p
    assert "系统对他这一格的判断：" in p and "他在这一格上的作答和记录：" in p


# --- (a) harness ---------------------------------------------------------------------------------

def test_valid_question_is_scheduled_one_generator_call_no_verifier(env):
    pid = setup(env)
    script_question(env)
    out = fu(env, pid, "learned")
    assert out.status == "pending" and out.llm_calls == 1
    f = env[0].load("u1").state.followups[out.followup_id]
    assert f.item.verified and f.item.cell == CELL and f.item.options[f.item.answer] == NEW_Q[
        "options"][0]
    assert env[1].roles(PURPOSE) == ["anthropic"] and env[1].calls("goals.diagnostic.verify") == 0


def _orig(env, pid, **upd):
    item = item_of(env, pid)
    return {"stem": item.stem, "options": list(item.options), "answer": item.answer, **upd}


@pytest.mark.parametrize("make", [
    lambda o: o,                                                             # the same question
    lambda o: {**o, "options": list(reversed(o["options"])),
               "answer": len(o["options"]) - 1 - o["answer"]},              # reshuffled
    lambda o: {**o, "stem": "换个说法：" + o["stem"]},                       # reworded stem only
    lambda o: {**o, "options": ["x", "y", "z"], "answer": 0},                 # same stem
], ids=["same", "reshuffled", "reworded_stem_same_options", "same_stem"])
def test_followup_never_re_tests_the_original(env, make):
    pid = setup(env)
    seq = recorded(env)
    bad = make(_orig(env, pid))
    env[1].push(PURPOSE, bad, bad)                    # one regeneration with the reasons
    with pytest.raises(LLMFailed):
        fu(env, pid, "slip")
    assert recorded(env) == seq and env[1].calls(PURPOSE) == 2
    assert env[1].requests(PURPOSE)[1].params == {"attempt": 2}


@pytest.mark.parametrize("bad", [
    "不是 JSON",
    {"stem": "q", "options": ["a"], "answer": 0},
    {"stem": "q", "options": ["a", "b", "c"], "answer": 3},
    {"stem": "", "options": ["a", "b", "c"], "answer": 0},
])
def test_unusable_question_fails_closed(env, bad):
    pid = setup(env)
    seq = recorded(env)
    env[1].push(PURPOSE, bad)
    with pytest.raises((LLMFailed, *LLM_FAILURES)):     # the endpoint answers 502 for each
        fu(env, pid, "learned")
    assert recorded(env) == seq and env[0].load("u1").state.followups == {}


def test_unclear_makes_no_call(env):
    pid = setup(env)
    out = fu(env, pid, "unclear")
    assert out.llm_calls == 0 and env[1].calls(PURPOSE) == 0


# --- ITEM_FLAWS / FOLLOWUP_NEW --------------------------------------------------------------------

NEGATIVE = {"stem": "下面哪个不是 z = 2 的意思？",
            "options": ["低于均值 2 个标准差", "高于均值 2 个标准差", "是 2%"], "answer": 0}


def test_prompt_carries_the_item_rules_and_the_same_construct_rule(env):
    pid = setup(env)
    script_question(env)
    fu(env, pid, "slip")
    req = env[1].requests(PURPOSE)[0]
    assert ITEM_RULES_ZH in req.prompt and "先自己把题解一遍" in req.prompt
    assert "考的是同一件事" in req.prompt and "原题里的错误选项不要再用" in req.prompt
    assert req.params == {"attempt": 1}


def test_flawed_then_clean_question_uses_the_clean_one_two_calls(env):
    pid = setup(env)
    env[1].push(PURPOSE, NEGATIVE, NEW_Q)
    out = fu(env, pid, "learned")
    f = env[0].load("u1").state.followups[out.followup_id]
    assert out.llm_calls == 2 and f.item.options[f.item.answer] == NEW_Q["options"][0]
    second = env[1].requests(PURPOSE)[1]
    assert second.params == {"attempt": 2} and "否定问法" in second.prompt


def test_flawed_twice_fails_closed_with_the_reasons(env):
    pid = setup(env)
    seq = recorded(env)
    env[1].push(PURPOSE, NEGATIVE, NEGATIVE)
    with pytest.raises(LLMFailed, match="否定问法"):
        fu(env, pid, "slip")
    assert recorded(env) == seq and env[0].load("u1").state.followups == {}


def test_reused_distractor_is_a_near_duplicate(env):
    pid = setup(env)
    item = item_of(env, pid)
    distractor = item.options[(item.answer + 1) % len(item.options)]
    reuse = {"stem": "某人 z = 3，说明什么？", "options": ["高于均值 3 个标准差", distractor, "是 3%"],
             "answer": 0}
    env[1].push(PURPOSE, reuse, NEW_Q)
    out = fu(env, pid, "slip")
    assert out.llm_calls == 2
    assert "沿用了原题的错误选项" in env[1].requests(PURPOSE)[1].prompt


REPEATED = {"stem": "某人 z = 3，说明什么？",
            "options": ["高于均值 3 个标准差", "高于均值 3 个标准差 ", "是 3%"], "answer": 0}


def test_repeated_options_get_the_one_retry_like_other_flaws(env):
    """Two options equal after whitespace / case (CheckItem refuses them: two right answers) are
    an item flaw 「选项重复」: one more call with the reason, not an immediate format failure."""
    pid = setup(env)
    env[1].push(PURPOSE, REPEATED, NEW_Q)
    out = fu(env, pid, "learned")
    assert out.llm_calls == 2
    second = env[1].requests(PURPOSE)[1]
    assert second.params == {"attempt": 2} and "选项重复" in second.prompt


def test_repeated_options_twice_fail_closed_with_the_reason(env):
    pid = setup(env)
    seq = recorded(env)
    env[1].push(PURPOSE, REPEATED, REPEATED)
    with pytest.raises(LLMFailed, match="选项重复"):
        fu(env, pid, "slip")
    assert recorded(env) == seq and env[0].load("u1").state.followups == {}
