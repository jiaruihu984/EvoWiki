import copy

import pytest

from evowiki.belief.aspects import Declare, Observe
from evowiki.goals.contracts import Band, Observation
from evowiki.llm.gateway import Gateway
from evowiki.wikiapp.events import (
    DiffNote,
    PageOpened,
    PageRecord,
    PageSaved,
    UsageAdded,
    WikiEvent,
)
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.page import (
    is_page_stale,
    mark_done,
    open_page_flow,
    set_fold,
    set_pin,
)
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiInvalid
from evowiki.wikiapp.stale import patch_pending
from evowiki.wikiapp.wiki_schemas import DoneOut, PageOut, SectionOut
from tests.runtime.llm_helpers import GW_CONFIG, MemoryCallLog
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_organize import OUTLINE, add_page, page1, run, script_page

LINE = copy.deepcopy(OUTLINE)
LINE["sections"][2]["role"] = "line"            # s3 「标准差提醒」 only a line: c2.term
LINE_CONFLICT = {"kind": "line_weak", "section_id": "s3", "cell": "c2.term"}


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def organized(env, outline=OUTLINE) -> str:
    _, s, _ = env
    pid = page1(env)
    script_page(s, outline)
    run(env, pid)
    return pid


def commit_wiki(env, *bodies):
    repo, _, ids = env
    snap = repo.load("u1")
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=b)
                                   for b in bodies]))


def declare(env, value, *cells):
    repo, _, ids = env
    snap = repo.load("u1")
    repo.commit(snap, Commit(aspect=[Declare(event_id=ids("aev"), ts=NOW, cell=c, value=value)
                                     for c in cells]))


def miss(env, *cells):
    """A wrong answer on each cell (K drops, unlike a 「不会」, which is the chat's to re-explain)."""
    repo, _, ids = env
    snap = repo.load("u1")
    repo.commit(snap, Commit(aspect=[Observe(event_id=ids("aev"), ts=NOW, observation=Observation(
        cell=c, polarity="-", strength=1.0, kind="check", evidence_ref="check:x:y")) for c in cells]))


def page_on(repo, ids, concepts=("c1", "c2"), title="另一页") -> str:
    """An unorganized page on `concepts` (its target the first)."""
    snap = repo.load("u1")
    rec = PageRecord(page_id=ids("pg"), title=title, goal=title,
                     concepts=concepts, targets=concepts[:1], created_at=NOW)
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=PageSaved(record=rec, action="created"))]))
    return rec.page_id


def shelve(env, pid):
    """The page shelved as a reference card, its collapse not applied yet (a regenerated
    shelved page: the next open folds it)."""
    rec = env[0].load("u1").state.pages[pid]
    commit_wiki(env, PageSaved(record=rec.model_copy(update={"shelved": True}), action="test"))


def gw_of(env):
    return CountingGateway(env[1].gateway())


def open_(env, pid, gw="new", **kw) -> PageOut:
    repo, _, ids = env
    gw = gw_of(env) if gw == "new" else gw
    return open_page_flow(repo, gw, "u1", pid, now=NOW, new_id=ids, **kw)


def sec(out: PageOut, sid: str) -> SectionOut:
    return next(x for x in out.sections if x.id == sid)


def test_unorganized_page_open_only_records_opened(env):
    repo, s, _ = env
    pid = page1(env)
    seq = repo.load("u1").wiki_seq
    calls = s.calls()
    out = open_(env, pid)
    assert out.organized is False and out.sections == [] and out.page_id == pid
    assert out.title == "z 分数" and out.llm_calls == 0
    log = repo.wiki_log("u1")[seq:]
    assert [type(e.body) for e in log] == [PageOpened]
    assert repo.load("u1").state.current_page == pid and s.calls() == calls


def test_unknown_page_is_lookup_error(env):
    page1(env)
    with pytest.raises(LookupError):
        open_(env, "pg_nope")


