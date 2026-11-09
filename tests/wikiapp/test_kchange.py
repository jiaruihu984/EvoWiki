"""Task 9: the one K-change path (feedback, embedded checks, ask, declarations). Covers the
prototype's call-count table: know 0; deeper / other 1; a K change that leaves a conflict 0 (the
sections that can fix it wait for the page writer); checks 0; ask 1; declare 0 for the current
page and 0 for every other page."""

import pytest

from evowiki.belief.aspects import AddNote, Declare, DefineConcept, Observe
from evowiki.goals.contracts import Aspect, Band, Observation, SectionRole
from evowiki.goals.diagnostic import IDK
from evowiki.wikiapp.events import PageOpened, PageSaved, UsageAdded
from evowiki.wikiapp.kchange import (
    answer_check,
    declare_flow,
    feedback_know,
    feedback_rewrite_stream,
    k_changed,
)
from evowiki.wikiapp.kwrite import apply_in_memory
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.page import set_pin
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict, WikiInvalid
from evowiki.wikiapp.stale import patch_pending
from evowiki.wikiapp.views import page_out
from evowiki.wikiapp.wiki_schemas import (
    REWRITE_PAYLOADS,
    ChangeOut,
    CheckAnswerOut,
    DeclareOut,
    FeedbackOut,
    RewriteDoneEvent,
)
from tests.wikiapp.fake_writer import WRITE, writing
from tests.wikiapp.runs import runs_for
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_organize import (
    CHECK_KEY,
    add_page,
    shown,
    text,
)
from tests.wikiapp.test_page import LINE, commit_wiki, gw_of, miss, organized


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def band(env, cell):
    return env[0].load("u1").kview(NOW).cell(cell).band


def new_aspect(env, seq):
    return env[0].aspect_log("u1")[seq:]


def rec_of(env, pid):
    return env[0].load("u1").state.pages[pid]


def sec(rec, sid):
    return next(s for s in rec.page.outline.sections if s.id == sid)


def usage(env):
    return [e.body.record for e in env[0].wiki_log("u1") if isinstance(e.body, UsageAdded)]


def know(env, pid, sid, gw=None):
    repo, _, ids = env
    return feedback_know(repo, gw, "u1", pid, sid, now=NOW, new_id=ids)


def rewrite(env, pid, sid, kind, gw=None, kit=None):
    repo, _, ids = env
    gw = gw or gw_of(env)
    runs = runs_for(repo)
    return list(feedback_rewrite_stream(repo, gw, "u1", pid, sid, kind, now=NOW,
                                        new_id=ids, writing=kit or writing(env), runs=runs)), gw


def names(items):
    return [n for n, _ in items]


# ---- section feedback ----

def test_know_folds_and_locks_with_zero_calls_and_writes_feedback_observations(env):
    repo, s, _ = env
    pid = organized(env)
    seq = len(repo.aspect_log("u1"))
    calls = s.calls()
    out = know(env, pid, "s2", gw=gw_of(env))
    assert isinstance(out, FeedbackOut) and s.calls() == calls
    assert out.section.id == "s2" and out.section.folded and out.section.fold_locked
    assert out.change.folded == ["s2"] and out.change.llm_calls == 0
    assert out.change.rewritten == [] and out.change.other_pages_stale == []
    evs = new_aspect(env, seq)
    obs = [e.observation for e in evs if isinstance(e, Observe)]
    assert [(o.cell, o.polarity, o.strength, o.kind) for o in obs] == [
        ("c1.procedure", "+", 0.6, "feedback"), ("c1.interpret", "+", 0.6, "feedback")]
    notes = [e for e in evs if isinstance(e, AddNote)]
    assert [n.text for n in notes] == ["z 分数怎么读"] * 2          # raw: the heading (D21)
    assert all(n.evidence_ref.startswith("feedback:") for n in notes)
    rec = rec_of(env, pid)
    st = rec.page.state("s2")
    assert st.folded and st.fold_locked and rec.changed == {"s2": "folded"}
    assert rec.page.snapshot["c1.interpret"] is band(env, "c1.interpret")
    assert [(u.action, u.page, u.heading) for u in usage(env)] == [("这部分我懂", pid, "z 分数怎么读")]


def test_know_works_without_gateway(env):
    pid = organized(env)
    out = know(env, pid, "s3", gw=None)
    assert out.section.folded and out.change.folded == ["s3"] and out.change.llm_calls == 0


