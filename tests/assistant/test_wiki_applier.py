"""WikiApplier (assistant/wiki.py): an accepted proposal as a Phase 1 minimal patch on E1's page
record - insert after a section / rewrite one section with the assistant's text, guardrails,
one PageSaved(action="assistant"), WikiConflict retries - and the decision
endpoint reporting `applied` or the refusal reason."""

import itertools
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from evowiki.agent.tools import ProposalDraft
from evowiki.assistant.loop import TurnDeps
from evowiki.assistant.service import AssistantService
from evowiki.assistant.session import MemorySessionStore
from evowiki.assistant.wiki import (
    APPLY_ACTION,
    NOT_ORGANIZED,
    ApplyRefused,
    WikiApplier,
    WikiData,
    WikiTools,
    added_role,
)
from evowiki.belief.aspects import Declare
from evowiki.bot.actions import STALE_DETAIL
from evowiki.goals.contracts import Band, SectionRole
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.service.app import create_app
from evowiki.wikiapp.events import PageSaved, UsageRecord, WikiEvent
from evowiki.wikiapp.hand_edit import hand_edit_flow
from evowiki.wikiapp.page import set_pin
from evowiki.wikiapp.page_version import page_fingerprint, section_fingerprint
from evowiki.wikiapp.repo import CONFLICT_DETAIL, Commit, MemoryRepo, WikiConflict
from evowiki.wikiapp.wiki_schemas import HandEditIn
from tests.assistant.test_wiki_adapter import gateway
from tests.service.test_assistant_api import events
from tests.wikiapp.partial_write_check import assert_partial_write
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_organize import add_page, page1, run, script_page
from tests.wikiapp.test_page import declare, organized

U1 = {"X-EvoWiki-User": "u1"}


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


@pytest.fixture
def page(env):
    """u1's organized page: s1 organizer, s2 explain (c1.procedure shaky, c1.interpret none),
    s3 recap (c2.term none), s4 check."""
    return organized(env)


def clock():
    return NOW


_N = itertools.count(1)


def app_ids(prefix: str) -> str:
    """The applier's ids: never reused within the test session, never equal to E1 flow ids."""
    return f"{prefix}_a{next(_N):04d}"


def applier(repo, **kw):
    return WikiApplier(repo, clock, app_ids, **kw)


def add(pid, after=None, cells=(), text="z=-1 表示比均值低一个标准差。", heading="补充：z=-1", cue="z=-1 表示什么？",
        answer="比均值低一个标准差。"):
    return ProposalDraft(kind="add_section", page_id=pid, section_id=after, heading=heading,
                         cells=tuple(cells), text=text, why="刚问过", cue=cue, answer=answer)


def rewrite(pid, sid, text="新的讲法。", heading="z 分数怎么读（新）", cells=(), cue="新的问题？", answer="新的答案。"):
    return ProposalDraft(kind="rewrite_section", page_id=pid, section_id=sid, heading=heading,
                         cells=tuple(cells), text=text, why="讲得更清楚", cue=cue, answer=answer)


def saves(repo):
    return [e.body for e in repo.wiki_log("u1") if isinstance(e.body, PageSaved)]


def refused(repo, draft, match):
    before = len(repo.wiki_log("u1"))
    with pytest.raises(ApplyRefused, match=match):
        applier(repo).apply("u1", draft)
    assert len(repo.wiki_log("u1")) == before          # nothing written


# ---- add_section ----

def test_add_after_a_section_inserts_the_assistant_text(env, page):
    repo = env[0]
    n = len(saves(repo))
    applied = applier(repo).apply("u1", add(page, after="s2", cells=["c1.interpret"]))
    (new,) = applied
    assert new.startswith("sec_a")
    new_saves = saves(repo)[n:]
    assert len(new_saves) == 1 and new_saves[0].action == APPLY_ACTION
    # the same commit records usage, as E1's feedback flows do
    last = [e.body.type for e in repo.wiki_log("u1")[-3:]]
    assert last == ["page_saved", "section_logs_saved", "usage_added"]
    assert repo.load("u1").state.usage[-1] == UsageRecord(action=APPLY_ACTION, page=page,
                                                          heading="补充：z=-1")
    rec = repo.load("u1").state.pages[page]
    p = rec.page
    assert [s.id for s in p.outline.sections] == ["s1", "s2", new, "s3", "s4"]
    s = p.outline.sections[2]
    assert (s.heading, s.role, s.cells, s.why, s.notes) == (
        "补充：z=-1", SectionRole.EXPLAIN, ("c1.interpret",), "刚问过", "刚问过")
    t = p.texts[new]
    assert (t.cue, t.answer, t.body) == ("z=-1 表示什么？", "比均值低一个标准差。", "z=-1 表示比均值低一个标准差。")
    st = p.state(new)
    assert not st.folded and not st.pinned and new in p.states
    assert rec.changed == {new: "inserted"}
    old = saves(repo)[n - 1].record.page
    assert all(p.outline.sections[i] == s for i, s in
               zip((0, 1, 3, 4), old.outline.sections, strict=True))   # the rest untouched
    assert {k: v for k, v in p.texts.items() if k != new} == old.texts
    assert_partial_write(repo, page, "assistant", {new}, producer="edit")


