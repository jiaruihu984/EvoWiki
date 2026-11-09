"""review.card_rewrite (card-rewrite@v6) offline cases. Information flow (the product specification
2026-10-11-llm-information-flow.md): rule ASK — the check rewrite asks the learner again, so it
sees the existing evidence for the card's cell (the learner's K / answers on it) besides the
card, the teaching text of that cell and the learner's words; rule NO_VERIFIER — there is no
separate verifier call: one generator call, the new check item is only structurally checked
(CheckItem, a changed stem / option set, shuffled). The cue rewrite sees only its own section
(heading, body, current cue) and the learner's words. Neither prompt carries other cells'
sections, page / concept ids or internal labels.
Rule ITEM_FLAWS — the check prompt carries the item-writing rules (goals.itemflaws.ITEM_RULES_ZH,
with the distractor procedure); a rewritten check with a flaw code can see gets one more call with
the reasons; rule FOLLOWUP_NEW — a rewritten check too close to the old one (same stem, same option
set, an old distractor reused) is treated the same way. The cue prompt carries the card rules (one
specific point, one stable answer, no summary-style cue).
Harness: valid cue / check saved, unchanged / summary-style cue or ungradable check → invalid_output
with the old card untouched and no verifier call, unparseable → llm_failed; flawed then clean → the
clean check, 2 calls; flawed twice → not_verified, the old card kept."""

import pytest

from evowiki.belief.aspects import AddNote, AspectKView
from evowiki.goals.contracts import CheckItem
from evowiki.goals.guardrails import BAND_ZH
from evowiki.goals.itemflaws import ITEM_RULES_ZH, item_flaws, near_duplicate
from evowiki.llm.prompt_steps import strip_marks
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.review import card_id
from evowiki.wikiapp.review_cards import CUE_MAX, PURPOSE
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_review_cards import NEW_CHECK, fb, page_of
from tests.wikiapp.test_review_flow import LATER, organized_learned_page

CHECK_CELL = "c1.interpret"
VERIFY = "goals.diagnostic.verify"


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def prompt_of(env):
    return env[1].requests(PURPOSE)[-1].prompt


def no_verifier(env):
    """Every call of this purpose went to the generator provider; nothing to a verifier."""
    s = env[1]
    assert set(s.roles(PURPOSE)) == {"anthropic"} and s.calls(VERIFY) == 0
    assert "deepseek" not in {p for p, _ in s._served}


# --- (a) what the prompts must and must not contain ---------------------------------------------

def test_cue_prompt_has_its_section_and_the_learners_words_only(env):
    pid = organized_learned_page(env)
    t = page_of(env, pid).page.texts["s2"]
    env[1].push(PURPOSE, {"cue": "z 告诉你离均值几个标准差", "answer": "一句答案"})
    fb(env, card_id(pid, "s2", "cue"), "unclear", "看不懂在问什么")
    p = prompt_of(env)
    for s in ("「z 分数」", "「z 分数怎么读」", t.body, f"现在的回想提示：{t.cue}",
              "他的话：看不懂在问什么", f"不超过 {CUE_MAX} 字", "不要和现在的一模一样"):
        assert s in p, s
    for s in ("s1 正文", "s3 正文", "z=-1 表示？", "低 1 分", pid, "c1.", "c2."):
        assert s not in p, s


def test_check_prompt_has_the_card_its_teaching_and_the_learners_words(env):
    pid = organized_learned_page(env)
    env[1].push(PURPOSE, NEW_CHECK)
    # organizer@v9 shuffles a page's check options: read where the key went
    key = next(s.check.answer for s in page_of(env, pid).page.outline.sections if s.id == "s4")
    fb(env, card_id(pid, "s4", "check"), "wrong", "标准答案不对")
    p = prompt_of(env)
    for s in ("测的是「z 分数」的 interpret", "【z 分数怎么读】z 告诉你离均值几个标准差",
              "z=-1 表示？", f"{key}) 低一个标准差", f"（现在的标准答案是第 {key} 项）",
              "他的反馈：答案不对。他的话：标准答案不对", "不要只顺着他说", "不要和现在的题一模一样"):
        assert s in p, s
    for s in ("s1 正文", "s3 正文", "s2 的要点", pid, "c1.", "c2."):    # not its cell / ids
        assert s not in p, s


