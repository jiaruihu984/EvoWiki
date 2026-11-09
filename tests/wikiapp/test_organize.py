import pytest

from evowiki.belief.aspects import Declare
from evowiki.goals.contracts import FOLDABLE_ROLES, Band, CheckItem
from evowiki.goals.diagnostic import _shuffled
from evowiki.goals.fold import set_pinned
from evowiki.wikiapp.events import (
    PageOpened,
    PageRecord,
    PageSaved,
    SectionLogsSaved,
    WikiEvent,
)
from evowiki.wikiapp.goal import _taught
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import organize_stream
from evowiki.wikiapp.page import mark_done, open_page_flow
from evowiki.wikiapp.repo import Commit, MemoryRepo, WikiConflict
from evowiki.wikiapp.wiki_schemas import (
    ORGANIZE_PAYLOADS,
    ErrorEvent,
    OrganizeDoneEvent,
    OutlineEvent,
)
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.fake_writer import WRITE, writing
from tests.wikiapp.flat_goal import flat_page
from tests.wikiapp.runs import runs_for
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids, goal1

OUTLINE = {
    "profile": "你会算 z 分数，但解读还模糊",
    "assumed": [],
    "sections": [
        {"id": "s1", "heading": "从你会的出发", "role": "organizer", "cells": [], "why": "挂到已会的"},
        {"id": "s2", "heading": "z 分数怎么读", "role": "explain",
         "cells": ["c1.procedure", "c1.interpret"], "why": "目标格，计算模糊",
         "needs_lookup": ["经验法则 68-95-99.7"]},
        {"id": "s3", "heading": "标准差提醒", "role": "recap", "cells": ["c2.term"], "why": "前置"},
        {"id": "s4", "heading": "确认一下", "role": "check", "cells": ["c1.interpret"],
         "why": "答案决定 s2 写多深",
         "check": {"cell": "c1.interpret", "stem": "z=-1 表示？",
                   "options": ["低一个标准差", "低 1 分", "无意义"], "answer": 0}},
    ],
}


def shuffled_key(check: dict) -> int:
    """Where a scripted check's key lands: organizer@v9 shuffles kept check options."""
    return _shuffled(CheckItem(cell=check["cell"], stem=check["stem"],
                               options=tuple(check["options"]), answer=check["answer"])).answer


CHECK_KEY = shuffled_key(OUTLINE["sections"][3]["check"])     # where 「低一个标准差」 is shown


def shown(option: str) -> int:
    """The index OUTLINE's check option `option` is shown at (options shuffled)."""
    c = OUTLINE["sections"][3]["check"]
    return _shuffled(CheckItem(cell=c["cell"], stem=c["stem"], options=tuple(c["options"]),
                               answer=c["answer"])).options.index(option)
LAST_KIT: list = []


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


BODY2 = "z 告诉你离均值几个标准差。⟦待查证：约 68% 落在 ±1 内⟧"


def text(sid: str, body: str = "") -> dict:
    return {"text": f"回想：{sid} 的要点\n\n{body or sid + ' 正文'}"}


def page1(env):
    """The z-score goal's one page (tests.wikiapp.flat_goal.flat_page: its draft skipped)."""
    repo, s, ids = env
    res, _ = goal1(env)
    return flat_page(repo, CountingGateway(s.gateway()), "u1", res.draft.draft_id, now=NOW,
                     new_id=ids)


def add_page(repo, ids, title="z 分数（复习）") -> str:
    """A second page on the same concepts, committed directly (no intake needed)."""
    snap = repo.load("u1")
    rec = PageRecord(page_id=ids("pg"), title=title, goal=title,
                     concepts=("c1", "c2"), targets=("c1",), created_at=NOW)
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=PageSaved(record=rec, action="created"))]))
    return rec.page_id


def run(env, pid, gw=None, **kw):
    repo, s, ids = env
    gw = gw or CountingGateway(s.gateway())
    if "writing" not in kw:
        kw["writing"] = writing(env)
        LAST_KIT.append(kw["writing"])
    return list(organize_stream(repo, gw, "u1", pid, now=NOW, new_id=ids,
                                runs=kw.pop("runs", None) or runs_for(repo), **kw)), gw


def kit_inputs(env):
    """The inputs the scripted writer of `run`'s default kit got (the last kit `writing` built)."""
    return LAST_KIT[-1].writer.inputs


def names(items):
    return [n for n, _ in items]


