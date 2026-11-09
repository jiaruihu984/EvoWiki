"""Wrong-answer reminders use real PostgreSQL and the existing suggestion lifecycle."""

from datetime import UTC, datetime, timedelta

import pytest

from evowiki.belief.aspects import Claim, DefineConcept, Observe
from evowiki.bot.facts import load_facts
from evowiki.bot.pipeline import dispatch, run_daily, scan_events
from evowiki.bot.schedule import Slot
from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.bot.suggestions import BotConflict, list_suggestions, react
from evowiki.goals.contracts import Aspect, Observation, OutlineSection, PageOutline, SectionRole
from evowiki.goals.fold import Page
from evowiki.store import bot as store
from evowiki.wikiapp.events import PageRecord, PageSaved, PagesTrashed, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 14, 9, tzinfo=UTC)
_n = iter(range(100_000))


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


@pytest.fixture
def repo(pool):
    make_user(pool, "u1")
    repo = PgRepo(pool)
    outline = PageOutline(
        page_id="p1", goal="g", concepts=("c1",), targets=("c1",),
        sections=(OutlineSection(id="s1", heading="Interpretation", role=SectionRole.EXPLAIN,
                                 cells=("c1.interpret",)),))
    page = PageRecord(page_id="p1", title="Interpretation", goal="g", concepts=("c1",),
                      targets=("c1",), page=Page(outline=outline, states={}), created_at=NOW)
    repo.commit(repo.load("u1"), Commit(
        aspect=[DefineConcept(event_id=new_id("ae"), ts=NOW, concept_id="c1",
                              aspects=(Aspect.INTERPRET,))],
        wiki=[WikiEvent(event_id=new_id("we"), ts=NOW,
                        body=PageSaved(record=page, action="created"))]))
    return repo


def answer(repo, question, *, at=NOW, pol="-", cell="c1.interpret", kind="check"):
    ev = Observe(event_id=new_id("ae"), ts=at, observation=Observation(
        cell=cell, polarity=pol, strength=0.8, kind=kind, evidence_ref=question))
    repo.commit(repo.load("u1"), Commit(aspect=[ev]))
    return ev


def seed(repo, n=3, *, at=NOW):
    return [answer(repo, f"check:p1:s{i}", at=at - timedelta(minutes=n - i)) for i in range(n)]


def scan(pool, now=NOW):
    return scan_events(pool, "u1", now=now, new_id=new_id)


def dispatch_one(pool, tid, now=NOW):
    return dispatch(pool, tid, now=now, new_id=new_id)


def listed(pool, now=NOW):
    return list_suggestions(pool, "u1", placement="today", page_id=None, now=now)


def settings(pool, **kw):
    with pool.connection() as conn:
        store.put_settings(conn, "u1", DEFAULT_SETTINGS.model_copy(update=kw), NOW)


def test_threshold_distinct_questions_and_existing_assistant_action(pool, repo):
    seed(repo, 2)
    answer(repo, "check:p1:s0")  # A repeated click is still one question.
    answer(repo, "check:p1:right", pol="+")
    answer(repo, "check:p1:old", at=NOW - timedelta(days=8))
    answer(repo, "check:p1:feedback", kind="feedback")
    assert scan(pool) == []
    third = answer(repo, "check:p1:s2")
    [tid] = scan(pool)
    before = repo.load("u1").aspect_seq
    assert dispatch_one(pool, tid).outcome == "pass"
    [card] = listed(pool)
    assert card.kind == "wrong_answers" and card.page_id == "p1"
    assert card.title.startswith("补讲：") and "3 道不同的题" in card.why
    assert card.action["type"] == "open_page" and card.action["page_id"] == "p1"
    assert "换个讲法" in card.action["ask"] and "再出一道题" in card.action["ask"]
    with pool.connection() as conn:
        trigger = store.get_trigger(conn, tid)
        assert trigger.route == "direct" and trigger.dedup_key == "cell:c1.interpret"
        assert third.event_id in trigger.evidence and len(trigger.evidence) == 3
    assert react(pool, "u1", card.suggestion_id, "accepted", now=NOW).status == "accepted"
    assert react(pool, "u1", card.suggestion_id, "done", now=NOW).status == "done"
    assert repo.load("u1").aspect_seq == before  # A reminder never writes mastery.
    assert scan(pool) == []
    answer(repo, "check:p1:s2", at=NOW + timedelta(minutes=1))
    assert scan(pool, NOW + timedelta(minutes=1)) == []


def test_configured_window_and_count_are_used(pool, repo):
    settings(pool, wrong_days=2, wrong_count=2)
    answer(repo, "check:p1:old", at=NOW - timedelta(days=2, seconds=1))
    answer(repo, "check:p1:first", at=NOW - timedelta(days=2))
    assert scan(pool) == []
    answer(repo, "check:p1:second")
    [tid] = scan(pool)
    assert dispatch_one(pool, tid).outcome == "pass"
    assert "2 天内" in listed(pool)[0].why


def test_slips_corrected_and_unknown_pages_do_not_count(pool, repo):
    seed(repo, 2)
    slipped = answer(repo, "check:p1:slip")
    repo.commit(repo.load("u1"), Commit(aspect=[Claim(
        event_id=new_id("ae"), ts=NOW, claim="slip", cell="c1.interpret",
        source_event_id=slipped.event_id)]))
    answer(repo, "review:p1:corrected:try1", kind="recall", at=NOW - timedelta(minutes=2))
    answer(repo, "review:p1:corrected:try2", kind="recall", pol="+")
    answer(repo, "check:missing:s1")
    answer(repo, "review:-:-:try1", kind="recall")
    assert scan(pool) == []