def test_check_prompt_sees_the_cells_evidence(env):
    """The cell's band and his answers on it (a note's label), not other cells' notes."""
    repo, _, ids = env
    pid = organized_learned_page(env)
    repo.commit(repo.load("u1"), Commit(aspect=[
        AddNote(event_id=ids("aev"), ts=LATER, cell=CHECK_CELL, text="高一个标准差",
                evidence_ref="quiz:x", label="题目：z=1 表示？；选了「高一个标准差」"),
        AddNote(event_id=ids("aev"), ts=LATER, cell="c1.procedure", text="算对了",
                evidence_ref="quiz:y")]))
    env[1].push(PURPOSE, NEW_CHECK)
    fb(env, card_id(pid, "s4", "check"), "wrong", "")
    kv = AspectKView(repo.load("u1").k, LATER)
    p = prompt_of(env)
    assert f"系统对他这一格的判断：{BAND_ZH[kv.cell(CHECK_CELL).band]}" in p
    assert "新题不要再测这些作答已经说明了的东西" in p
    assert "- 题目：z=1 表示？；选了「高一个标准差」" in p and "算对了" not in p


def test_check_prompt_without_answers_says_so(env):
    pid = organized_learned_page(env)
    env[1].push(PURPOSE, NEW_CHECK)
    fb(env, card_id(pid, "s4", "check"), "wrong", "")
    assert "他在这一格上的作答和记录：（暂无）" in prompt_of(env)


# --- (a) harness ---------------------------------------------------------------------------------

def test_valid_cue_is_saved_one_generator_call(env):
    pid = organized_learned_page(env)
    env[1].push(PURPOSE, {"cue": "回想：z 告诉你离均值几个标准差", "answer": "一句答案"})
    out = fb(env, card_id(pid, "s2", "cue"), "unclear", "")
    assert out.rewritten and out.card.cue == "z 告诉你离均值几个标准差"
    no_verifier(env)


def test_valid_check_is_saved_structurally_checked_no_verifier(env):
    pid = organized_learned_page(env)
    env[1].push(PURPOSE, NEW_CHECK)
    out = fb(env, card_id(pid, "s4", "check"), "wrong", "")
    item = page_of(env, pid).page.outline.sections[3].check
    assert out.rewritten and item.verified and item.options[item.answer] == NEW_CHECK["options"][0]
    no_verifier(env)


def test_unchanged_cue_is_invalid_output_no_verifier(env):
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    env[1].push(PURPOSE, {"cue": "  回想：" + before.page.texts["s2"].cue, "answer": before.page.texts["s2"].answer})
    out = fb(env, card_id(pid, "s2", "cue"), "unclear", "")
    assert not out.rewritten and out.reason == "invalid_output" and page_of(env, pid) == before
    no_verifier(env)


@pytest.mark.parametrize("bad", [
    {"stem": "q", "options": ["a", "a", "b"], "answer": 0},   # repeated options: two right answers
    {"stem": "q", "options": ["a", "b", "c"], "answer": -1},                   # no key
    {"stem": "  ", "options": ["a", "b", "c"], "answer": 1},                   # no stem
    {"stem": "z=-1 表示？", "options": ["无意义", "低一个标准差", "低 1 分"], "answer": 1},  # reshuffled
])
def test_ungradable_or_unchanged_check_is_invalid_output_no_verifier(env, bad):
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    env[1].push(PURPOSE, bad)
    out = fb(env, card_id(pid, "s4", "check"), "wrong", "")
    assert not out.rewritten and out.reason == "invalid_output" and page_of(env, pid) == before
    no_verifier(env)


def test_unparseable_reply_is_llm_failed_and_nothing_changes(env):
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    env[1].push(PURPOSE, "不是 JSON")
    out = fb(env, card_id(pid, "s4", "check"), "unclear", "")
    assert out.reason == "llm_failed" and page_of(env, pid) == before


# --- ITEM_FLAWS / FOLLOWUP_NEW: item-writing rules and the one regeneration ----------------------