def test_add_without_anchor_appends(env, page):
    repo = env[0]
    applied = applier(repo).apply("u1", add(page, text="纯正文。"))
    p = repo.load("u1").state.pages[page].page
    assert p.outline.sections[-1].id == applied[0]
    assert p.outline.sections[-1].role is SectionRole.EXPLAIN        # no cells -> explain
    assert p.texts[applied[0]].body == "纯正文。"


def test_added_role_rule():
    bands = {"a": Band.KNOWN, "b": Band.LIKELY, "c": Band.SHAKY, "d": Band.GAP, "e": Band.NONE}
    assert added_role((), bands) is SectionRole.EXPLAIN
    assert added_role(("a", "b"), bands) is SectionRole.LINE
    for weak in "cde":
        assert added_role(("a", weak), bands) is SectionRole.EXPLAIN


def test_add_with_only_known_cells_is_a_line(env, page):
    repo = env[0]
    declare(env, "know", "c2.term")
    applied = applier(repo).apply("u1", add(page, after="s3", cells=["c2.term"]))
    s = repo.load("u1").state.pages[page].page.outline.sections[3]
    assert s.id == applied[0] and s.role is SectionRole.LINE


def test_add_next_to_a_pinned_section_is_fine(env, page):
    repo, _, ids = env
    set_pin(repo, "u1", page, "s2", True, now=NOW, new_id=ids)
    applied = applier(repo).apply("u1", add(page, after="s2"))
    assert repo.load("u1").state.pages[page].page.outline.sections[2].id == applied[0]


def test_explaining_a_known_cell_is_applied(env, page):
    """Was guardrail ③ (会的不能展开讲); now guidance only, never a refusal of the user's edit."""
    repo = env[0]
    declare(env, "know", "c2.term")
    applied = applier(repo).apply("u1", add(page, cells=["c2.term", "c1.procedure"]))
    sec = next(s for s in repo.load("u1").state.pages[page].page.outline.sections
               if s.id == applied[0])
    assert sec.role is SectionRole.EXPLAIN and sec.cells == ("c2.term", "c1.procedure")


def test_add_refusals(env, page):
    repo, _, ids = env
    refused(repo, add(page, after="s9"), "没有这一段：s9")
    refused(repo, add("pg_nope"), "没有这个页面：pg_nope")
    raw = add_page(repo, ids, title="还没生成")
    refused(repo, add(raw), NOT_ORGANIZED)
    refused(repo, add(page, cells=["c9.term"]), "不存在的格子 c9.term")
    with pytest.raises(ApplyRefused, match="没有这个用户"):
        applier(repo).apply("ghost", add(page))


# ---- rewrite_section ----

def test_rewrite_replaces_body_keeps_role_cells_and_cue(env, page):
    repo = env[0]
    old = repo.load("u1").state.pages[page].page
    applied = applier(repo).apply("u1", rewrite(page, "s2"))
    assert applied == ("s2",)
    rec = repo.load("u1").state.pages[page]
    p = rec.page
    s = p.outline.sections[1]
    assert (s.id, s.heading, s.role, s.cells) == (
        "s2", "z 分数怎么读（新）", SectionRole.EXPLAIN, ("c1.procedure", "c1.interpret"))
    assert p.texts["s2"].body == "新的讲法。"
    assert (p.texts["s2"].cue, p.texts["s2"].answer) == ("新的问题？", "新的答案。")   # never the old section's card
    assert [x.id for x in p.outline.sections] == [x.id for x in old.outline.sections]
    assert rec.changed == {"s2": "rewritten"} and saves(repo)[-1].action == APPLY_ACTION