def checks_of(outline) -> list[dict]:
    return [x["check"] for x in outline["sections"] if x["role"] == "check" and x.get("check")]


def script_page(s, outline=OUTLINE, writes=None):
    """organizer → one write per section the page writer writes (default: every non-check
    section of the outline). Check items need no verification call."""
    if writes is None:
        writes = [x["id"] for x in outline["sections"] if x["role"] != "check"]
    s.push(ORGANIZE, outline)
    for sid in writes:
        s.push(WRITE, text(sid, BODY2 if sid == "s2" else ""))


def test_first_generation_events_calls_and_record(env):
    repo, s, _ = env
    pid = page1(env)
    assert repo.load("u1").state.pages[pid].page is None        # organized=False before
    script_page(s)
    items, gw = run(env, pid)
    assert names(items) == ["planning", "outline", "writer", "section_delta", "section_done", "section_delta",
                            "section_done", "section_delta", "section_done", "done"]
    for name, payload in items:
        assert isinstance(payload, ORGANIZE_PAYLOADS[name])
    out = items[1][1]
    assert isinstance(out, OutlineEvent)
    assert (out.page_id, out.source) == (pid, "organizer")
    assert out.profile == OUTLINE["profile"]
    assert [x.id for x in out.sections] == ["s1", "s2", "s3", "s4"]
    assert all(x.body == "" and x.cue == "" for x in out.sections)
    s2 = out.sections[1]
    assert [(c.cell, c.concept_id, c.concept_name, c.aspect.value, c.band, c.declared)
            for c in s2.cells] == [
        ("c1.procedure", "c1", "z 分数", "procedure", Band.SHAKY, None),
        ("c1.interpret", "c1", "z 分数", "interpret", Band.NONE, None)]
    assert s2.foldable and not out.sections[0].foldable and s2.why == "目标格，计算模糊"
    chk = out.sections[3].check
    assert chk is not None and chk.correct_index is None and chk.options[CHECK_KEY] == "低一个标准差"
    assert items[2][1].model == "anthropic:claude-sonnet-5-5" and not items[2][1].fallback
    delta = items[5][1]
    assert (delta.section_id, delta.cue) == ("s2", "s2 的要点") and delta.body == BODY2
    assert delta.heading == "z 分数怎么读"
    assert [p.cached for n, p in items if n == "section_done"] == [False, False, False]
    done = items[-1][1]
    assert isinstance(done, OrganizeDoneEvent)
    assert (done.page_id, done.llm_calls) == (pid, 3)
    assert (done.model, done.fallback, done.prefetched) == ("anthropic:claude-sonnet-5-5", False,
                                                            False)
    # no model call before the writer: the outline is its plan_page (one), then its writes
    assert gw.calls == 0 and s.calls(ORGANIZE) == 1 and s.calls(WRITE) == 3
    assert s.calls("goals.diagnostic.verify") == 0             # no verification call
    assert repo.load("u1").state.pages[pid].page.outline.sections[3].check.verified

    rec = repo.load("u1").state.pages[pid]
    log = repo.wiki_log("u1")       # the SectionLogs go in the same commit, right after the page
    assert log[-2].body.action == "organized" and isinstance(log[-1].body, SectionLogsSaved)
    assert rec.page is not None and set(rec.page.texts) == {"s1", "s2", "s3"}
    assert rec.page.texts["s2"].body == BODY2
    assert rec.changed == {"s1": "rewritten", "s2": "rewritten", "s3": "rewritten"}
    assert rec.page.unresolved_conflicts == () and rec.diff is None and rec.checks == {}
    assert rec.page.snapshot["c1.procedure"] is Band.SHAKY
    assert rec.written.kind == "page" and rec.written.sections == ("s1", "s2", "s3")
    assert rec.written.model == "anthropic:claude-sonnet-5-5" and rec.written.fallback_reason is None
    producers = {x.section_id: x.producer for x in log[-1].body.logs}
    assert producers == {"s1": "writer", "s2": "writer", "s3": "writer", "s4": "organizer"}


def test_every_page_is_written_by_the_writer_without_a_section_cache(env):
    """Single writer: a second page on the same cells is written in full again (the old DeepSeek
    section cache does not feed page writes any more)."""
    repo, s, ids = env
    pid = page1(env)
    script_page(s)
    run(env, pid)
    pid2 = add_page(repo, ids)
    script_page(s)
    items, gw = run(env, pid2)
    assert gw.calls == 0 and items[-1][1].llm_calls == 3       # the writer's 3 writes
    assert [(p.section_id, p.cached) for n, p in items if n == "section_done"] == [
        ("s1", False), ("s2", False), ("s3", False)]
    rec = repo.load("u1").state.pages[pid2]
    assert rec.changed == {"s1": "rewritten", "s2": "rewritten", "s3": "rewritten"}
    assert s.pending() == {}