FLAWED = {
    "negative_stem": {"stem": "下面哪个不是 z = 2 的意思？",
                      "options": ["低于均值 2 个标准差", "高于均值 2 个标准差", "是 2%"], "answer": 0},
    "all_of_the_above": {"stem": "z = 2 表示这个值？",
                         "options": ["高于均值 2 个标准差", "高于均值 2 分", "以上都对"], "answer": 0},
    "absolute_word": {"stem": "z = 2 表示这个值？",
                      "options": ["高于均值 2 个标准差", "总是高于均值 2 分", "是 2%"], "answer": 0},
    "key_longest": {"stem": "z = 2 表示这个值？",
                    "options": ["它比这组数据的均值高出整整两个标准差那么远", "高 2 分", "是 2%"],
                    "answer": 0},
    "overlapping": {"stem": "z = 2 表示这个值？",
                    "options": ["高于均值", "高于均值 2 个标准差", "是 2%"], "answer": 1},
}


def old_check(env, pid):
    return page_of(env, pid).page.outline.sections[3].check


def test_check_prompt_carries_the_item_rules_and_the_no_reuse_rule(env):
    pid = organized_learned_page(env)
    env[1].push(PURPOSE, NEW_CHECK)
    fb(env, card_id(pid, "s4", "check"), "wrong", "")
    p = prompt_of(env)
    assert ITEM_RULES_ZH in p
    assert "先自己把题解一遍" in p and "学习者为什么会选它" in p
    assert "可以把原来的某个错误选项改成正确答案" in p           # 答案不对: the key may be fixed
    assert "新题里的错误选项不要沿用现在的错误选项" in p
    assert env[1].requests(PURPOSE)[0].params == {"attempt": 1}


def test_unclear_check_prompt_keeps_the_strict_no_reuse_rule(env):
    pid = organized_learned_page(env)
    env[1].push(PURPOSE, NEW_CHECK)
    fb(env, card_id(pid, "s4", "check"), "unclear", "")
    p = prompt_of(env)
    assert "题干和选项都要换，现在的错误选项不要再用" in p and "改成正确答案" not in p


def test_cue_prompt_carries_the_card_rules(env):
    pid = organized_learned_page(env)
    env[1].push(PURPOSE, {"cue": "z 告诉你离均值几个标准差", "answer": "一句答案"})
    fb(env, card_id(pid, "s2", "cue"), "unclear", "")
    p = prompt_of(env)
    for s in ("一张卡只问一件事", "只针对这一段里一个具体的点", "能想起的答案只有一个",
              "不要写「这一段讲的是什么」"):
        assert s in p, s
    assert ITEM_RULES_ZH not in p


def test_cue_prompt_does_not_ask_for_a_summary_outside_the_prohibition(env):
    """card-rewrite@v6: the injected review-card standard no longer says a cue recalls
    「这一段讲的是什么」 (CUE_RULES forbids exactly that); the phrase is left only in the rule
    that forbids it, and the standard asks for one specific point instead."""
    from evowiki.wikiapp.review_cards import CUE_CLAUSE, CUE_RULES
    pid = organized_learned_page(env)
    env[1].push(PURPOSE, {"cue": "z 告诉你离均值几个标准差", "answer": "一句答案"})
    fb(env, card_id(pid, "s2", "cue"), "unclear", "")
    p = strip_marks(prompt_of(env))
    assert strip_marks(CUE_RULES) in p and CUE_CLAUSE in p
    assert "讲的是什么" not in p.replace(strip_marks(CUE_RULES), "")
    assert env[1].requests(PURPOSE)[0].prompt_version == "card-rewrite@v9"


@pytest.mark.parametrize("kind", sorted(FLAWED))
def test_each_flaw_kind_is_caught(kind):
    out = FLAWED[kind]
    item = CheckItem(cell=CHECK_CELL, stem=out["stem"], options=tuple(out["options"]),
                     answer=out["answer"])
    assert item_flaws(item), kind