def test_deeper_rewrites_once_as_explain_and_lowers_k(env):
    repo, s, _ = env
    pid = organized(env)
    seq = len(repo.aspect_log("u1"))
    s.push(WRITE, text("s3", "标准差讲深一点"))
    kit = writing(env)
    items, gw = rewrite(env, pid, "s3", "deeper", kit=kit)
    assert names(items) == ["writer", "section_delta", "section_done", "change", "done"]
    for n, p in items:
        assert isinstance(p, REWRITE_PAYLOADS[n])
    assert items[1][1].section_id == "s3" and items[1][1].body == "标准差讲深一点"
    assert items[2][1].cached is False
    change = items[3][1]
    assert isinstance(change, ChangeOut)
    assert change.rewritten == ["s3"] and change.llm_calls == 1 and gw.calls == 0   # the writer
    assert items[4][1] == RewriteDoneEvent(page_id=pid, llm_calls=1,
                                           model="anthropic:claude-sonnet-5-5")
    [inp] = kit.writer.inputs                      # one section, by the page writer
    assert (inp.mode, inp.scope, inp.required) == ("feedback", ("s3",), ())
    assert "再讲深一点" in inp.instruction and "标准差提醒" in inp.instruction
    assert all(k in inp.instruction for k in ("步骤粒度", "讲解深度", "假设的已有知识"))
    assert sec_of_outline(inp.page.outline, "s3").role is SectionRole.EXPLAIN
    rec = rec_of(env, pid)
    assert sec(rec, "s3").role is SectionRole.EXPLAIN
    assert rec.page.texts["s3"].body == "标准差讲深一点" and rec.changed["s3"] == "rewritten"
    obs = [e.observation for e in new_aspect(env, seq) if isinstance(e, Observe)]
    assert [(o.cell, o.polarity, o.strength) for o in obs] == [("c2.term", "-", 0.4)]
    assert [u.action for u in usage(env)] == ["再讲深一点"]
    assert s.pending() == {}


def test_other_rewrites_once_and_leaves_k_alone(env):
    repo, s, _ = env
    pid = organized(env)
    seq = len(repo.aspect_log("u1"))
    s.push(WRITE, text("s2", "换个角度讲 z"))
    items, gw = rewrite(env, pid, "s2", "other")
    assert names(items) == ["writer", "section_delta", "section_done", "change", "done"]
    change = items[3][1]
    assert change.rewritten == ["s2"] and change.llm_calls == 1 and gw.calls == 0
    assert rec_of(env, pid).written.kind == "feedback"
    assert new_aspect(env, seq) == []
    rec = rec_of(env, pid)
    assert sec(rec, "s2").role is SectionRole.EXPLAIN and rec.page.texts["s2"].body == "换个角度讲 z"
    assert [u.action for u in usage(env)] == ["换个讲法"]


def test_deeper_with_conflict_elsewhere_leaves_that_section_alone(env):
    _repo, s, _ = env
    pid = organized(env, LINE)                     # s3 is a line on c2.term
    miss(env, "c2.term")                # K moved; the page was not brought up to date
    s.push(WRITE, text("s2", "z 讲深一点"))
    items, gw = rewrite(env, pid, "s2", "deeper")
    assert names(items) == ["writer", "section_delta", "section_done", "change", "done"]
    assert [p.section_id for n, p in items if n == "section_delta"] == ["s2"]
    change = items[3][1]
    assert change.rewritten == ["s2"] and change.inserted == []
    # the rewrite he asked for only: s3 keeps its text and nothing waits for the writer
    assert change.llm_calls == 1 and gw.calls == 0
    assert [c.kind for c in change.conflicts] == ["line_weak"]
    assert items[-1][1].llm_calls == 1
    rec = rec_of(env, pid)
    assert sec(rec, "s3").role is SectionRole.LINE and "s3" not in rec.changed
    assert s.pending() == {} and patch_pending(env[0].load("u1").state, pid) == ()


def test_deeper_without_text_errors_are_raised_before_stream(env):
    repo, _, ids = env
    pid = organized(env)
    gw = gw_of(env)
    with pytest.raises(WikiInvalid):
        feedback_rewrite_stream(repo, gw, "u1", pid, "s4", "deeper", now=NOW, new_id=ids,
                                writing=writing(env), runs=runs_for(repo))
    with pytest.raises(LookupError):
        feedback_rewrite_stream(repo, gw, "u1", pid, "s9", "other", now=NOW, new_id=ids,
                                writing=writing(env), runs=runs_for(repo))
    with pytest.raises(WikiInvalid):
        know(env, pid, "s4")                       # a check question has no feedback buttons