def test_rewrite_with_new_cells(env, page):
    repo = env[0]
    applier(repo).apply("u1", rewrite(page, "s3", text="标准差……", cue="σ 量什么？", answer="离散程度。",
                                      cells=["c2.term", "c1.procedure"]))
    p = repo.load("u1").state.pages[page].page
    assert p.outline.sections[2].cells == ("c2.term", "c1.procedure")
    assert p.outline.sections[2].role is SectionRole.RECAP
    assert (p.texts["s3"].cue, p.texts["s3"].answer, p.texts["s3"].body) == ("σ 量什么？", "离散程度。", "标准差……")


def test_rewrite_refusals(env, page):
    repo, _, _ = env
    refused(repo, rewrite(page, "s9"), "没有这一段：s9")
    refused(repo, rewrite(page, "s4"), "确认题不能改写")
    refused(repo, rewrite(page, "s2", cells=["c9.term"]), "不存在的格子 c9.term")


def test_a_locked_section_takes_a_rewrite_the_user_confirmed_and_stays_locked(env, page):
    repo, _, ids = env
    set_pin(repo, "u1", page, "s3", True, now=NOW, new_id=ids)
    applier(repo).apply("u1", rewrite(page, "s3"))
    p = repo.load("u1").state.pages[page].page
    assert p.texts["s3"].body == "新的讲法。" and p.state("s3").pinned


def test_pre_existing_page_violations_do_not_block_an_edit(env, page):
    """K moved after organizing (s2 now explains known cells): the page already breaks ③, but an
    edit that adds no violation of its own is still applied."""
    repo = env[0]
    declare(env, "know", "c1.procedure", "c1.interpret")
    applied = applier(repo).apply("u1", add(page, after="s1"))
    assert repo.load("u1").state.pages[page].page.outline.sections[1].id == applied[0]


def test_rewrite_with_new_heading_keeps_pre_existing_section_violations_out(env, page):
    """s2 already explains known cells (③ twice). A rewrite that renames it (and moves it one
    place down, after the add above it) is not refused for those: same role and cells."""
    repo = env[0]
    declare(env, "know", "c1.procedure", "c1.interpret")
    applier(repo).apply("u1", add(page, after="s1"))            # s2 is now the 3rd section
    assert applier(repo).apply("u1", rewrite(page, "s2", heading="完全不同的标题")) == ("s2",)
    s2 = repo.load("u1").state.pages[page].page.outline.sections[2]
    assert (s2.id, s2.heading) == ("s2", "完全不同的标题")
    # a NEW unusable cell on the touched section is still refused
    refused(repo, rewrite(page, "s2", cells=["c1.procedure", "c1.interpret", "c9.term"]),
            "不存在的格子 c9.term")


LINE_PAGE = {
    "profile": "p", "assumed": [],
    "sections": [
        {"id": "s1", "heading": "开篇", "role": "organizer", "cells": [], "why": "w"},
        {"id": "s2", "heading": "z 分数怎么读", "role": "explain",
         "cells": ["c1.procedure", "c1.interpret"], "why": "w"},
        {"id": "s3", "heading": "顺带一句", "role": "line", "cells": [], "why": "w"},
    ],
}


def test_coverage_never_refuses_the_users_edit(env):
    """User ruling 2026-10-10: how an edit fits K is not a refusal. c2.term (now 不会) on a line
    section applies; so does dropping c1.procedure (shaky) from the only section teaching it."""
    repo = env[0]
    pid = page1(env)
    declare(env, "know", "c2.term")
    script_page(env[1], LINE_PAGE)
    run(env, pid)
    declare(env, "dont", "c2.term")
    assert applier(repo).apply("u1", rewrite(pid, "s3", heading="标准差一句",
                                             cells=["c2.term"])) == ("s3",)
    s3 = repo.load("u1").state.pages[pid].page.outline.sections[2]
    assert (s3.role, s3.cells) == (SectionRole.LINE, ("c2.term",))
    assert applier(repo).apply("u1", rewrite(pid, "s2", cells=["c1.interpret"])) == ("s2",)
    s2 = repo.load("u1").state.pages[pid].page.outline.sections[1]
    assert s2.cells == ("c1.interpret",)


