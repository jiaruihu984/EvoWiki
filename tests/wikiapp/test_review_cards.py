"""B4: feedback on a review card - too_easy / drop hide it for good (replayed), unclear / wrong
rewrite the cue or the check (the check only when the independent verifier confirms a single
right answer, else fail closed), every kind records a card_feedback signal."""

import pytest

from evowiki.wikiapp.events import SignalRecorded
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.review import card_id, review_today
from evowiki.wikiapp.review_cards import KIND_LABEL, PURPOSE, card_feedback
from evowiki.wikiapp.state import replay
from evowiki.wikiapp.wiki_schemas import CardFeedbackIn
from tests.wikiapp.partial_write_check import assert_partial_write
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_review_flow import LATER, organized_learned_page

NEW_CHECK = {"stem": "z = 2 表示这个值？", "options": ["高于均值 2 个标准差", "高于均值 2 分", "是 2%"],
             "answer": 0}


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def fb(env, cid, kind, text="", gw="new"):
    _, s, ids = env
    gw = CountingGateway(s.gateway()) if gw == "new" else gw
    return card_feedback(env[0], gw, "u1", cid, CardFeedbackIn(kind=kind, text=text), now=LATER,
                         new_id=ids)


def signals(env):
    return [e.body.signal for e in env[0]._wiki["u1"] if isinstance(e.body, SignalRecorded)]


def page_of(env, pid):
    return env[0].load("u1").state.pages[pid]


def test_today_items_carry_a_stable_card_id(env):
    pid = organized_learned_page(env)
    items = review_today(env[0], "u1", now=LATER).items
    assert items and {i.card_id for i in items} == {card_id(pid, "s2", "cue")}
    assert card_id(pid, "s2", "cue") != card_id(pid, "s2", "check")
    assert card_id(pid, "s2", "cue").replace("_", "").isalnum()


@pytest.mark.parametrize("kind", ["too_easy", "drop"])
def test_hidden_card_disappears_and_stays_hidden_after_replay(env, kind):
    repo, s, _ = env
    pid = organized_learned_page(env)
    calls = s.calls()
    cid = card_id(pid, "s2", "cue")
    out = fb(env, cid, kind, "没意思", gw=None)
    assert out.hidden and not out.rewritten and out.card is None and out.reason is None
    assert s.calls() == calls                             # 0 LLM calls
    today = review_today(repo, "u1", now=LATER)
    assert cid not in [i.card_id for i in today.items]
    # replaying the stored log gives the same state; the card is still skipped
    assert cid in replay(repo._wiki["u1"]).hidden_cards
    assert replay(repo._wiki["u1"]).to_json() == repo.load("u1").state.to_json()
    # the cell falls back to the verified check card of s4 (the cue is no longer a source)
    assert {i.card_id for i in today.items} == {card_id(pid, "s4", "check")}
    assert today.items[0].mode == "check"
    with pytest.raises(LookupError):                      # hidden = unknown
        fb(env, cid, kind, gw=None)


@pytest.mark.parametrize("kind", ["unclear", "wrong", "too_easy", "drop"])
def test_every_kind_records_a_card_feedback_signal(env, kind):
    pid = organized_learned_page(env)
    cid = card_id(pid, "s2", "cue")
    if kind in ("unclear", "wrong"):
        env[1].push(PURPOSE, {"cue": "新的提示", "answer": "一句答案"})
    out = fb(env, cid, kind, "我的话")
    sig = signals(env)[-1]
    assert (sig.kind, sig.page_id, sig.section_id) == ("card_feedback", pid, "s2")
    assert sig.text == f"{KIND_LABEL[kind]}：我的话" and sig.signal_id == out.signal_id
    assert out.kind == kind


def test_signal_without_words_is_just_the_label(env):
    pid = organized_learned_page(env)
    fb(env, card_id(pid, "s2", "cue"), "unclear", "", gw=None)
    assert signals(env)[-1].text == "题目没说清楚"


def test_rewrite_cue_updates_the_page_and_today(env):
    repo, s, _ = env
    pid = organized_learned_page(env)
    cid = card_id(pid, "s2", "cue")
    old = page_of(env, pid).page.texts["s2"]
    s.push(PURPOSE, {"cue": "回想：z 告诉你离均值几个标准差", "answer": "一句答案"})
    out = fb(env, cid, "unclear", "看不懂在问什么")
    assert out.rewritten and not out.hidden and out.reason is None
    assert out.card is not None and out.card.card_id == cid
    assert out.card.cue == "z 告诉你离均值几个标准差" and out.card.mode == "cue"
    new = page_of(env, pid).page.texts["s2"]
    assert new.cue == "z 告诉你离均值几个标准差" and new.body == old.body
    assert new.cache_key == old.cache_key
    assert_partial_write(repo, pid, "card", {"s2"}, producer="writer")
    prompt = s.requests(PURPOSE)[0].prompt
    assert "看不懂在问什么" in prompt and "题目没说清楚" in prompt and old.body in prompt
    items = review_today(repo, "u1", now=LATER).items
    assert items[0].cue == "z 告诉你离均值几个标准差" and items[0].card_id == cid
    assert s.calls(PURPOSE) == 1 and s.calls("goals.diagnostic.verify") == 0