@pytest.mark.parametrize("kind", sorted(FLAWED))
def test_flawed_then_clean_check_uses_the_clean_one_two_calls(env, kind):
    pid = organized_learned_page(env)
    env[1].push(PURPOSE, FLAWED[kind], NEW_CHECK)
    out = fb(env, card_id(pid, "s4", "check"), "wrong", "")
    item = old_check(env, pid)
    assert out.rewritten and item.options[item.answer] == NEW_CHECK["options"][0]
    assert item_flaws(item) == ()
    reqs = env[1].requests(PURPOSE)
    assert len(reqs) == 2 and reqs[1].params == {"attempt": 2}
    assert "上一版的题有这些问题，这次必须改掉" in reqs[1].prompt
    no_verifier(env)


def test_flawed_twice_is_not_verified_and_the_old_card_stays(env):
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    env[1].push(PURPOSE, FLAWED["negative_stem"], FLAWED["all_of_the_above"])
    out = fb(env, card_id(pid, "s4", "check"), "wrong", "")
    assert not out.rewritten and out.reason == "not_verified" and page_of(env, pid) == before
    assert env[1].calls(PURPOSE) == 2


def test_near_duplicate_rewrite_is_refused(env):
    """A new stem but an old distractor reused, then the same stem with new options: both too
    close to the old check (FOLLOWUP_NEW) -> not_verified."""
    pid = organized_learned_page(env)
    old = old_check(env, pid)
    before = page_of(env, pid)
    distractor = old.options[(old.answer + 1) % len(old.options)]
    reuse = {"stem": "某人 z = 3，说明什么？", "options": ["高于均值 3 个标准差", distractor, "是 3%"],
             "answer": 0}
    same_stem = {"stem": old.stem, "options": ["高于均值 1 个标准差", "高 1 分", "是 1%"],
                 "answer": 0}
    for reply in (reuse, same_stem):
        r = CheckItem(cell=CHECK_CELL, stem=reply["stem"], options=tuple(reply["options"]),
                      answer=0)
        assert near_duplicate(r, old), reply
    env[1].push(PURPOSE, reuse, same_stem)
    out = fb(env, card_id(pid, "s4", "check"), "wrong", "")
    assert not out.rewritten and out.reason == "not_verified" and page_of(env, pid) == before
    assert "沿用了原题的错误选项" in env[1].requests(PURPOSE)[1].prompt


@pytest.mark.parametrize("cue", ["这一段讲的是什么？", "本节的主要内容", "z 分数这一节讲了什么"])
def test_summary_style_cue_is_refused(env, cue):
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    env[1].push(PURPOSE, {"cue": cue, "answer": "一句答案"})
    out = fb(env, card_id(pid, "s2", "cue"), "unclear", "")
    assert not out.rewritten and out.reason == "invalid_output" and page_of(env, pid) == before
    assert env[1].calls(PURPOSE) == 1


def _rekeyed(old, *, new_stem=None):
    """The old check with an old distractor made the key (optionally a new stem)."""
    j = (old.answer + 1) % len(old.options)
    return {"stem": new_stem or old.stem, "options": list(old.options), "answer": j}, old.options[j]


@pytest.mark.parametrize("new_stem", [None, "某人的 z = -1，这说明什么？"],
                         ids=["key_only", "new_stem"])
def test_wrong_key_fix_making_an_old_distractor_the_key_is_accepted(env, new_stem):
    """「答案不对」 and he is right: the corrected key is one of the old distractors (same stem and
    options, or a new stem) — a correction, not a near-duplicate; one call."""
    pid = organized_learned_page(env)
    reply, key = _rekeyed(old_check(env, pid), new_stem=new_stem)
    env[1].push(PURPOSE, reply)
    out = fb(env, card_id(pid, "s4", "check"), "wrong", "标准答案不对")
    item = old_check(env, pid)
    assert out.rewritten and item.options[item.answer] == key and env[1].calls(PURPOSE) == 1


def test_unclear_key_only_change_is_still_a_near_duplicate(env):
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    reply, _ = _rekeyed(old_check(env, pid))
    env[1].push(PURPOSE, reply, reply)
    out = fb(env, card_id(pid, "s4", "check"), "unclear", "")
    assert not out.rewritten and out.reason == "not_verified" and page_of(env, pid) == before