def test_pinned_section_feedback_is_invalid(env):
    repo, _, ids = env
    pid = organized(env)
    set_pin(repo, "u1", pid, "s2", True, now=NOW, new_id=ids)
    with pytest.raises(WikiInvalid):
        feedback_rewrite_stream(repo, gw_of(env), "u1", pid, "s2", "deeper", now=NOW, new_id=ids,
                                writing=writing(env), runs=runs_for(repo))
    with pytest.raises(WikiInvalid):
        know(env, pid, "s2")


def only_signal_since(env, seq, kind):
    """Nothing but the click's signal was written after wiki seq `seq` (R1 Task 5)."""
    new = [e.body for e in env[0].wiki_log("u1")[seq:]]
    assert [b.type for b in new] == ["signal_recorded"] and new[0].signal.kind == kind


def test_rewrite_llm_failure_is_error_event_and_commits_only_the_signal(env):
    repo, s, _ = env
    pid = organized(env)
    seq = repo.load("u1").wiki_seq
    s.push(WRITE, "not json")
    items, _ = rewrite(env, pid, "s2", "deeper")
    assert names(items) == ["writer", "error"] and items[-1][1].code == "llm_failed"
    only_signal_since(env, seq, "section_deeper")


# ---- embedded checks ----

def check(env, pid, choice, gw=None):
    repo, _, ids = env
    return answer_check(repo, gw, "u1", pid, "s4", choice, now=NOW, new_id=ids)


@pytest.mark.parametrize(("choice", "correct", "pol", "strength", "note"), [
    (shown("低一个标准差"), True, "+", 1.0, "低一个标准差"),
    (shown("低 1 分"), False, "-", 1.0, "低 1 分"),
    (IDK, None, "-", 0.8, "z=-1 表示？"),
])
def test_check_answers_write_observations_with_zero_calls(env, choice, correct, pol, strength,
                                                          note):
    repo, s, _ = env
    pid = organized(env)                           # organize verified s4's item (1 call)
    assert sec(rec_of(env, pid), "s4").check.verified
    seq = len(repo.aspect_log("u1"))
    calls = s.calls()
    out = check(env, pid, choice, gw=gw_of(env))
    assert isinstance(out, CheckAnswerOut)
    assert (out.correct, out.correct_index) == (correct, CHECK_KEY)
    assert out.change.llm_calls == 0 and s.calls() == calls
    evs = new_aspect(env, seq)
    obs = [e.observation for e in evs if isinstance(e, Observe)]
    assert [(o.cell, o.polarity, o.strength, o.kind) for o in obs] == [
        ("c1.interpret", pol, strength, "check")]
    notes = [e for e in evs if isinstance(e, AddNote)]
    assert [n.text for n in notes] == [note] and notes[0].evidence_ref.startswith("check:")
    item = sec(rec_of(env, pid), "s4").check
    picked = "不知道" if choice == IDK else item.options[choice]
    assert notes[0].label == f"题目：{item.stem}；选了「{picked}」"
    assert rec_of(env, pid).checks == {"s4": choice}
    with pytest.raises(WikiConflict):
        check(env, pid, 0)                                       # answered already


def test_old_stored_unverified_check_is_hidden_and_never_graded(env):
    """Pages stored before verification removed such sections: not shown, no key, no answer."""
    repo, _, _ = env
    pid = organized(env)
    rec = rec_of(env, pid)
    old = rec.page.outline.model_copy(update={"sections": tuple(
        x.model_copy(update={"check": x.check.model_copy(update={"verified": False})})
        if x.id == "s4" else x for x in rec.page.outline.sections)})
    commit_wiki(env, PageSaved(record=rec.model_copy(update={
        "page": rec.page.model_copy(update={"outline": old})}), action="test"))
    snap = repo.load("u1")
    out = page_out(snap.state, snap.kview(NOW), snap.state.pages[pid])
    assert [x.id for x in out.sections] == ["s1", "s2", "s3"]
    assert "correct" not in out.model_dump_json().replace("correct_index", "")
    seq = len(repo.aspect_log("u1"))
    with pytest.raises(WikiInvalid):
        check(env, pid, 0)
    assert new_aspect(env, seq) == [] and rec_of(env, pid).checks == {}


def test_check_errors(env):
    repo, _, ids = env
    pid = organized(env)
    with pytest.raises(WikiInvalid):
        check(env, pid, 3)                                       # 3 options: 0..2
    with pytest.raises(WikiInvalid):
        answer_check(repo, None, "u1", pid, "s2", 0, now=NOW, new_id=ids)   # not a check
    with pytest.raises(LookupError):
        answer_check(repo, None, "u1", pid, "s9", 0, now=NOW, new_id=ids)
    assert rec_of(env, pid).checks == {}