@pytest.mark.parametrize("bad", [{"cue": ""}, {"cue": "字" * 61, "answer": "一句答案"}, {"cue": "新的提示"}, {"nothing": 1}])
def test_unusable_cue_keeps_the_old_card(env, bad):
    s = env[1]
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    s.push(PURPOSE, bad)
    out = fb(env, card_id(pid, "s2", "cue"), "wrong")
    assert not out.rewritten and not out.hidden and out.reason == "invalid_output"
    assert out.card is None and signals(env)[-1].kind == "card_feedback"
    assert page_of(env, pid) == before


def test_unchanged_cue_is_unusable(env):
    pid = organized_learned_page(env)
    old = page_of(env, pid).page.texts["s2"]
    env[1].push(PURPOSE, {"cue": old.cue, "answer": old.answer})
    out = fb(env, card_id(pid, "s2", "cue"), "unclear")
    assert not out.rewritten and out.reason == "invalid_output"


def test_no_gateway_records_the_signal_and_changes_nothing(env):
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    out = fb(env, card_id(pid, "s2", "cue"), "wrong", "错了", gw=None)
    assert not out.rewritten and not out.hidden and out.reason == "llm_unavailable"
    assert signals(env)[-1].text == "答案不对：错了"
    assert page_of(env, pid) == before


def test_failed_llm_call_records_signal_only(env):
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    env[1].push(PURPOSE, "this is not json")
    out = fb(env, card_id(pid, "s2", "cue"), "unclear")
    assert not out.rewritten and out.reason == "llm_failed"
    assert signals(env)[-1].kind == "card_feedback"
    assert page_of(env, pid) == before


def test_rewrite_check_verified(env):
    s = env[1]
    pid = organized_learned_page(env)
    cid = card_id(pid, "s4", "check")
    s.push(PURPOSE, NEW_CHECK)
    out = fb(env, cid, "wrong", "标准答案不对")
    assert out.rewritten and out.reason is None
    assert out.card is not None and out.card.mode == "check" and out.card.card_id == cid
    assert out.card.check.stem == NEW_CHECK["stem"]
    assert sorted(out.card.check.options) == sorted(NEW_CHECK["options"])
    item = page_of(env, pid).page.outline.sections[3].check
    assert item.verified and item.stem == NEW_CHECK["stem"]
    assert item.options[item.answer] == NEW_CHECK["options"][0]
    assert s.roles(PURPOSE) == ["anthropic"] and s.calls("goals.diagnostic.verify") == 0
    assert "标准答案不对" in s.requests(PURPOSE)[0].prompt


@pytest.mark.parametrize("bad", [
    {"stem": "", "options": ["a", "b", "c"], "answer": 0},
    {"stem": "q", "options": ["a"], "answer": 0},
    {"stem": "q", "options": ["a", "b", "c"], "answer": 5},
    {"stem": "z=-1 表示？", "options": ["低一个标准差", "低 1 分", "无意义"], "answer": 0},   # same
])
def test_rewrite_check_bad_output(env, bad):
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    env[1].push(PURPOSE, bad)
    out = fb(env, card_id(pid, "s4", "check"), "unclear")
    assert not out.rewritten and out.reason == "invalid_output"
    assert page_of(env, pid) == before


def test_unknown_card_is_a_lookup_error(env):
    organized_learned_page(env)
    with pytest.raises(LookupError):
        fb(env, "rc_nope", "drop", gw=None)


def _only_sections(prompt, *keys):
    from evowiki.teach.standard import SECTION_KEYS, marker
    for k in SECTION_KEYS:
        assert (marker(k) in prompt) == (k in keys), k


def test_cue_rewrite_prompt_asks_for_recall_and_injects_review_card_and_spoken_first(env):
    from evowiki.wikiapp.llm import JSON_PROMPT_VERSION
    s = env[1]
    pid = organized_learned_page(env)
    s.push(PURPOSE, {"cue": "z 告诉你离均值几个标准差", "answer": "一句答案"})
    fb(env, card_id(pid, "s2", "cue"), "unclear", "看不懂")
    req = s.requests(PURPOSE)[0]
    _only_sections(req.prompt, "review_card", "spoken_first")
    assert "你能不能自己说出来" in req.prompt and "而不是认出一个术语" in req.prompt
    assert req.prompt_version == JSON_PROMPT_VERSION[PURPOSE] == "card-rewrite@v9"


