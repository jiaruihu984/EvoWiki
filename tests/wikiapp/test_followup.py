"""Wrong-answer follow-up: slip / learned schedule a verified new question and a K claim, the
confirmation resolves it; unclear records 「看不懂」 + a check_unclear signal and returns where to
review and a quote for the assistant. Scripted gateway, MemoryRepo, no network."""

from datetime import timedelta

import pytest

from evowiki.belief.aspects import Claim, Observe, ResolveClaim
from evowiki.goals.contracts import CheckItem
from evowiki.goals.diagnostic import IDK, _shuffled
from evowiki.wikiapp.events import PageOpened, SignalRecorded, WikiEvent
from evowiki.wikiapp.followup import CONFIRM_DELAY, PURPOSE, confirm_answer, followup
from evowiki.wikiapp.kchange import answer_check
from evowiki.wikiapp.llm import CountingGateway, LLMFailed, LLMUnavailable
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict, WikiInvalid
from evowiki.wikiapp.review import grade_review, review_today
from evowiki.wikiapp.state import replay
from evowiki.wikiapp.wiki_schemas import CheckFollowupIn, ReviewGradeIn
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized

CELL = "c1.interpret"
NEW_Q = {"stem": "z = 2 表示这个值？", "options": ["高于均值 2 个标准差", "高于均值 2 分", "是 2%"],
         "answer": 0}
LATER = NOW + CONFIRM_DELAY + timedelta(minutes=1)


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def item_of(env, pid, sid="s4") -> CheckItem:
    return next(s.check for s in env[0].load("u1").state.pages[pid].page.outline.sections
                if s.id == sid)


def wrong(env, pid, idk=False):
    item = item_of(env, pid)
    choice = IDK if idk else (item.answer + 1) % len(item.options)
    return answer_check(env[0], None, "u1", pid, "s4", choice, now=NOW, new_id=env[2])


def fu(env, pid, choice, gw="new", sid="s4", **kw):
    gw = CountingGateway(env[1].gateway(), "u1") if gw == "new" else gw
    return followup(env[0], gw, "u1", pid, sid, CheckFollowupIn(choice=choice, **kw), now=NOW,
                    new_id=env[2])


def script_question(env):
    """The confirmation question (no verification call: structural checks only)."""
    env[1].push(PURPOSE, NEW_Q)
    return _shuffled(CheckItem(cell=CELL, **NEW_Q))


def ab(env):
    c = env[0].load("u1").k._cells[CELL]
    return c.a, c.b


def signals(env):
    return [e.body.signal for e in env[0]._wiki["u1"] if isinstance(e.body, SignalRecorded)]


def test_answer_refs_are_returned(env):
    pid = organized(env)
    assert wrong(env, pid).answer_ref == f"check:{pid}:s4"


@pytest.mark.parametrize("kind", ["slip", "learned"])
def test_slip_and_learned_schedule_a_new_verified_question_and_a_claim(env, kind):
    pid = organized(env)
    wrong(env, pid)
    before = ab(env)
    q = script_question(env)
    out = fu(env, pid, kind)
    assert out.status == "pending" and out.confirm_due == NOW + CONFIRM_DELAY and out.llm_calls == 1
    assert out.cell == CELL and out.quote is None and out.review is None
    after = ab(env)
    if kind == "slip":
        assert after[1] < before[1] and after[0] <= before[0]          # part of the miss set aside
    else:
        assert after[0] == pytest.approx(before[0] + 0.3) and after[1] == before[1]
    claim = [e for e in env[0].aspect_log("u1") if isinstance(e, Claim)]
    assert [(c.claim, c.cell) for c in claim] == [(kind, CELL)]
    prompt = env[1].requests(PURPOSE)[0].prompt
    assert item_of(env, pid).stem in prompt and "新的单选题" in prompt
    # not due yet; due later, in today's review — the NEW question, never the original
    assert review_today(env[0], "u1", now=NOW).confirms == []
    [card] = review_today(env[0], "u1", now=LATER).confirms
    assert card.followup_id == out.followup_id and card.kind == kind
    assert card.check.stem == q.stem != item_of(env, pid).stem
    st = env[0].load("u1").state
    assert replay(env[0]._wiki["u1"]).to_json() == st.to_json()