# ---- declarations ----

def two_pages(env):
    """pid (organized, current), pid2 (organized copy on the same concepts), pid3 (unorganized)."""
    repo, _, ids = env
    pid = organized(env)
    pid2 = add_page(repo, ids, title="z 分数（第二页）")
    pid3 = add_page(repo, ids, title="z 分数（未生成）")
    rec2 = rec_of(env, pid2).model_copy(update={"page": rec_of(env, pid).page.model_copy(
        update={"outline": rec_of(env, pid).page.outline.model_copy(update={"page_id": pid2})})})
    commit_wiki(env, PageSaved(record=rec2, action="test"), PageOpened(page_id=pid))
    return pid, pid2, pid3


def decl(env, cell, value, gw=None):
    repo, _, ids = env
    return declare_flow(repo, gw, "u1", cell, value, now=NOW, new_id=ids)


def test_declare_know_folds_current_page_and_marks_other_pages_stale(env):
    repo, s, _ = env
    pid, pid2, _pid3 = two_pages(env)
    assert repo.load("u1").state.current_page == pid
    seq = len(repo.aspect_log("u1"))
    calls = s.calls()
    out = decl(env, "c2.term", "know", gw=gw_of(env))
    assert isinstance(out, DeclareOut)
    assert (out.cell.cell, out.cell.concept_id, out.cell.aspect) == ("c2.term", "c2", Aspect.TERM)
    assert out.cell.band is Band.KNOWN and out.cell.declared == "know"
    assert out.change.folded == ["s3"] and out.change.other_pages_stale == [pid2]
    assert out.change.llm_calls == 0 and s.calls() == calls
    evs = new_aspect(env, seq)
    assert [type(e).__name__ for e in evs] == ["Declare", "AddNote"]
    assert evs[1].text == "know" and evs[1].evidence_ref.startswith("declare:")
    snap = repo.load("u1")
    assert snap.state.pages[pid].page.state("s3").folded
    assert not snap.state.pages[pid2].page.state("s3").folded       # other page: untouched
    assert snap.state.pages[pid2].page.snapshot["c2.term"] is not Band.KNOWN

    out = decl(env, "c2.term", None)                                # clear: restored, no LLM
    assert out.cell.band is Band.NONE and out.cell.declared is None
    assert out.change.other_pages_stale == [pid2] and out.change.llm_calls == 0
    assert new_aspect(env, seq)[-1].text == "clear"
    assert band(env, "c2.term") is Band.NONE


def test_declare_dont_on_a_line_section_writes_nothing(env):
    pid = organized(env, LINE)
    out = decl(env, "c2.term", "dont", gw=gw_of(env))
    assert out.cell.band is Band.GAP and out.cell.declared == "dont"
    assert out.change.rewritten == [] and out.change.llm_calls == 0
    assert out.change.unresolved_conflicts == []
    assert patch_pending(env[0].load("u1").state, pid) == ()
    # nor does a later K-change pass
    snap = env[0].load("u1")
    assert k_changed(snap, snap.k, pid, gw_of(env), skip=None, now=NOW,
                     new_id=env[2]).change.rewritten == []


def test_mastered_takes_back_the_mark_on_its_cells(env):
    repo, _, ids = env
    pid = organized(env)
    decl(env, "c2.term", "dont", gw=gw_of(env))
    sid = next(s.id for s in rec_of(env, pid).page.outline.sections if "c2.term" in s.cells)
    feedback_know(repo, gw_of(env), "u1", pid, sid, now=NOW, new_id=ids)
    assert repo.load("u1").kview(NOW).cell("c2.term").declared is None


def test_declare_without_gateway_degrades(env):
    pid = organized(env, LINE)
    out = decl(env, "c2.term", "dont", gw=None)
    assert out.cell.band is Band.GAP and out.change.llm_calls == 0
    assert rec_of(env, pid).page.unresolved_conflicts == ()


def test_declare_with_no_current_page_only_lists_stale(env):
    repo, _, ids = env
    snap = repo.load("u1")
    repo.commit(snap, Commit(aspect=[DefineConcept(event_id=ids("aev"), ts=NOW, concept_id="c9",
                                                   aspects=(Aspect.TERM,))]))
    assert repo.load("u1").state.current_page is None
    out = decl(env, "c9.term", "know")
    assert out.change == ChangeOut() and out.cell.band is Band.KNOWN


