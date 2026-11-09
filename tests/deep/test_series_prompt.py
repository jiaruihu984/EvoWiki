"""The page writer sees the series note and can read the other pages of its path (B2 Task 4, deep@v14)."""

from dataclasses import replace

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.deep.agent import write_prompt
from evowiki.deep.tools import Workspace, build_tools
from evowiki.goals.contracts import OutlineSection, PageOutline, SectionRole
from evowiki.goals.fold import Page, states_for
from evowiki.goals.outline_context import other_pages_text
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.refine_apply import refine_input
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.series import SeriesNote, SeriesPage, SeriesUpdate, merge, render
from tests.deep.fakes import FakeChecker, planned
from tests.deep.test_writer import outline_input
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized
from tests.wikiapp.test_paths import big_goal

NOTE = SeriesNote(example="抽奖箱里的红球和白球",
                  notation=("用 p 表示红球的比例",),
                  pages=(SeriesPage(page_id="x", title="第一页", summary="用抽奖箱引入总体与样本"),))
ASK = "要求：沿用上面的贯穿例子"
FIRST = "这是系列的第一页"


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def _page(pid: str, goal: str, concepts, targets) -> Page:
    outline = PageOutline(page_id=pid, goal=goal, concepts=concepts, targets=targets,
                          sections=(OutlineSection(id="s1", heading="先看问题",
                                                   role=SectionRole.EXPLAIN),))
    return Page(outline=outline, states=states_for(outline), texts={}, snapshot={},
                unresolved_conflicts=(), conflicts=())


def _path_page_input(env, index: int, *, note: SeriesNote | None = None, written_first=False):
    repo, _, ids = env
    res, _ = big_goal(env)
    pid, first = None, None
    for i in range(index):
        pid = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id
        first = first or pid
    snap = repo.load("u1")
    state = snap.state
    if note is not None:
        state = state.model_copy(update={"series": {res.path_id: note}})
    if written_first and first != pid:
        r = state.pages[first]
        state = state.model_copy(update={"pages": {
            **state.pages, first: r.model_copy(update={"page": _page(first, r.goal, r.concepts,
                                                                      r.targets)})}})
    snap = replace(snap, state=state)
    rec = snap.state.pages[pid]
    return refine_input(snap, pid, NOW, WikiSpecSource(repo),
                        page=_page(pid, rec.goal, rec.concepts, rec.targets), mode="page"), first


def _prompts(inp) -> list[str]:
    """The first write message before plan_page and after it."""
    return [write_prompt(inp, (), Workspace(inp.page)), write_prompt(inp, (), planned(inp))]


def test_path_page_with_a_note_shows_it_in_both_prompts(env):
    inp, _ = _path_page_input(env, 2, note=NOTE)
    assert inp.series == render(NOTE)
    for p in _prompts(inp):
        assert "系列笔记（这个学习路径前面各页的约定）" in p
        assert render(NOTE) in p and ASK in p and "调用 finish 时填 series" in p
        assert FIRST not in p


def test_first_page_without_a_note_says_so(env):
    inp, _ = _path_page_input(env, 1)
    assert inp.series == ""
    for p in _prompts(inp):
        assert FIRST in p and ASK in p


def test_page_on_no_path_has_no_series_block(env):
    inp = outline_input(env, organized(env))
    assert inp.series == "" and inp.other_pages == ()
    for p in _prompts(inp):
        assert "系列笔记" not in p


def test_read_page_opens_a_sibling_page_of_the_path(env):
    inp, first = _path_page_input(env, 2, note=NOTE, written_first=True)
    assert [wp.page_id for wp in inp.other_pages] == [first]
    tools = build_tools(Workspace(inp.page), user_id=inp.user_id, page_id=inp.page_id,
                        title=inp.title, kview=inp.kview, names=inp.names, teaching=inp.teaching,
                        checker=FakeChecker(), known=inp.known, other_pages=inp.other_pages)
    read = next(t for t in tools if t.spec.name == "read_page")
    res = read.run({"page_id": first}, ToolContext(user_id=inp.user_id, session_id="t", page_id=inp.page_id))
    assert res.error is None and f"page_id={first}" in res.content and "s1" in res.content