@pytest.mark.parametrize("kind, correct, outcome", [
    ("slip", True, "confirmed"), ("slip", False, "refuted"),
    ("learned", True, "confirmed"), ("learned", False, "refuted"),
])
def test_the_confirmation_resolves_the_claim(env, kind, correct, outcome):
    pid = organized(env)
    wrong(env, pid)
    no_claim = ab(env)
    q = script_question(env)
    out = fu(env, pid, kind)
    choice = q.answer if correct else (q.answer + 1) % 3
    res = confirm_answer(env[0], None, "u1", out.followup_id, choice, now=LATER, new_id=env[2])
    assert (res.correct, res.outcome) == (correct, outcome) and res.correct_index == q.answer
    log = env[0].aspect_log("u1")
    obs, resolve = log[-3], log[-1]          # Observe, its note, then ResolveClaim
    assert isinstance(obs, Observe) and obs.observation.evidence_ref == f"confirm:{out.followup_id}"
    assert isinstance(resolve, ResolveClaim) and resolve.outcome == outcome
    reteach = kind == "learned" and outcome == "refuted"
    assert (res.reteach is not None) == reteach and (res.signal_id is not None) == reteach
    if reteach:                              # 看懂没学会: the teaching section gets a signal
        assert res.reteach.section_id != "s4" and signals(env)[-1].kind == "check_unclear"
        assert signals(env)[-1].section_id == res.reteach.section_id
    if outcome == "refuted" and kind == "learned":
        c = env[0].load("u1").k._cells[CELL]
        # the weak positive is gone: just the two misses (same kind / polarity / strength) remain
        assert c.a == pytest.approx(2 * no_claim[0]) and c.claims == {}
    assert review_today(env[0], "u1", now=LATER).confirms == []
    with pytest.raises(WikiConflict):
        confirm_answer(env[0], None, "u1", out.followup_id, 0, now=LATER, new_id=env[2])


def test_unclear_records_dont_signals_the_teaching_section_and_returns_targets(env):
    pid = organized(env)
    item = item_of(env, pid)
    wrong(env, pid)
    calls = env[1].calls()
    out = fu(env, pid, "unclear", gw=None)
    assert env[1].calls() == calls and out.status == "done" and out.confirm_due is None
    assert out.review is not None and out.review.page_id == pid and out.review.section_id != "s4"
    assert out.suggest is None
    assert item.stem in out.quote.text and item.options[item.answer] in out.quote.text
    assert "我选的" in out.quote.text
    [sig] = [s for s in signals(env) if s.kind == "check_unclear"]
    assert sig.signal_id == out.signal_id and sig.section_id == out.review.section_id
    last = [e for e in env[0].aspect_log("u1") if isinstance(e, Observe)][-1].observation
    assert (last.cell, last.polarity, last.kind) == (CELL, "-", "self")
    assert review_today(env[0], "u1", now=LATER).confirms == []


def test_unclear_after_idk_quotes_idk_as_the_pick(env):
    """Live 2026-11-04: the quote said 我选的：<the stem> after 不知道."""
    pid = organized(env)
    wrong(env, pid, idk=True)
    assert "我选的：不知道" in fu(env, pid, "unclear", gw=None).quote.text


def test_the_next_card_answered_while_the_question_is_written_loses_nothing(env):
    """The question takes seconds: he may answer the next card meanwhile, and the log moves."""
    repo, s, ids = env
    pid = organized(env)
    wrong(env, pid)
    script_question(env)
    calls = s.calls()
    commit = repo.commit
    moved = []

    def racing(snap, c):
        if not moved:
            moved.append(1)
            commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                           body=PageOpened(page_id=pid))]))
        return commit(snap, c)

    repo.commit = racing
    out = fu(env, pid, "learned")
    assert out.status == "pending" and s.calls() == calls + 1
    assert [f.followup_id for f in repo.load("u1").state.open_followups()] == [out.followup_id]


def test_a_review_card_answer_can_be_followed_up_too(env):
    pid = organized(env)
    item = item_of(env, pid)
    g = grade_review(env[0], None, "u1", ReviewGradeIn(
        cell=CELL, choice=(item.answer + 1) % len(item.options), page_id=pid, section_id="s4"),
        now=NOW, new_id=env[2])
    assert g.answer_ref.startswith(f"review:{pid}:s4:")
    out = fu(env, pid, "unclear", gw=None, answer_ref=g.answer_ref)
    assert out.cell == CELL


def test_fail_closed_paths_record_nothing(env):
    pid = organized(env)
    wrong(env, pid)
    seq = (len(env[0].aspect_log("u1")), len(env[0]._wiki["u1"]))
    with pytest.raises(LLMUnavailable):
        fu(env, pid, "slip", gw=None)
    same = {"stem": item_of(env, pid).stem, "options": ["x", "y", "z"], "answer": 0}
    env[1].push(PURPOSE, same, same)                     # asked once more with the reasons
    with pytest.raises(LLMFailed):                       # the original question again, twice
        fu(env, pid, "slip")
    assert env[1].calls(PURPOSE) == 2
    assert (len(env[0].aspect_log("u1")), len(env[0]._wiki["u1"])) == seq


def test_refusals(env):
    pid = organized(env)
    with pytest.raises(LookupError):                     # nothing answered yet
        fu(env, pid, "unclear", gw=None)
    item = item_of(env, pid)
    answer_check(env[0], None, "u1", pid, "s4", item.answer, now=NOW, new_id=env[2])
    with pytest.raises(WikiInvalid):                     # a right answer
        fu(env, pid, "unclear", gw=None)


def test_idk_cannot_be_a_slip_and_an_answer_is_followed_up_once(env):
    pid = organized(env)
    wrong(env, pid, idk=True)
    with pytest.raises(WikiInvalid):
        fu(env, pid, "slip", gw=None)
    fu(env, pid, "unclear", gw=None)
    with pytest.raises(WikiConflict):
        fu(env, pid, "unclear", gw=None)