def test_reopen_without_k_change_is_free(env):
    repo, s, _ = env
    pid = organized(env)
    calls = s.calls()
    out = open_(env, pid)
    assert out.organized and out.source == "organizer" and out.profile == OUTLINE["profile"]
    assert [x.id for x in out.sections] == ["s1", "s2", "s3", "s4"]
    assert out.diff is None and out.llm_calls == 0 and s.calls() == calls
    assert sec(out, "s2").body.startswith("z 告诉你") and sec(out, "s2").changed == "rewritten"
    assert sec(out, "s4").check is not None and out.unresolved_conflicts == []
    seq = repo.load("u1").wiki_seq
    open_(env, pid)
    assert [type(e.body) for e in repo.wiki_log("u1")[seq:]] == [PageOpened]   # nothing to save


def test_k_up_folds_section_with_structured_diff(env):
    repo, s, _ = env
    pid = organized(env)
    declare(env, "know", "c2.term")
    calls = s.calls()
    assert is_page_stale(repo.load("u1").state, repo.load("u1").k,
                         repo.load("u1").state.pages[pid], NOW)
    out = open_(env, pid)
    assert out.llm_calls == 0 and s.calls() == calls
    assert out.diff is not None
    assert [(c.cell, c.from_, c.to) for c in out.diff.cell_changes] == [
        ("c2.term", Band.NONE, Band.KNOWN)]
    assert out.diff.folded == ["s3"] and out.diff.unfolded == [] and out.diff.patched == []
    assert sec(out, "s3").folded and sec(out, "s3").changed == "folded"
    assert out.model_dump(mode="json")["diff"]["cell_changes"] == [
        {"cell": "c2.term", "from": "none", "to": "known"}]
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    assert repo.wiki_log("u1")[-2].body.action == "opened"
    assert isinstance(rec.diff, DiffNote) and rec.page.snapshot["c2.term"] is Band.KNOWN
    assert not is_page_stale(snap.state, snap.k, rec, NOW)
    again = open_(env, pid)                       # nothing moved since: diff cleared, still folded
    assert again.diff is None and sec(again, "s3").folded and again.llm_calls == 0


def test_k_down_to_conflict_records_it_and_rewrites_nothing(env):
    repo, s, _ = env
    pid = organized(env, LINE)
    old = repo.load("u1").state.pages[pid].page.texts["s3"].body
    miss(env, "c2.term")
    calls = s.calls()
    out = open_(env, pid)
    assert s.calls() == calls and out.llm_calls == 0           # the outline is the writer's
    assert out.diff.patched == []
    assert [(c.cell, c.to) for c in out.diff.cell_changes] == [("c2.term", Band.GAP)]
    s3 = sec(out, "s3")
    assert s3.role.value == "line" and s3.changed is None and s3.body == old
    assert [c.model_dump() for c in out.conflicts] == [LINE_CONFLICT]
    assert patch_pending(repo.load("u1").state, pid) == ()


def test_pinned_section_is_never_handed_to_the_writer(env):
    repo, _s, _ = env
    pid = organized(env, LINE)
    pinned = set_pin(repo, "u1", pid, "s3", True, now=NOW, new_id=env[2])
    assert pinned.pinned and repo.wiki_log("u1")[-1].body.action == "pin"
    miss(env, "c2.term")
    out = open_(env, pid)
    assert [x.id for x in out.sections] == ["s1", "s2", "s3", "s4"]
    s3 = sec(out, "s3")
    assert s3.role.value == "line" and s3.pinned and s3.changed is None
    assert out.diff.patched == [] and out.llm_calls == 0
    assert [c.kind for c in out.conflicts] == ["pinned_line_weak"]   # the banner: a reorganize
    assert patch_pending(repo.load("u1").state, pid) == ()