def test_a_headed_line_section_keeps_its_heading_when_rewritten_short(env):
    """The note shows a LINE section of several paragraphs under its heading; made one paragraph
    it stays headed (not a bullet with the heading in bold). A one-paragraph LINE stays a line."""
    repo = env[0]
    pid = page1(env)
    script_page(env[1], LINE_PAGE)
    run(env, pid)
    role = lambda: repo.load("u1").state.pages[pid].page.outline.sections[2].role
    applier(repo).apply("u1", rewrite(pid, "s3", text="只一句"))
    assert role() is SectionRole.LINE
    applier(repo).apply("u1", rewrite(pid, "s3", text="第一段\n\n第二段"))
    assert role() is SectionRole.LINE
    applier(repo).apply("u1", rewrite(pid, "s3", text="短了"))
    assert role() is SectionRole.EXPLAIN


# ---- conflicts ----

class Raced:
    """MemoryRepo whose first `races` commits lose to another writer (an aspect event lands
    between the snapshot and the commit, and the commit raises WikiConflict: a K event alone no
    longer conflicts in the repo, so the conflict the applier retries on is raised here)."""

    def __init__(self, inner: MemoryRepo, races: int):
        self.inner, self.races, self.commits = inner, races, 0

    def load(self, user_id):
        return self.inner.load(user_id)

    def commit(self, snap, c, *, once=None):
        self.commits += 1
        if self.commits <= self.races:
            other = self.inner.load(snap.user_id)
            self.inner.commit(other, Commit(aspect=[Declare(
                event_id=f"race_{self.commits}", ts=NOW, cell="c2.term", value="dont")]))
            raise WikiConflict()
        return self.inner.commit(snap, c, once=once)


def test_conflict_reloads_and_retries(env, page):
    repo = Raced(env[0], races=1)
    n = len(saves(env[0]))
    applied = applier(repo).apply("u1", add(page, after="s2"))
    assert repo.commits == 2 and len(saves(env[0])) == n + 1
    assert env[0].load("u1").state.pages[page].page.outline.sections[2].id == applied[0]


def test_conflict_every_time_gives_up(env, page):
    repo = Raced(env[0], races=99)
    n = len(saves(env[0]))
    with pytest.raises(ApplyRefused, match=CONFLICT_DETAIL):
        applier(repo, tries=3).apply("u1", add(page))
    assert repo.commits == 3 and len(saves(env[0])) == n


# ---- expect_version / extra (action ledger) ----

def _version(repo, page):
    return page_fingerprint(repo.load("u1").state.pages[page])


def _edit_page(repo, page, body="别人改的"):
    snap = repo.load("u1")
    rec = snap.state.pages[page]
    sec = rec.page.outline.sections[0].id
    text = rec.page.texts[sec].model_copy(update={"body": body})
    rec2 = rec.model_copy(update={"page": rec.page.model_copy(
        update={"texts": {**rec.page.texts, sec: text}})})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=f"edit_{body}", ts=NOW,
                                             body=PageSaved(record=rec2, action="edit"))]))


def test_expect_version_matching_applies_and_runs_extra(env, page):
    repo, calls = env[0], []
    applier(repo).apply("u1", add(page), expect_version=_version(repo, page),
                        extra=[lambda conn: calls.append(conn)])
    assert calls == [None]                       # MemoryRepo hands the effect no connection


def _sec(repo, page, k=0):
    return repo.load("u1").state.pages[page].page.outline.sections[k].id


def _sv(repo, page, sid):
    return section_fingerprint(repo.load("u1").state.pages[page], sid)


def test_a_section_rewritten_after_the_proposal_refuses_and_writes_nothing(env, page):
    """The section the proposal rewrites was itself edited since: applying would overwrite it."""
    repo = env[0]
    sid = _sec(repo, page)
    version, sv = _version(repo, page), _sv(repo, page, sid)
    _edit_page(repo, page)                                   # edits section 0
    before, calls = len(repo.wiki_log("u1")), []
    with pytest.raises(ApplyRefused) as e:
        applier(repo).apply("u1", rewrite(page, sid), expect_version=version, section_version=sv,
                            extra=[lambda conn: calls.append(conn)])
    assert str(e.value) == STALE_DETAIL and calls == []
    assert len(repo.wiki_log("u1")) == before
    # a proposal from before section versions (none recorded) is not re-anchored either
    with pytest.raises(ApplyRefused):
        applier(repo).apply("u1", rewrite(page, _sec(repo, page, 1)), expect_version=version)