def test_check_rewrite_prompt_is_recall_flavoured_and_injects_review_card_only(env):
    s = env[1]
    pid = organized_learned_page(env)
    s.push(PURPOSE, NEW_CHECK)
    fb(env, card_id(pid, "s4", "check"), "wrong", "答案不对")
    prompt = s.requests(PURPOSE)[0].prompt
    _only_sections(prompt, "review_card")
    assert "不要问“下列哪个是 X 的定义”" in prompt and "只有一个明确正确的选项" in prompt


# ---- kind None: the user's own words, no fixed label; the model decides ----

def test_own_words_rewrite_prompt_has_the_words_and_no_fixed_label(env):
    s = env[1]
    pid = organized_learned_page(env)
    cid = card_id(pid, "s2", "cue")
    s.push(PURPOSE, {"action": "rewrite", "cue": "z 告诉你离均值几个标准差", "answer": "一句答案"})
    out = fb(env, cid, None, "  应该问的是公式  ")
    assert out.rewritten and not out.hidden and out.kind is None and out.reason is None
    prompt = s.requests(PURPOSE)[0].prompt
    assert "他对这张卡说的话：应该问的是公式" in prompt
    assert not any(label in prompt for label in KIND_LABEL.values())
    assert "他的反馈：" not in prompt and '"action": "hide"' in prompt
    assert signals(env)[-1].text == "应该问的是公式"
    assert page_of(env, pid).page.texts["s2"].cue == "z 告诉你离均值几个标准差"


def test_own_words_check_prompt_has_the_words_and_no_fixed_label(env):
    s = env[1]
    pid = organized_learned_page(env)
    s.push(PURPOSE, NEW_CHECK)  # no action = rewrite
    out = fb(env, card_id(pid, "s4", "check"), None, "选项太绕了")
    assert out.rewritten and out.kind is None
    prompt = s.requests(PURPOSE)[0].prompt
    assert "他对这张卡说的话：选项太绕了" in prompt
    assert not any(label in prompt for label in KIND_LABEL.values())


@pytest.mark.parametrize("where", [("s2", "cue"), ("s4", "check")])
def test_own_words_model_may_hide_the_card(env, where):
    repo, s, _ = env
    pid = organized_learned_page(env)
    cid = card_id(pid, *where)
    s.push(PURPOSE, {"action": "hide"})
    out = fb(env, cid, None, "这张卡别再给我看了")
    assert out.hidden and not out.rewritten and out.reason is None and out.kind is None
    assert cid in replay(repo._wiki["u1"]).hidden_cards
    assert s.calls("goals.diagnostic.verify") == 0
    assert signals(env)[-1].text == "这张卡别再给我看了"


def test_own_words_unknown_action_fails_closed(env):
    pid = organized_learned_page(env)
    before = page_of(env, pid)
    env[1].push(PURPOSE, {"action": "delete_everything", "cue": "新的提示", "answer": "一句答案"})
    out = fb(env, card_id(pid, "s2", "cue"), None, "随便改改")
    assert not out.rewritten and not out.hidden and out.reason == "invalid_output"
    assert page_of(env, pid) == before
    assert card_id(pid, "s2", "cue") not in env[0].load("u1").state.hidden_cards


def test_old_kinds_ignore_an_action_field(env):
    pid = organized_learned_page(env)
    env[1].push(PURPOSE, {"action": "hide", "cue": "新的提示", "answer": "一句答案"})
    out = fb(env, card_id(pid, "s2", "cue"), "unclear", "看不懂")
    assert out.rewritten and not out.hidden
    prompt = env[1].requests(PURPOSE)[0].prompt
    assert "他的反馈：题目没说清楚。他的话：看不懂" in prompt and '"action"' not in prompt


def test_own_words_without_gateway_hides_nothing(env):
    pid = organized_learned_page(env)
    out = fb(env, card_id(pid, "s2", "cue"), None, "不想看了", gw=None)
    assert out.reason == "llm_unavailable" and not out.hidden and not out.rewritten
    assert signals(env)[-1].text == "不想看了"


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "  \n "}, {"kind": None},
                                  {"text": "字" * 1001}])
def test_own_words_are_required(body):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CardFeedbackIn.model_validate(body)
    assert CardFeedbackIn.model_validate({"text": "x"}).kind is None
    assert CardFeedbackIn.model_validate({"kind": "drop"}).text == ""