def test_conflict_stays_unresolved_until_reorganize(env):
    _repo, s, _ = env
    pid = organized(env, LINE)
    miss(env, "c2.term")
    out = open_(env, pid)
    assert out.llm_calls == 0
    assert len(out.unresolved_conflicts) == 1 and "标准差" in out.unresolved_conflicts[0]
    calls = s.calls()
    again = open_(env, pid)                                     # K unchanged: still there, free
    assert again.unresolved_conflicts == out.unresolved_conflicts
    assert [c.model_dump() for c in out.conflicts] == [LINE_CONFLICT]
    assert again.conflicts == out.conflicts
    assert again.llm_calls == 0 and s.calls() == calls
    script_page(s, OUTLINE)  # s3 a recap; verifier log hit
    items, _ = run(env, pid)                                    # reorganize clears them
    assert items[-1][0] == "done" and s.pending() == {}
    cleared = open_(env, pid)
    assert cleared.unresolved_conflicts == [] and cleared.conflicts == []


def test_no_llm_open_folds_and_keeps_conflicts_unresolved(env):
    repo, s, _ = env
    pid = organized(env, LINE)
    miss(env, "c2.term")
    declare(env, "know", "c1.procedure", "c1.interpret")
    calls = s.calls()
    out = open_(env, pid, gw=None)
    assert s.calls() == calls and out.llm_calls == 0
    assert out.diff.folded == ["s2"] and out.diff.patched == []
    assert len(out.unresolved_conflicts) == 1 and "标准差" in out.unresolved_conflicts[0]
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    assert rec.page.unresolved_conflicts == tuple(out.unresolved_conflicts)
    assert [c.model_dump() for c in out.conflicts] == [LINE_CONFLICT]
    assert [c.model_dump() for c in rec.page.conflicts] == [LINE_CONFLICT]
    assert rec.page.snapshot["c2.term"] is Band.GAP
    assert not is_page_stale(snap.state, snap.k, rec, NOW)


def test_collapsed_page_folds_everything_on_first_open_only(env):
    repo, _s, _ = env
    pid = organized(env)
    declare(env, "know", "c1.procedure", "c1.interpret")       # targets known: stays shelved
    open_(env, pid, gw=None)                                    # fold pass folds s2
    shelve(env, pid)
    out = open_(env, pid)
    assert out.collapsed and out.llm_calls == 0
    assert [x.id for x in out.sections if x.folded] == ["s2", "s3"]
    assert sec(out, "s3").changed == "folded"
    assert out.diff is not None                                 # a collapse is a visible change
    assert (out.diff.cell_changes, out.diff.folded, out.diff.unfolded, out.diff.patched) == (
        [], ["s3"], [], [])
    assert repo.load("u1").state.pages[pid].collapse_applied
    unfolded = set_fold(repo, "u1", pid, "s2", False, now=NOW, new_id=env[2])
    assert not unfolded.folded and unfolded.fold_locked
    again = open_(env, pid)                                     # applied once: s2 stays open
    assert not sec(again, "s2").folded and sec(again, "s3").folded
    assert again.diff is None


def test_collapse_then_fold_pass_unfold_is_no_net_change(env):
    pid = organized(env)
    shelve(env, pid)
    declare(env, "know", "c1.procedure", "c1.interpret")   # K moved: fold pass after collapse
    miss(env, "c2.term")                  # s3 (recap, c2.term) has a weak cell again
    out = open_(env, pid, gw=None)
    assert sec(out, "s2").folded and not sec(out, "s3").folded
    assert out.diff.folded == ["s2"] and out.diff.unfolded == []
    assert sec(out, "s2").changed == "folded" and sec(out, "s3").changed is None


# ---- LLM down or failing: GET / done degrade instead of failing ----

def unavailable_gateway() -> CountingGateway:
    """A configured gateway whose provider is unreachable (ProviderUnavailableError)."""
    return CountingGateway(Gateway(GW_CONFIG, MemoryCallLog(), mode="record", providers={}))


