"""R1 Task 5: feedback signals — each source normalised into one `Signal` in the SAME commit as
the action (right kind / outcome / section / spec_version), page feedback and note edits, the
assistant selections derived at read time, and the `list_signals` read API (cursor, kinds)."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.goals.diagnostic import IDK
from evowiki.teach.contracts import PageFeedbackIn, Signal
from evowiki.wikiapp.events import SignalRecorded, UsageAdded
from evowiki.wikiapp.feedback import page_feedback_flow
from evowiki.wikiapp.hand_edit import hand_edit_flow
from evowiki.wikiapp.kchange import answer_check
from evowiki.wikiapp.page import set_fold
from evowiki.wikiapp.repo import WikiInvalid
from evowiki.wikiapp.review import grade_review
from evowiki.wikiapp.sectionlog import log_versions
from evowiki.wikiapp.shelf import collapse, expand
from evowiki.wikiapp.signals import (
    Cursor,
    all_signals,
    cursor_of,
    list_signals,
    parse_cursor,
    selection_signal,
)
from evowiki.wikiapp.teach import add_item_flow
from evowiki.wikiapp.wiki_schemas import HandEditIn, ReviewGradeIn, SpecItemIn
from tests.wikiapp.fake_writer import WRITE
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_kchange import know, rewrite
from tests.wikiapp.test_organize import shown, text
from tests.wikiapp.test_page import declare, open_, organized, page_on
from tests.wikiapp.test_review_flow import LATER, organized_learned_page
from tests.wikiapp.test_teach_generation import SpyRepo

OBSERVABLE = {"signal_id", "kind", "at", "page_id", "section_id", "chips", "text", "outcome",
              "spec_version", "infer_version", "target_artifact_id", "evidence_type"}


@pytest.fixture
def env():
    return SpyRepo(["u1"]), Script(), Ids()


def kw(env):
    return {"now": NOW, "new_id": env[2]}


def last_commit_bodies(env):
    return [e.body for e in env[0].commits[-1].wiki]


def signals_of(bodies) -> list[Signal]:
    return [b.signal for b in bodies if isinstance(b, SignalRecorded)]


def one_signal(env) -> Signal:
    """The single signal of the last commit (it went in with the action)."""
    [sig] = signals_of(last_commit_bodies(env))
    return sig


def stored(env) -> list[Signal]:
    return signals_of(e.body for e in env[0].wiki_log("u1"))


# ---- flows: one signal in the action's commit ----

def test_section_know_signal_in_the_feedback_commit(env):
    pid = organized(env)
    know(env, pid, "s3")
    bodies = last_commit_bodies(env)
    assert {b.type for b in bodies} >= {"page_saved", "usage_added", "signal_recorded"}
    sig = one_signal(env)
    assert (sig.kind, sig.page_id, sig.section_id, sig.outcome, sig.text) == (
        "section_know", pid, "s3", None, "")
    assert (sig.spec_version, sig.infer_version, sig.at) == (1, 1, NOW)
    assert env[0].load("u1").state.signals_recorded == 1


@pytest.mark.parametrize(("kind", "signal"), [("other", "section_other"),
                                             ("deeper", "section_deeper")])
def test_rewrite_signal_goes_with_the_rewrite(env, kind, signal):
    _, s, _ = env
    pid = organized(env)
    s.push(WRITE, text("s2", "换个角度讲 z"))
    items, _ = rewrite(env, pid, "s2", kind)
    assert items[-1][0] == "done"
    commit = next(c for c in reversed(env[0].commits)
                  if any(b.body.type == "page_saved" for b in c.wiki))
    [sig] = signals_of(e.body for e in commit.wiki)
    assert (sig.kind, sig.page_id, sig.section_id, sig.spec_version) == (signal, pid, "s2", 1)


def test_failed_rewrite_still_records_the_click_alone(env):
    _, s, _ = env
    pid = organized(env)
    s.push(WRITE, "not json")
    items, _ = rewrite(env, pid, "s2", "deeper")
    assert items[-1][0] == "error"
    assert [b.type for b in last_commit_bodies(env)] == ["signal_recorded"]
    assert one_signal(env).kind == "section_deeper"


@pytest.mark.parametrize(("choice", "outcome", "label"), [
    (shown("低一个标准差"), 1.0, "低一个标准差"), (shown("低 1 分"), 0.0, "低 1 分"),
    (IDK, 0.0, "不知道")])
def test_check_answer_outcomes(env, choice, outcome, label):
    repo, _, ids = env
    pid = organized(env)
    answer_check(repo, None, "u1", pid, "s4", choice, now=NOW, new_id=ids)
    sig = one_signal(env)
    assert (sig.kind, sig.section_id, sig.outcome, sig.text, sig.spec_version) == (
        "check_answer", "s4", outcome, label, 1)


@pytest.mark.parametrize(("grade", "outcome", "label"), [
    ("recalled", 1.0, "想起来了"), ("fuzzy", 0.5, "有点模糊"), ("forgot", 0.0, "没想起来")])
def test_review_grade_self_grades(env, grade, outcome, label):
    repo, _, ids = env
    pid = organized_learned_page(env)
    grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade=grade, page_id=pid,
                                                 section_id="s2"), now=LATER, new_id=ids)
    sig = one_signal(env)
    assert (sig.kind, sig.page_id, sig.section_id, sig.outcome, sig.text, sig.at) == (
        "review_grade", pid, "s2", outcome, label, LATER)
    assert sig.spec_version == 1


@pytest.mark.parametrize(("choice", "outcome", "label"), [
    (shown("低一个标准差"), 1.0, "低一个标准差"), (shown("低 1 分"), 0.0, "低 1 分"),
    (IDK, 0.0, "不知道")])
def test_review_grade_check_mode_is_correctness(env, choice, outcome, label):
    repo, _, ids = env
    pid = organized_learned_page(env)
    grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", choice=choice, page_id=pid,
                                                 section_id="s4"), now=LATER, new_id=ids)
    sig = one_signal(env)
    assert (sig.kind, sig.section_id, sig.outcome, sig.text) == ("review_grade", "s4", outcome,
                                                                 label)


def test_review_grade_without_page_has_no_versions(env):
    repo, _, ids = env
    organized_learned_page(env)
    grade_review(repo, None, "u1", ReviewGradeIn(cell="c1.interpret", grade="fuzzy"),
                 now=LATER, new_id=ids)
    sig = one_signal(env)
    assert (sig.page_id, sig.section_id, sig.spec_version, sig.infer_version) == (
        None, None, None, None)


def test_unfold_by_hand_is_expand_with_section_fold_is_not(env):
    repo, _, ids = env
    pid = organized(env)
    set_fold(repo, "u1", pid, "s3", True, now=NOW, new_id=ids)
    assert signals_of(last_commit_bodies(env)) == []
    set_fold(repo, "u1", pid, "s3", False, now=NOW, new_id=ids)
    sig = one_signal(env)
    assert (sig.kind, sig.page_id, sig.section_id, sig.spec_version) == ("expand", pid, "s3", 1)
    n = len(env[0].commits)
    set_fold(repo, "u1", pid, "s3", False, now=NOW, new_id=ids)      # no change: no event
    assert len(env[0].commits) == n


def test_restore_full_page_is_expand_without_section(env):
    repo, _, ids = env
    pid = organized(env)
    declare(env, "know", "c1.procedure", "c1.interpret")       # its targets 确定会: shelvable
    open_(env, pid)
    collapse(repo, "u1", pid, now=NOW, new_id=ids)
    expand(repo, "u1", pid, now=NOW, new_id=ids)
    sig = one_signal(env)
    assert (sig.kind, sig.page_id, sig.section_id, sig.spec_version) == ("expand", pid, None, 1)


def test_spec_version_follows_the_section_log(env):
    _, s, _ = env
    repo, ids = env[0], env[2]
    pid = organized(env)                                  # logs at v1
    add_item_flow(repo, "u1", SpecItemIn(text="每段先给结论"), **kw(env))     # spec v2
    know(env, pid, "s3")
    assert one_signal(env).spec_version == 1              # the page the user saw was made at v1
    s.push(WRITE, text("s2", "换个角度讲 z"))
    items, _ = rewrite(env, pid, "s2", "other")           # s2 now logged at v2
    assert items[-1][0] == "done", items
    know(env, pid, "s2")
    assert one_signal(env).spec_version == 2
    page_feedback_flow(repo, "u1", pid, PageFeedbackIn(text="还行"), now=NOW, new_id=ids)
    assert (one_signal(env).spec_version, one_signal(env).infer_version) == log_versions(
        repo.load("u1").state, pid)                        # page level: the page's latest log


# ---- page feedback / note edit ----

def test_page_feedback_records_signal_and_usage(env):
    repo, _, ids = env
    pid = organized(env)
    out = page_feedback_flow(repo, "u1", pid, PageFeedbackIn(
        chips=("too_long", "no_example"), text="  第二段看不懂 ", section_id="s2"),
        now=NOW, new_id=ids)
    bodies = last_commit_bodies(env)
    [sig] = signals_of(bodies)
    assert out.signal_id == sig.signal_id
    assert (sig.kind, sig.page_id, sig.section_id, sig.chips, sig.text, sig.outcome) == (
        "page_feedback", pid, "s2", ("too_long", "no_example"), "第二段看不懂", None)
    [usage] = [b.record for b in bodies if isinstance(b, UsageAdded)]
    assert (usage.action, usage.page, usage.heading, usage.text) == (
        "整页反馈", pid, "z 分数怎么读", "太长、例子不够：第二段看不懂")
    page_feedback_flow(repo, "u1", pid, PageFeedbackIn(chips=("too_hard",)), **kw(env))
    assert env[0].load("u1").state.usage[-1].text == "太难"


def test_page_feedback_validation(env):
    repo, _, ids = env
    pid = organized(env)
    with pytest.raises(ValidationError):
        PageFeedbackIn(text="   ")                         # empty → 422 (contract validator)
    with pytest.raises(LookupError):
        page_feedback_flow(repo, "u1", "pg_nope", PageFeedbackIn(text="x"), **kw(env))
    with pytest.raises(WikiInvalid):
        page_feedback_flow(repo, "u1", pid, PageFeedbackIn(text="x", section_id="s9"),
                           **kw(env))
    assert stored(env) == []
    unorganized = page_on(repo, ids)
    with pytest.raises(WikiInvalid):
        page_feedback_flow(repo, "u1", unorganized, PageFeedbackIn(text="x", section_id="s1"),
                           **kw(env))
    out = page_feedback_flow(repo, "u1", unorganized, PageFeedbackIn(text="x"), **kw(env))
    assert stored(env)[-1].signal_id == out.signal_id and stored(env)[-1].spec_version is None


# ---- assistant selections (derived at read time) ----

def test_the_end_of_page_chips_reach_the_round_as_his_words_and_their_chip():
    from evowiki.assistant.session import Entry, SessionInfo
    from evowiki.teach.localize import classes_of
    from evowiki.wikiapp.state import WikiState
    at = datetime(2026, 11, 4, tzinfo=UTC)
    info = SessionInfo(session_id="s", user_id="u", page_id="pg", note_concept_id=None, created_at=at)
    entry = Entry(session_id="s", seq=1, entry_id="e", type="user", at=at, payload={
        "text": "这页内容太简单", "selection": {"text": "引用「节点与边」", "heading": "节点与边"}})
    sig = selection_signal(WikiState(), info, entry)
    assert (sig.text, sig.chips, sig.spec_version) == ("这页内容太简单", ("too_shallow",), None)
    assert classes_of(sig) == [("too_shallow", True)]
    # the plugin's current send has no selection at all
    bare = entry.model_copy(update={"payload": {"text": "这页理解有困难", "selection": None}})
    sig = selection_signal(WikiState(), info, bare)
    assert (sig.page_id, sig.text, sig.chips) == ("pg", "这页理解有困难", ("too_hard",))
    assert selection_signal(WikiState(), info, entry.model_copy(update={
        "payload": {"text": "别的话", "selection": None}})) is None


def sessions_with_selections(pid: str) -> MemorySessionStore:
    st = MemorySessionStore()
    st.create(SessionInfo(session_id="as1", user_id="u1", page_id=pid, note_concept_id=None,
                          created_at=NOW))
    st.create(SessionInfo(session_id="as2", user_id="u2", page_id=pid, note_concept_id=None,
                          created_at=NOW))
    st.create(SessionInfo(session_id="as3", user_id="u1", page_id=None, note_concept_id="c1",
                          created_at=NOW))
    t = lambda m: NOW + timedelta(minutes=m)
    st.append("as1", "e1", "user", {"message_id": "m1", "text": "这句什么意思",
                                    "selection": {"text": "低一个标准差", "heading": " z 分数怎么读 "}},
              t(1))
    st.append("as1", "e2", "assistant", {"message_id": "a1", "text": "答", "sources": [],
                                         "proposals": [], "tool_calls": 0, "llm_calls": 1}, t(2))
    st.append("as1", "e3", "user", {"message_id": "m2", "text": "没选", "selection": None}, t(3))
    st.append("as1", "e4", "user", {"message_id": "m3", "text": "问",
                                    "selection": {"text": "x" * 3000, "heading": "不存在的标题"}},
              t(4))
    st.append("as2", "e5", "user", {"message_id": "m4", "text": "别人的",
                                    "selection": {"text": "他人", "heading": None}}, t(5))
    st.append("as3", "e6", "user", {"message_id": "m5", "text": "笔记里",
                                    "selection": {"text": "笔记选中", "heading": "标题"}}, t(6))
    return st


def test_assistant_selections_are_derived_not_stored(env):
    repo = env[0]
    pid = organized(env)
    store = sessions_with_selections(pid)
    sigs = [s for s in all_signals(repo, "u1", sessions=store)
            if s.kind == "assistant_selection"]
    assert [s.signal_id for s in sigs] == ["asel:e1", "asel:e4", "asel:e6"]
    a, b, c = sigs
    assert (a.page_id, a.section_id, a.text, a.spec_version, a.infer_version) == (
        pid, "s2", "低一个标准差", None, None)
    assert a.at == NOW + timedelta(minutes=1)
    assert (b.section_id, len(b.text), b.spec_version) == (None, 2000, None)   # page's latest log
    assert (c.page_id, c.section_id, c.spec_version) == (None, None, None)
    assert stored(env) == []                                # nothing written
    info = store.info("as1")
    entry = store.entries("as1")[2]
    assert selection_signal(repo.load("u1").state, info, entry) is None


# ---- read API ----

def note_edit(env, pid, summary, now):
    hand_edit_flow(env[0], "u1", pid, HandEditIn(
        sections=[{"section_id": "s3", "body": summary, "note_body": summary}], summary=summary),
        now=now, new_id=env[2])


def three_signals(env) -> str:
    repo, _, ids = env
    pid = organized(env)
    know(env, pid, "s3")
    page_feedback_flow(repo, "u1", pid, PageFeedbackIn(text="a"),
                       now=NOW + timedelta(seconds=1), new_id=ids)
    note_edit(env, pid, "b", NOW + timedelta(seconds=2))
    return pid


URL_SAFE = re.compile(r"[A-Za-z0-9._-]+")


def drain(repo, store, *, after=None, limit=4, kinds=None) -> tuple[list[Signal], str]:
    """Every page from `after` on; returns the items and the final cursor."""
    seen = []
    while True:
        page = list_signals(repo, "u1", sessions=store, after=after, limit=limit, kinds=kinds)
        assert URL_SAFE.fullmatch(page.cursor)
        assert page.next in (None, page.cursor)
        seen += page.items
        after = page.cursor
        if page.next is None:
            return seen, after


def test_list_signals_cursor_paging_and_kinds(env):
    repo = env[0]
    pid = three_signals(env)
    store = sessions_with_selections(pid)
    full = list_signals(repo, "u1", sessions=store)
    assert [s.kind for s in full.items] == [
        "section_know", "page_feedback", "note_edit",
        "assistant_selection", "assistant_selection", "assistant_selection"]
    assert full.next is None and full.cursor
    keys = [(s.at, s.signal_id) for s in full.items]
    assert keys == sorted(keys)
    seen, _ = drain(repo, store, limit=4)
    assert sorted(seen, key=lambda s: (s.at, s.signal_id)) == full.items
    assert len({s.signal_id for s in seen}) == len(seen)          # nothing twice
    two = list_signals(repo, "u1", sessions=store, limit=3)
    assert len(two.items) == 3 and two.next is not None
    rest = list_signals(repo, "u1", sessions=store, after=two.next, limit=3)
    assert len(rest.items) == 3 and rest.next is None          # exactly the end: no next
    end = list_signals(repo, "u1", sessions=store, after=rest.cursor)
    assert (end.items, end.next, end.cursor) == ([], None, rest.cursor)   # nothing new: stays
    only = list_signals(repo, "u1", sessions=store, kinds="note_edit, page_feedback")
    assert [s.kind for s in only.items] == ["page_feedback", "note_edit"]
    no_asel = list_signals(repo, "u1", sessions=store, kinds=["section_know"])
    assert [s.kind for s in no_asel.items] == ["section_know"]
    assert len(list_signals(repo, "u1").items) == 3              # no session store: stored only
    for bad in ({"after": "garbage"}, {"after": "2026-01-01T00:00:00+00:00|sig_1"},
                {"after": "w1"}, {"after": "w-1.a0"}, {"after": "w1.a"}, {"after": "wx.a0"},
                {"after": "w1.a0.x"}, {"after": "w1.a1-2"},     # not a memory-store position
                {"kinds": "nope"}, {"limit": 0}, {"limit": 501}):
        with pytest.raises(WikiInvalid):
            list_signals(repo, "u1", sessions=store, **bad)
    with pytest.raises(LookupError):
        list_signals(repo, "nobody")


def test_cursor_round_trip():
    assert parse_cursor("w12.a0") == Cursor(12, "0")
    assert cursor_of(parse_cursor("w3.a1700000000000000-6531")) == "w3.a1700000000000000-6531"


def test_a_late_committed_signal_with_an_older_at_is_still_returned(env):
    """I1: a flow stamps `at` at its start but commits later. A reader that already holds a
    cursor past newer signals still gets it on the next call — stored and assistant alike."""
    repo, _, ids = env
    pid = three_signals(env)                       # at NOW, NOW+1s, NOW+2s
    store = sessions_with_selections(pid)          # selections at NOW+1..6min
    first, cur = drain(repo, store, limit=2)
    assert len(first) == 6
    # committed now, stamped before everything the reader has seen
    page_feedback_flow(repo, "u1", pid, PageFeedbackIn(text="晚到的"),
                       now=NOW - timedelta(hours=1), new_id=ids)
    store.append("as1", "e9", "user", {"message_id": "m9", "text": "晚到",
                                       "selection": {"text": "晚到的选中", "heading": None}},
                 NOW - timedelta(hours=2))
    late = list_signals(repo, "u1", sessions=store, after=cur)
    assert [(s.kind, s.text) for s in late.items] == [
        ("assistant_selection", "晚到的选中"), ("page_feedback", "晚到的")]   # by (at, id)
    assert late.next is None and late.cursor != cur
    again = list_signals(repo, "u1", sessions=store, after=late.cursor)
    assert again.items == []                       # each signal once


def test_kinds_filter_pages_without_breaking_the_cursor(env):
    repo, _, _ = env
    pid = three_signals(env)
    for i in range(3):
        note_edit(env, pid, f"n{i}", NOW + timedelta(seconds=10 + i))
    store = sessions_with_selections(pid)
    seen, cur = drain(repo, store, limit=2, kinds="note_edit")
    assert [s.text for s in seen] == ["b", "n0", "n1", "n2"]
    know(env, pid, "s2")                           # another kind: skipped
    note_edit(env, pid, "old", NOW - timedelta(days=1))
    more = list_signals(repo, "u1", sessions=store, after=cur, kinds="note_edit")
    assert [s.text for s in more.items] == ["old"] and more.next is None
    asel, _ = drain(repo, store, limit=1, kinds="assistant_selection")
    assert [s.signal_id for s in asel] == ["asel:e1", "asel:e4", "asel:e6"]


def test_signals_carry_only_observable_data(env):
    """Only ids, the user's chips / words / choice, the outcome and the versions in force —
    no cells, bands, beliefs or other K internals."""
    repo, _, ids = env
    pid = three_signals(env)
    answer_check(repo, None, "u1", pid, "s4", 0, now=NOW, new_id=ids)
    set_fold(repo, "u1", pid, "s3", False, now=NOW, new_id=ids)
    assert set(Signal.model_fields) == OBSERVABLE
    sigs = list_signals(repo, "u1", sessions=sessions_with_selections(pid)).items
    assert len(sigs) == 8
    for sig in sigs:
        dumped = sig.model_dump(mode="json")
        assert set(dumped) == OBSERVABLE
        flat = " ".join(str(v) for v in dumped.values())
        assert "c1." not in flat and "c2." not in flat              # no K cells