def test_reorganize_of_reference_card_page_folds_again_on_open(env):
    """M3: reorganizing a page shelved as a reference card resets `collapse_applied` (it stays
    shelved)."""
    repo, s, ids = env
    pid = page1(env)
    script_page(s)
    run(env, pid)
    snap = repo.load("u1")
    rec = snap.state.pages[pid].model_copy(update={"collapse_applied": True, "shelved": True})
    repo.commit(snap, Commit(wiki=[
        WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(record=rec, action="opened"))]))
    # a reference card is a `quick` page: other spec items, so no section-cache hit
    script_page(s)
    items, _ = run(env, pid)
    assert names(items)[-1] == "done"
    assert repo.load("u1").state.pages[pid].collapse_applied is False
    assert repo.load("u1").state.pages[pid].shelved
    snap = repo.load("u1")                      # targets known, or the page leaves the shelf
    repo.commit(snap, Commit(aspect=[Declare(event_id=ids("aev"), ts=NOW, cell=c, value="know")
                                     for c in ("c1.procedure", "c1.interpret")]))

    from evowiki.wikiapp.page import open_page_flow
    open_page_flow(repo, None, "u1", pid, now=NOW, new_id=ids)
    out = repo.load("u1").state.pages[pid]
    assert out.collapse_applied is True
    foldable = [x.id for x in out.page.outline.sections if x.role in FOLDABLE_ROLES]
    assert foldable and all(out.page.state(x).folded for x in foldable)


def test_reorganize_replaces_page_and_asks_organizer_again(env):
    repo, s, ids = env
    pid = page1(env)
    script_page(s)
    run(env, pid)
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    pinned = rec.page.model_copy(update={
        "states": set_pinned(rec.page.outline, rec.page.states, "s2"),
        "unresolved_conflicts": ("「z 分数怎么读」和 K 冲突",)})
    rec = rec.model_copy(update={"page": pinned, "checks": {"s4": 0, "s3": 1}})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=PageSaved(record=rec, action="pin"))]))
    assert repo.load("u1").state.pages[pid].page.state("s2").pinned

    script_page(s)
    items, gw = run(env, pid)
    assert "outline" in names(items) and names(items)[-1] == "done"
    assert s.calls(ORGANIZE) == 2 and gw.calls == 0                # the writer outlines again
    assert kit_inputs(env)[-1].needs_outline and kit_inputs(env)[-1].page is None
    assert [(p.section_id, p.cached) for n, p in items if n == "section_done"] == [
        ("s1", False), ("s2", False), ("s3", False)]
    out = repo.load("u1").state.pages[pid]
    assert repo.wiki_log("u1")[-2].body.action == "reorganized"
    # the locked section stays as it was (goals.fold.keep_locked); everything else is new
    assert out.page.state("s2").pinned and out.page.texts["s2"] == rec.page.texts["s2"]
    assert not any(st.folded for st in out.page.states.values())
    assert out.page.unresolved_conflicts == ()
    assert out.checks == {}                       # every check item is new (I1)
    assert out.changed == {"s1": "rewritten", "s3": "rewritten"}


def test_reorganize_drops_answers_to_vanished_checks(env):
    repo, s, ids = env
    pid = page1(env)
    script_page(s)
    run(env, pid)
    snap = repo.load("u1")
    rec = snap.state.pages[pid].model_copy(update={"checks": {"s4": 0}})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=PageSaved(record=rec, action="check"))]))
    no_check = {**OUTLINE, "sections": OUTLINE["sections"][:3]}
    script_page(s, outline=no_check)
    run(env, pid)
    assert repo.load("u1").state.pages[pid].checks == {}


def test_a_refused_outline_is_fixed_by_the_writer(env):
    """plan_page sends an unusable outline back; the writer's next one is accepted."""
    _, s, _ = env
    pid = page1(env)
    s.push(ORGANIZE, {"sections": []})
    script_page(s)
    items, _ = run(env, pid)
    assert names(items)[-1] == "done" and s.calls(ORGANIZE) == 2
    assert names(items).count("outline") == 1 and items[1][1].source == "organizer"