def test_wrong_again_after_a_correct_answer_uses_the_new_evidence(pool, repo):
    seed(repo, 2)
    first = answer(repo, "check:p1:s2", at=NOW - timedelta(minutes=3))
    answer(repo, "check:p1:s2", at=NOW - timedelta(minutes=2), pol="+")
    assert scan(pool) == []
    latest = answer(repo, "check:p1:s2")
    [tid] = scan(pool)
    with pool.connection() as conn:
        trigger = store.get_trigger(conn, tid)
    assert latest.event_id in trigger.evidence and first.event_id not in trigger.evidence


@pytest.mark.parametrize("change", ["slip", "correct", "trash", "hidden", "expired"])
def test_queued_and_shown_cards_recheck_evidence(pool, repo, change):
    answers = seed(repo)
    [tid] = scan(pool)
    assert dispatch_one(pool, tid).outcome == "pass"
    [card] = listed(pool)
    now = NOW
    if change == "slip":
        repo.commit(repo.load("u1"), Commit(aspect=[Claim(
            event_id=new_id("ae"), ts=NOW, claim="slip", cell="c1.interpret",
            source_event_id=answers[0].event_id)]))
    elif change == "correct":
        answer(repo, "check:p1:s0", pol="+")
    elif change == "trash":
        repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
            event_id=new_id("we"), ts=NOW, body=PagesTrashed(page_ids=("p1",)))]))
    elif change == "hidden":
        with pool.connection() as conn:
            conn.execute("insert into memory_suppression_closure(user_id,owner,node) "
                         "values (%s,%s,%s)", ("u1", "synthetic-forgotten-source", "page:p1"))
    else:
        now += timedelta(days=8)
    with pytest.raises(BotConflict):
        react(pool, "u1", card.suggestion_id, "accepted", now=now)
    assert listed(pool, now) == []
    with pool.connection() as conn:
        assert store.lock_suggestion(conn, card.suggestion_id).status == "expired"


def test_queued_trigger_expires_before_dispatch(pool, repo):
    seed(repo)
    [tid] = scan(pool)
    result = dispatch_one(pool, tid, NOW + timedelta(days=8))
    assert (result.outcome, result.reason) == ("skip", "empty")
    assert listed(pool, NOW + timedelta(days=8)) == []


@pytest.mark.parametrize("switch,reason", [("pause", "paused"), ("kind", "kind_off"),
                                         ("cap", "cap_reached")])
def test_gate_and_next_daily_check_after_restart(pool, repo, switch, reason):
    seed(repo)
    if switch == "pause":
        settings(pool, paused="always")
    elif switch == "kind":
        settings(pool, kinds={**DEFAULT_SETTINGS.kinds, "wrong_answers": False})
    else:
        settings(pool, today_cap=0)
    [tid] = scan(pool)
    assert dispatch_one(pool, tid).reason == reason
    assert listed(pool) == []
    settings(pool)
    tomorrow = NOW + timedelta(days=1)
    slot = Slot(tomorrow.date(), tomorrow)
    [retry] = run_daily(pool, "u1", slot, now=tomorrow, new_id=new_id)
    assert dispatch_one(pool, retry, tomorrow).outcome == "pass"
    assert run_daily(pool, "u1", slot, now=tomorrow, new_id=new_id) == []
    assert len(listed(pool, tomorrow)) == 1


def test_cooldown_uses_real_event_time_and_survives_restart(pool, repo):
    seed(repo)
    [first] = scan(pool)
    assert dispatch_one(pool, first).outcome == "pass"
    later = NOW + timedelta(hours=15)  # Next day 08:00 local, only 15 hours since first card.
    answer(repo, "check:p1:s3", at=later)
    [next_trigger] = scan(pool, later)
    assert dispatch_one(pool, next_trigger, later).reason == "cooldown"
    tomorrow = NOW + timedelta(days=1)
    [daily] = run_daily(pool, "u1", Slot(tomorrow.date(), tomorrow),
                        now=tomorrow, new_id=new_id)
    assert dispatch_one(pool, daily, tomorrow).outcome == "pass"
    assert len(listed(pool, tomorrow)) == 1


def test_forgotten_question_reference_is_excluded_in_facts(pool, repo):
    seed(repo)
    with pool.connection() as conn:
        conn.execute("insert into memory_suppression_closure(user_id,owner,node) values (%s,%s,%s)",
                     ("u1", "synthetic-forgotten-source", "check:p1:s0"))
        facts = load_facts(conn, "u1", now=NOW, settings=DEFAULT_SETTINGS)
    assert len(facts.wrong_answers) == 2
    assert scan(pool) == []


def test_declining_remedial_uses_existing_preference_counters(pool, repo):
    seed(repo)
    [tid] = scan(pool)
    assert dispatch_one(pool, tid).outcome == "pass"
    [card] = listed(pool)
    react(pool, "u1", card.suggestion_id, "not_interested", now=NOW)
    with pool.connection() as conn:
        assert store.get_counters(conn, "u1", "cell:c1.interpret").not_interested == 1
    tomorrow = NOW + timedelta(days=1)
    [retry] = run_daily(pool, "u1", Slot(tomorrow.date(), tomorrow),
                        now=tomorrow, new_id=new_id)
    assert dispatch_one(pool, retry, tomorrow).reason == "suppressed"
    later = NOW + timedelta(days=2)
    [retry] = run_daily(pool, "u1", Slot(later.date(), later), now=later, new_id=new_id)
    assert dispatch_one(pool, retry, later).outcome == "pass"