def test_open_with_unreachable_provider_needs_no_llm(env):
    repo, s, _ = env
    pid = organized(env, LINE)
    miss(env, "c2.term")
    seq = repo.load("u1").wiki_seq
    calls = s.calls()
    out = open_(env, pid, gw=unavailable_gateway())
    assert out.organized and len(out.unresolved_conflicts) == 1
    assert out.diff is not None and out.diff.patched == []
    assert [type(e.body) for e in repo.wiki_log("u1")[seq:]] == [PageSaved, PageOpened]
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    assert rec.page.unresolved_conflicts == tuple(out.unresolved_conflicts)
    assert not is_page_stale(snap.state, snap.k, rec, NOW)
    assert s.calls() == calls and out.budget_exceeded is False


def budget_gateway(env) -> CountingGateway:
    """A gateway whose every call hits the spending limit (BudgetExceededError)."""
    from evowiki.llm.usage import Account, BudgetExceededError

    class Budget(CountingGateway):
        def call(self, role, request, schema):
            raise BudgetExceededError(Account(user_id="u1", group="page"), "day", 1.0)

    return Budget(env[1].gateway())


def test_open_over_budget_still_updates_the_page(env):
    repo, _s, _ = env
    pid = organized(env, LINE)
    miss(env, "c2.term")
    out = open_(env, pid, gw=budget_gateway(env))
    assert out.organized and len(out.unresolved_conflicts) == 1
    assert out.budget_exceeded is False and out.diff.patched == []   # the update calls nothing
    rec = repo.load("u1").state.pages[pid]
    assert "budget_exceeded" not in type(rec).model_fields      # response-only flag


def test_manual_fold_lock_survives_auto_fold(env):
    repo, _, ids = env
    pid = organized(env)
    seq = repo.load("u1").wiki_seq
    same = set_fold(repo, "u1", pid, "s3", False, now=NOW, new_id=ids)   # already unfolded
    assert not same.folded and not same.fold_locked
    assert repo.load("u1").wiki_seq == seq                                # no event
    folded = set_fold(repo, "u1", pid, "s3", True, now=NOW, new_id=ids)
    assert folded.folded and folded.fold_locked
    set_fold(repo, "u1", pid, "s3", False, now=NOW, new_id=ids)
    assert [getattr(e.body, "action", e.body.type) for e in repo.wiki_log("u1")[seq:]] == [
        "fold", "fold", "signal_recorded"]                              # the unfold is a signal
    declare(env, "know", "c2.term")
    out = open_(env, pid)
    assert not sec(out, "s3").folded and out.diff.folded == []


def test_fold_and_pin_errors(env):
    repo, _, ids = env
    pid = organized(env)
    with pytest.raises(WikiInvalid):
        set_fold(repo, "u1", pid, "s1", True, now=NOW, new_id=ids)     # organizer: not foldable
    with pytest.raises(LookupError):
        set_fold(repo, "u1", pid, "s9", True, now=NOW, new_id=ids)
    with pytest.raises(LookupError):
        set_pin(repo, "u1", pid, "s9", True, now=NOW, new_id=ids)
    with pytest.raises(LookupError):
        set_pin(repo, "u1", "pg_nope", "s1", True, now=NOW, new_id=ids)


def test_fold_on_unorganized_page_is_lookup_error(env):
    repo, _, ids = env
    pid = page1(env)
    with pytest.raises(LookupError):
        set_fold(repo, "u1", pid, "s2", True, now=NOW, new_id=ids)


def test_done_marks_the_page_done(env):
    repo, _s, ids = env
    pid = organized(env)
    add_page(repo, ids)
    out = mark_done(repo, "u1", pid, now=NOW, new_id=ids)
    assert out == DoneOut()
    snap = repo.load("u1")
    assert snap.state.pages[pid].done
    assert any(isinstance(e.body, UsageAdded) and e.body.record.page == pid
               for e in repo.wiki_log("u1"))
    assert open_(env, pid).done


def test_page_opened_and_saved_events_order(env):
    repo, _, _ = env
    pid = organized(env)
    declare(env, "know", "c2.term")
    seq = repo.load("u1").wiki_seq
    open_(env, pid)
    bodies = [e.body for e in repo.wiki_log("u1")[seq:]]
    assert [type(b) for b in bodies] == [PageSaved, PageOpened]
    assert bodies[0].action == "opened"