def test_an_outline_never_accepted_is_an_error_and_nothing_is_stored(env):
    """No deterministic fallback outline any more: the write fails closed."""
    repo, s, _ = env
    pid = page1(env)
    s.push(ORGANIZE, {"sections": []}, {"sections": []}, {"sections": []})
    items, _ = run(env, pid)
    assert names(items) == ["planning", "error"] and items[-1][1].code == "llm_failed"
    assert repo.load("u1").state.pages[pid].page is None


def test_writer_failure_keeps_the_committed_sections_and_the_page_is_not_taught(env):
    """T3: the outline and each written section are committed as the writer goes; a failed write
    leaves them on the page (not taught, not finishable) and the next write overwrites them."""
    repo, s, ids = env
    pid = page1(env)
    s.push(ORGANIZE, OUTLINE)
    s.push(WRITE, text("s1"), "not json")
    items, _ = run(env, pid)
    assert names(items) == ["planning", "outline", "writer", "section_delta", "section_done", "error"]
    err = items[-1][1]
    assert isinstance(err, ErrorEvent)
    assert (err.code, err.retryable) == ("llm_failed", True) and "no_draft" in err.detail
    rec = repo.load("u1").state.pages[pid]
    assert [x.id for x in rec.page.outline.sections] == ["s1", "s2", "s3", "s4"]
    assert set(rec.page.texts) == {"s1"} and rec.page.texts["s1"].body == "s1 正文"
    assert rec.writing is not None and not rec.has_body
    assert rec.written is None and not rec.done
    saved = [e.body.action for e in repo.wiki_log("u1")
             if isinstance(e.body, PageSaved) and e.body.record.page_id == pid]
    assert saved[-2:] == ["outlined", "section_written"]
    assert not _taught(repo.load("u1").state)
    with pytest.raises(WikiConflict, match="还没写好"):
        mark_done(repo, "u1", pid, now=NOW, new_id=ids)
    # 重新写: still a first write (organized), overwriting
    script_page(s)
    items, _ = run(env, pid)
    assert names(items)[-1] == "done"
    rec = repo.load("u1").state.pages[pid]
    assert rec.has_body and rec.writing is None and set(rec.page.texts) == {"s1", "s2", "s3"}
    assert repo.wiki_log("u1")[-2].body.action == "organized"


def test_a_failed_reorganize_keeps_the_finished_page_stored_and_taught(env):
    """Fail closed: a write over a page that has its finished text commits nothing as it goes;
    when it fails half-way the old page is exactly as it was, still taught and finishable. The
    stream still showed the new outline and section."""
    repo, s, ids = env
    pid = page1(env)
    script_page(s)
    run(env, pid)
    before = repo.load("u1").state.pages[pid]
    assert before.has_body
    seq = repo.load("u1").wiki_seq
    s.push(ORGANIZE, OUTLINE)
    s.push(WRITE, text("s1", "新写的 s1"), "not json")
    items, _ = run(env, pid)                                   # reorganize, fails after s1
    assert names(items) == ["planning", "outline", "writer", "section_delta", "section_done",
                            "error"]
    snap = repo.load("u1")
    assert snap.wiki_seq == seq and snap.state.pages[pid] == before
    assert _taught(snap.state)
    mark_done(repo, "u1", pid, now=NOW, new_id=ids)
    assert repo.load("u1").state.pages[pid].done


def test_each_section_is_on_the_page_as_soon_as_it_is_written(env):
    """T3: a reload while the page is written shows sections 1..k, under the writing mark; the
    final commit is the one a single commit made (record, logs) and clears the mark."""
    repo, s, _ = env
    pid = page1(env)
    script_page(s)
    seen = []
    kit = writing(env)
    orig = kit.writer.write

    def write(inp, **kw):
        change = kw["on_change"]

        def spy(ws, sid):
            change(ws, sid)
            rec = repo.load("u1").state.pages[pid]
            seen.append((sorted(rec.page.texts), rec.writing is not None, rec.has_body))
        return orig(inp, **{**kw, "on_change": spy})
    kit.writer.write = write
    items, _ = run(env, pid, writing=kit)
    assert names(items)[-1] == "done"
    assert seen == [(["s1"], True, False), (["s1", "s2"], True, False),
                    (["s1", "s2", "s3"], True, False)]
    rec = repo.load("u1").state.pages[pid]
    assert rec.writing is None and rec.has_body
    assert rec.page.texts["s2"].body == BODY2 and rec.page.texts["s2"].cache_key.startswith("deep:")
    log = repo.wiki_log("u1")
    assert log[-2].body.action == "organized" and isinstance(log[-1].body, SectionLogsSaved)
    saved = [e.body.action for e in log
             if isinstance(e.body, PageSaved) and e.body.record.page_id == pid]
    assert saved[-5:] == ["outlined", "section_written", "section_written", "section_written",
                          "organized"]