def test_declare_unknown_cell_is_lookup_error(env):
    organized(env)
    for cell in ("c2.procedure", "c7.term", "nodot", "c2.nonsense"):
        with pytest.raises(LookupError):
            decl(env, cell, "know")


# ---- k_changed directly ----

def test_k_changed_lists_only_other_organized_pages_whose_bands_moved(env):
    repo, _, ids = env
    pid, pid2, _pid3 = two_pages(env)
    snap = repo.load("u1")
    k_after = apply_in_memory(snap, [Declare(event_id="d1", ts=NOW, cell="c2.term",
                                             value="know")])
    kc = k_changed(snap, k_after, pid2, None, skip=None, now=NOW,
                   new_id=ids)
    assert kc.change.other_pages_stale == [pid]                  # pid3 is not organized
    assert kc.change.folded == ["s3"] and kc.record is not None and kc.record.page_id == pid2
    assert [type(e.body) for e in kc.wiki] == [PageSaved]
    same = k_changed(snap, snap.k.copy(), pid, None, skip=None, now=NOW,
                     new_id=ids)
    assert same.change == ChangeOut() and same.wiki == []


def test_gateway_counts_are_per_request(env):
    pid = organized(env)
    gw = CountingGateway(env[1].gateway())
    out = know(env, pid, "s2", gw=gw)
    assert out.change.llm_calls == 0 and gw.calls == 0


# ---- I1: a K change never replaces a check section, so its stored answer stays ----

def answered_s4(env) -> str:
    """LINE page whose verified check s4 has been answered (0 calls)."""
    pid = organized(env, LINE)
    check(env, pid, 0)
    assert rec_of(env, pid).checks == {"s4": 0}
    return pid


def test_check_keeps_its_answer_through_a_k_change(env):
    pid = answered_s4(env)
    repo, _, ids = env
    snap = repo.load("u1")
    aspect = [Observe(event_id=ids("aev"), ts=NOW, observation=Observation(
        cell="c2.term", polarity="-", strength=1.0, kind="check", evidence_ref="check:x:y"))]
    kc = k_changed(snap, apply_in_memory(snap, aspect), pid, gw_of(env), skip=None, now=NOW,
                   new_id=ids)
    repo.commit(snap, Commit(aspect=aspect, wiki=kc.wiki))
    assert rec_of(env, pid).checks == {"s4": 0}


# ---- M1: the rewrite stream commits before `change` ----

def test_rewrite_retries_on_a_fresh_snapshot_when_another_page_changed(env):
    """The writer takes minutes: a write elsewhere meanwhile is not a conflict any more."""
    repo, s, ids = env
    pid = organized(env)
    pid2 = add_page(repo, ids)
    s.push(WRITE, text("s2", "换个角度讲 z"))
    kit = writing(env)

    def meddle():                                  # a non-benign write lands meanwhile
        snap = repo.load("u1")
        done2 = snap.state.pages[pid2].model_copy(update={"done": True})
        repo.commit(snap, Commit(wiki=[_done_event(ids, done2)]))
    kit.writer.before_write = meddle
    items, _ = rewrite(env, pid, "s2", "other", kit=kit)
    assert names(items) == ["writer", "section_delta", "section_done", "change", "done"]
    st = repo.load("u1").state
    assert st.pages[pid2].done and st.pages[pid].page.texts["s2"].body == "换个角度讲 z"


def test_rewrite_of_a_page_changed_meanwhile_is_a_conflict_and_commits_only_the_signal(env):
    repo, s, ids = env
    pid = organized(env)
    s.push(WRITE, text("s2", "换个角度讲 z"))
    kit = writing(env)

    def meddle():                                  # this page's text changes meanwhile
        snap = repo.load("u1")
        rec = snap.state.pages[pid]
        texts = {**rec.page.texts, "s3": rec.page.texts["s3"].model_copy(
            update={"body": "别人改的"})}
        rec = rec.model_copy(update={"page": rec.page.model_copy(update={"texts": texts})})
        repo.commit(snap, Commit(wiki=[_done_event(ids, rec)]))
    kit.writer.before_write = meddle
    items, _ = rewrite(env, pid, "s2", "other", kit=kit)
    seq = repo.load("u1").wiki_seq - 1             # the signal is the only commit after it
    assert names(items) == ["writer", "section_delta", "section_done", "error"]
    assert items[-1][1].code == "conflict"
    only_signal_since(env, seq, "section_other")


def _done_event(ids, rec):
    from evowiki.wikiapp.events import WikiEvent
    return WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(record=rec, action="done"))


def sec_of_outline(outline, sid):
    return next(x for x in outline.sections if x.id == sid)