def test_read_page_opens_every_page_the_prompt_lists(env):
    """A one-page path (the on-demand 学这个) is on no series; the prompt still lists his other pages."""
    repo, _, _ = env
    a, b = organized(env), organized(env)
    snap = repo.load("u1")
    inp = outline_input(env, b)
    assert inp.path == "" and f"#p:{a}" in other_pages_text(page_spec(snap.state, snap.k, b))
    tools = build_tools(Workspace(inp.page), user_id="u1", page_id=b, title=inp.title, kview=inp.kview,
                        names=inp.names, teaching=inp.teaching, checker=FakeChecker(), known=inp.known,
                        other_pages=inp.other_pages)
    read = next(t for t in tools if t.spec.name == "read_page")
    assert read.run({"page_id": a}, ToolContext(user_id="u1", session_id="t", page_id=b)).error is None


def test_pages_without_content_are_not_offered(env):
    inp, _ = _path_page_input(env, 2, note=NOTE)         # page 1 exists but has no content
    assert inp.other_pages == ()


def _page1_rewritten(env, mode: str):
    """Page 1 of the path, already in the note (its own term 「总体」 and line), written again."""
    repo, _, ids = env
    res, _ = big_goal(env)
    p1 = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id
    p2 = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id
    note = merge(None, SeriesUpdate(example="抽奖箱", terms=("总体",), summary="第一页讲总体。"),
                 page_id=p1, title="第一页")
    note = merge(note, SeriesUpdate(terms=("样本",), summary="第二页讲样本。"), page_id=p2,
                 title="第二页")
    snap = repo.load("u1")
    snap = replace(snap, state=snap.state.model_copy(update={"series": {res.path_id: note}}))
    rec = snap.state.pages[p1]
    return refine_input(snap, p1, NOW, WikiSpecSource(repo), mode=mode,
                        page=_page(p1, rec.goal, rec.concepts, rec.targets))


@pytest.mark.parametrize("mode", ["page", "rewrite"])
def test_a_page_does_not_see_its_own_terms_as_already_taught(env, mode):
    inp = _page1_rewritten(env, mode)
    assert "总体" not in inp.series and "第一页讲总体" not in inp.series
    assert "样本" in inp.series and "第二页讲样本" in inp.series and "抽奖箱" in inp.series


@pytest.mark.parametrize(("mode", "shown"), [("page", True), ("rewrite", False)])
def test_the_finish_series_line_only_on_a_page_write(env, mode, shown):
    for p in _prompts(_page1_rewritten(env, mode)):
        assert "系列笔记" in p and ("调用 finish 时填 series" in p) is shown


def test_page_3_can_link_every_earlier_page(env):
    repo, _, ids = env
    res, _ = big_goal(env)
    p1, p2, p3 = (next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id
                  for _ in range(3))
    note = merge(None, SeriesUpdate(example="抽奖箱", terms=("总体", "样本"), summary="讲总体。"),
                 page_id=p1, title="第一页")
    note = merge(note, SeriesUpdate(terms=("均值",), summary="讲均值。"), page_id=p2,
                 title="第二页")
    snap = repo.load("u1")
    snap = replace(snap, state=snap.state.model_copy(update={"series": {res.path_id: note}}))
    rec = snap.state.pages[p3]
    inp = refine_input(snap, p3, NOW, WikiSpecSource(repo), mode="page",
                       page=_page(p3, rec.goal, rec.concepts, rec.targets))
    for p in _prompts(inp):
        assert f"- [第一页](#p:{p1})：讲总体。" in p and f"- [第二页](#p:{p2})：讲均值。" in p
        assert "第一页：总体、样本；第二页：均值" in p