def test_another_flow_changing_a_first_write_stops_it_keeping_its_sections_and_spend(env):
    """A first write commits as it goes; another flow changing the page after s1 makes the next
    commit a PageChanged: the run stops with conflict, s1 stays on the page (not taught), and
    the run records what the writer spent until then."""
    repo, s, ids = env
    pid = page1(env)
    script_page(s)
    kit = writing(env, cost_usd=0.03)
    orig = kit.writer.write

    def write(inp, **kw):
        change = kw["on_change"]

        def meddle(ws, sid):
            change(ws, sid)
            if sid == "s1":                       # another flow edits s1 right after it landed
                snap = repo.load("u1")
                rec = snap.state.pages[pid]
                texts = {**rec.page.texts, "s1": rec.page.texts["s1"].model_copy(
                    update={"body": "别人改的"})}
                rec = rec.model_copy(update={"page": rec.page.model_copy(
                    update={"texts": texts})})
                repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(
                    record=rec, action="feedback"))]))
        return orig(inp, **{**kw, "on_change": meddle})
    kit.writer.write = write
    runs = runs_for(repo)
    items, _ = run(env, pid, writing=kit, runs=runs)
    assert names(items)[-1] == "error"
    assert (items[-1][1].code, items[-1][1].retryable) == ("conflict", True)
    rec = repo.load("u1").state.pages[pid]
    assert rec.page.texts["s1"].body == "别人改的" and set(rec.page.texts) == {"s1"}
    assert not rec.has_body
    [row] = [r for r in runs.store._rows.values() if r.page_id == pid]
    assert row.status == "failed" and row.cost_usd and row.cost_usd > 0


def test_a_page_being_written_opens_as_committed_so_far(env):
    """GET while a run writes the page: its committed sections, no fold / patch, no writer line;
    a part-written page is never stale and the Bot does not count it as written."""
    repo, s, ids = env
    pid = page1(env)
    s.push(ORGANIZE, OUTLINE)
    s.push(WRITE, text("s1"), "not json")
    run(env, pid)
    out = open_page_flow(repo, None, "u1", pid, now=NOW, new_id=ids)
    assert out.organized and [x.id for x in out.sections] == ["s1", "s2", "s3", "s4"]
    assert out.sections[0].body == "s1 正文" and out.sections[1].body == ""
    assert out.writer is None
    rec = repo.load("u1").state.pages[pid]
    assert rec.writing is not None


def meddling(env, change):
    """A writer kit whose writer runs `change(repo)` first: the wiki moves while the page is being
    written."""
    kit = writing(env)
    kit.writer.before_write = lambda: change(env[0])
    return kit


def test_another_page_changing_meanwhile_is_retried_on_a_fresh_snapshot(env):
    repo, s, ids = env
    pid = page1(env)
    pid2 = add_page(repo, ids)
    script_page(s)

    def change(repo):                             # another request writes meanwhile
        snap = repo.load("u1")
        rec2 = snap.state.pages[pid2].model_copy(update={"done": True})
        repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                 body=PageSaved(record=rec2, action="done"))]))
    items, _ = run(env, pid, writing=meddling(env, change))
    assert names(items)[-1] == "done"
    st = repo.load("u1").state
    assert st.pages[pid].page is not None and st.pages[pid2].done


def test_this_page_changing_meanwhile_is_a_conflict_and_nothing_is_written(env):
    repo, s, ids = env
    pid = page1(env)
    script_page(s)
    run(env, pid)
    script_page(s)

    def change(repo):                             # another flow rewrites a section meanwhile
        snap = repo.load("u1")
        rec = snap.state.pages[pid]
        texts = {**rec.page.texts, "s1": rec.page.texts["s1"].model_copy(
            update={"body": "别人改的"})}
        rec = rec.model_copy(update={"page": rec.page.model_copy(update={"texts": texts})})
        repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                 body=PageSaved(record=rec, action="feedback"))]))
    items, _ = run(env, pid, writing=meddling(env, change))      # reorganize
    assert names(items)[-1] == "error"
    assert (items[-1][1].code, items[-1][1].retryable) == ("conflict", True)
    assert repo.load("u1").state.pages[pid].page.texts["s1"].body == "别人改的"