def test_a_change_that_still_fits_the_changed_page_is_re_anchored(env, page):
    """Another section changed since the proposal: an added section (its anchor still there) and a
    rewrite of an untouched section go onto the page as it is now, the other edit kept."""
    repo = env[0]
    version = _version(repo, page)
    other = _sec(repo, page, 1)
    sv = _sv(repo, page, other)
    _edit_page(repo, page, body="用户之后的新版本")               # edits section 0 only
    applier(repo).apply("u1", add(page, after=other), expect_version=version)
    applier(repo).apply("u1", rewrite(page, other), expect_version=version, section_version=sv)
    rec = repo.load("u1").state.pages[page]
    assert rec.page.texts[_sec(repo, page)].body == "用户之后的新版本"
    assert rec.page.texts[other].body.endswith("新的讲法。")
    assert any(s.heading == "补充：z=-1" for s in rec.page.outline.sections)


def test_conflict_retry_rechecks_the_version(env, page):
    """The first commit loses a race to an edit of the section it rewrites; the retry must
    refuse, not overwrite."""

    class PageRaced(Raced):
        def commit(self, snap, c):
            self.commits += 1
            if self.commits == 1:
                _edit_page(self.inner, page, body="赛跑中的编辑")
            return self.inner.commit(snap, c)

    repo = PageRaced(env[0], races=1)
    sid = _sec(env[0], page)
    before = len(env[0].wiki_log("u1"))
    with pytest.raises(ApplyRefused) as e:
        applier(repo).apply("u1", rewrite(page, sid), expect_version=_version(env[0], page),
                            section_version=_sv(env[0], page, sid))
    assert str(e.value) == STALE_DETAIL and repo.commits == 1
    assert len(env[0].wiki_log("u1")) == before + 1      # only the racing edit


# ---- over HTTP ----

def client(repo, tmp_path, script):
    gw, _ = gateway(tmp_path, script)
    deps = TurnDeps(gateway=gw, tools=WikiTools(repo), store=MemorySessionStore(),
                    data=WikiData(repo, clock), new_id=Ids(), clock=clock)
    svc = AssistantService(deps, applier(repo))
    return TestClient(create_app(SimpleNamespace(assistant=svc)))


def proposal(c, pid):
    sid = c.post("/assistant/sessions", json={"page_id": pid}, headers=U1).json()["session_id"]
    r = c.post(f"/assistant/sessions/{sid}/messages", json={"text": "加进页面"}, headers=U1)
    return next(d for e, d in events(r) if e == "proposal")["proposal_id"]


def test_accept_over_http_applies_or_reports_the_reason(env, page, tmp_path):
    repo = env[0]
    c = client(repo, tmp_path, [
        ChatTurn(tool_calls=(ToolCall(id="c1", name="propose_page_edit", args={
            "kind": "add_section", "section_id": "s2", "heading": "补充：z=-1", "cue": "z=-1 表示什么？", "answer": "低一个标准差。",
            "text": "z=-1 表示比均值低一个标准差。", "why": "刚问过", "paragraphs": []}),)),
        ChatTurn(text="建议加一段"),
        # the second session on this page first closes the first one (one summary call)
        '{"handoff": {"summary": "问了 z，建议加一段"}, "learning": {}}',
        ChatTurn(tool_calls=(ToolCall(id="c2", name="propose_page_edit", args={
            "kind": "rewrite_section", "section_id": "s3", "heading": "标准差", "cue": "σ 量什么？", "answer": "离散程度。",
            "text": "新讲法", "why": "更清楚", "paragraphs": []}),)),
        ChatTurn(text="建议改写")])
    ok = proposal(c, page)
    out = c.post(f"/assistant/proposals/{ok}/accept", headers=U1).json()
    assert out["status"] == "accepted" and len(out["applied"]) == 1, out
    p = repo.load("u1").state.pages[page].page
    assert p.outline.sections[2].id == out["applied"][0]
    assert p.texts[out["applied"][0]].body == "z=-1 表示比均值低一个标准差。"

    bad = proposal(c, page)
    hand_edit_flow(repo, "u1", page, HandEditIn(removed=["s3"], summary="删了"), now=NOW, new_id=env[2])
    out = c.post(f"/assistant/proposals/{bad}/accept", headers=U1).json()
    assert out == {"proposal_id": bad, "status": "failed", "applied": [], "detail": "没有这一段：s3",
                   "action_id": "", "can_undo": False}