def test_page_opened_meanwhile_does_not_conflict(env):
    """M4: a GET of a page (PageOpened) while the page is written is benign; the page is stored
    after it and the opened mark is kept."""
    repo, s, ids = env
    pid = page1(env)
    script_page(s)

    def change(repo):
        snap = repo.load("u1")
        repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                 body=PageOpened(page_id=pid))]))
    items, _ = run(env, pid, writing=meddling(env, change))
    assert names(items)[-1] == "done"
    log = repo.wiki_log("u1")
    assert any(isinstance(e.body, PageOpened) for e in log[-8:])
    assert log[-2].body.action == "organized"
    out = repo.load("u1").state
    assert out.pages[pid].page is not None and pid in out.opened_at


def test_unknown_page_raises_before_streaming(env):
    repo, s, ids = env
    page1(env)
    with pytest.raises(LookupError):
        organize_stream(repo, CountingGateway(s.gateway()), "u1", "pg_nope", now=NOW, new_id=ids,
                        writing=writing(env), runs=runs_for(repo))
    assert s.calls() == 2                  # only the goal's intake (1 call, S2) and path


def section_ids(repo, pid):
    return [x.id for x in repo.load("u1").state.pages[pid].page.outline.sections]


def test_budget_exhausted_writer_is_budget_error_and_only_the_outline_is_stored(env):
    repo, s, _ = env
    pid = page1(env)
    s.push(ORGANIZE, OUTLINE)
    s.push(WRITE, "BUDGET")                 # the writer's spending limit is reached
    seq = repo.load("u1").wiki_seq
    items, _ = run(env, pid)
    assert names(items) == ["planning", "outline", "writer", "error"]
    err = items[-1][1]
    assert (err.code, err.retryable) == ("budget_exceeded", False)
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    assert snap.wiki_seq == seq + 1 and rec.page.texts == {} and not rec.has_body


def test_no_page_writer_is_llm_unavailable(env):
    repo, s, _ = env
    pid = page1(env)
    s.push(ORGANIZE, OUTLINE)
    items, _ = run(env, pid, writing=None)
    assert names(items) == ["error"] and items[-1][1].code == "llm_unavailable"
    assert repo.load("u1").state.pages[pid].page is None


def test_en_user_gets_the_english_rule_in_organizer_writer_and_check_verifier(env):
    from evowiki.goals.language import language_rule
    from evowiki.wikiapp.settings import put_settings
    from evowiki.wikiapp.wiki_schemas import SettingsIO
    repo, s, ids = env
    pid = page1(env)
    put_settings(repo, "u1", SettingsIO(language="en"), now=NOW, new_id=ids)
    script_page(s)
    kit = writing(env)
    items, _ = run(env, pid, writing=kit)
    assert names(items)[-1] == "done"
    en, zh = language_rule("en"), language_rule("zh")
    assert [i.language for i in kit.writer.inputs] == ["en"]   # the writer's prompts follow it
    assert kit.writer.inputs[0].spec.language == "en"          # its outline part too
    assert en != zh


def test_check_items_are_accepted_without_a_verification_call(env):
    repo, s, _ = env
    pid = page1(env)
    script_page(s)
    items, gw = run(env, pid)
    assert names(items)[-1] == "done" and gw.calls == 0 and s.pending() == {}
    out = items[1][1]
    assert [x.id for x in out.sections] == ["s1", "s2", "s3", "s4"] and out.dropped_checks == []
    check = repo.load("u1").state.pages[pid].page.outline.sections[3].check
    assert check.verified


def test_an_outline_event_that_cannot_be_sent_fails_the_run_visibly(env, monkeypatch):
    """Fail closed: when the outline cannot reach the stream, the writer is halted and the run
    ends in an error (no section is written after it)."""
    from evowiki.wikiapp import organize

    def broken(**kw):
        raise RuntimeError("outline event down")
    monkeypatch.setattr(organize, "OutlineEvent", broken)
    repo, s, _ = env
    pid = page1(env)
    script_page(s)
    items, _ = run(env, pid)
    assert names(items) == ["planning", "error"]
    assert "outline event down" in items[-1][1].detail
    rec = repo.load("u1").state.pages[pid]
    assert rec.page.texts == {} and not rec.has_body      # the outline was stored, nothing more
    assert s.calls(WRITE) == 0
