"""plan_page (deep@v28, LLM-call consolidation stage 4): the writer's first tool call carries the
plan and, for a page without an outline, the outline, checked by code. Scripted models only."""

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.deep.tools import (
    NO_PLAN,
    OutlineSetup,
    PlanPageTool,
    Workspace,
    WriteSectionTool,
    build_tools,
)
from evowiki.deep.writer import PageWriter
from evowiki.goals.contracts import SectionRole
from evowiki.goals.guardrails import page_bands
from evowiki.teach.contracts import TeachingSpec
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.refine_apply import refine_input
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from tests.deep.fakes import (
    PLAN,
    DeepScript,
    FakeChecker,
    finish,
    library,
    section,
    turn,
)
from tests.goals.fakes_k import ZSCORE_OUTLINE, zscore_k, zscore_spec
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_organize import OUTLINE, page1

CTX = ToolContext(user_id="u1", session_id="t", page_id="p-z")
CHECK = {"id": "q1", "heading": "确认一下", "role": "check", "cells": ["z_score.interpret"],
         "check": {"cell": "z_score.interpret", "stem": "z = 2 的分数比均值高多少？",
                   "options": ["两个标准差", "两分", "两倍均值"], "answer": 0}}


def _ws() -> Workspace:
    return Workspace(None, page_cells=tuple(page_bands(zscore_spec(), zscore_k())))


def _tool(setup=True, on_outline=None):
    ws = _ws()
    out = OutlineSetup(spec=zscore_spec(), kview=zscore_k()) if setup else None
    return ws, PlanPageTool(ws, known="确定会：均值·术语", setup=out, on_outline=on_outline)


def test_an_accepted_outline_sets_the_plan_page_and_required_and_reaches_on_outline():
    seen = []
    ws, tool = _tool(on_outline=seen.append)
    got = tool.run({"plan": PLAN, "outline": ZSCORE_OUTLINE}, CTX)
    assert got.error is None and "大纲和规划已记下" in got.content
    assert ws.plan is not None and ws.plan.known == "确定会：均值·术语"
    assert ws.plan.observable == PLAN["observable"]
    assert ws.page is ws.planned and seen == [ws.page]
    assert ws.page.texts == {} and ws.page.snapshot                       # the K snapshot
    assert ws.required == tuple(s.id for s in ws.page.outline.sections
                                if s.role is not SectionRole.CHECK)
    assert all(s.check.verified for s in ws.page.outline.sections if s.check is not None)


def test_an_unusable_outline_comes_back_with_every_problem_and_sets_nothing():
    seen = []
    ws, tool = _tool(on_outline=seen.append)
    bad = {"sections": [{"id": "s1", "heading": "x", "role": "explain", "cells": ["nope.term"]}]}
    got = tool.run({"plan": PLAN, "outline": bad}, CTX)
    assert got.error is not None and "nope.term" in got.content
    assert ws.plan is None and ws.page is None and seen == []
    # a role outside the six is refused by the schema (the tool's argument check)
    wrong = {"sections": [{"id": "s1", "heading": "x", "role": "lecture", "cells": []}]}
    got = tool.run({"plan": PLAN, "outline": wrong}, CTX)
    assert got.error is not None and "role" in got.content and ws.plan is None and seen == []


def test_a_failing_outline_listener_is_not_swallowed_and_nothing_is_set():
    def boom(page):
        raise RuntimeError("stream down")
    ws, tool = _tool(on_outline=boom)
    got = tool.run({"plan": PLAN, "outline": ZSCORE_OUTLINE}, CTX)
    assert got.error is not None and "stream down" in got.content
    assert ws.plan is None and ws.page is None and ws.required == ()


def test_a_plan_alone_after_an_accepted_outline_keeps_the_outline():
    ws, tool = _tool()
    assert tool.run({"plan": PLAN, "outline": ZSCORE_OUTLINE}, CTX).error is None
    page = ws.page
    got = tool.run({"plan": {**PLAN, "contrast": "换一组对照"}}, CTX)
    assert got.error is None and ws.page is page and ws.plan.contrast == "换一组对照"


def test_a_misaligned_check_is_sent_back_and_a_settled_one_is_dropped_and_reported():
    ws, tool = _tool()
    misaligned = {**ZSCORE_OUTLINE, "sections": [
        s for s in ZSCORE_OUTLINE["sections"] if "z_score.interpret" not in s.get("cells", [])
    ] + [CHECK]}
    got = tool.run({"plan": PLAN, "outline": misaligned}, CTX)
    assert got.error is not None and "不在任何 explain / compare 段里讲过" in got.content
    settled = {**CHECK, "id": "q2", "cells": ["mean.term"],
               "check": {**CHECK["check"], "cell": "mean.term"}}
    ok = {**ZSCORE_OUTLINE, "sections": [*ZSCORE_OUTLINE["sections"], settled]}
    got = tool.run({"plan": PLAN, "outline": ok}, CTX)
    assert got.error is None and "去掉了这些确认题" in got.content
    assert "q2" not in {s.id for s in ws.page.outline.sections}


def test_a_page_with_an_outline_takes_the_plan_only_and_one_without_needs_it():
    ws, tool = _tool(setup=False)
    assert tool.run({"plan": PLAN, "outline": ZSCORE_OUTLINE}, CTX).error is not None
    assert tool.run({"plan": PLAN}, CTX).error is None and ws.plan is not None
    ws, tool = _tool()
    assert "outline 必填" in tool.run({"plan": PLAN}, CTX).content and ws.plan is None


def test_the_args_are_typed_and_a_bad_plan_is_refused():
    ws, tool = _tool()
    props = tool.spec.parameters["properties"]
    assert set(props) == {"outline", "plan"}
    assert "不用当场练" in str(tool.spec.parameters)                 # field meanings in the schema
    got = tool.run({"plan": {**PLAN, "one_at_a_time": []}, "outline": ZSCORE_OUTLINE}, CTX)
    assert got.error is not None and ws.plan is None
    assert tool.run({"plan": PLAN, "outline": "{}"}, CTX).error is not None     # no JSON strings


def test_write_tools_refuse_until_a_plan_is_accepted():
    ws = _ws()
    tools = {t.spec.name: t for t in build_tools(
        ws, user_id="u1", page_id="p-z", title="t", kview=zscore_k(), names={},
        teaching=TeachingSpec(version=1, items=()), checker=FakeChecker(), known="k",
        setup=OutlineSetup(spec=zscore_spec(), kview=zscore_k()))}
    args = {"section_id": "s1", "heading": "h", "role": "explain", "depth": "quick", "cue": "c?", "answer": "一句答案",
            "body": "b"}
    for name, a in (("write_section", args), ("finish", {"self_check": []}),
                    ("write_block", {"block_id": "b1", "section_id": "s1", "why": "w",
                                     "block": {}})):
        assert tools[name].run(a, CTX).error == NO_PLAN, name
    assert tools["read_page"].run({}, CTX).error is not None              # no page yet
    assert tools["plan_page"].run({"plan": PLAN, "outline": ZSCORE_OUTLINE}, CTX).error is None
    sid = ws.required[0]
    assert tools["write_section"].run({**args, "section_id": sid}, CTX).error is None


def test_a_re_plan_replaces_the_outline_until_the_first_section_is_written():
    ws, tool = _tool()
    tool.run({"plan": PLAN, "outline": ZSCORE_OUTLINE}, CTX)
    shorter = {**ZSCORE_OUTLINE, "sections": ZSCORE_OUTLINE["sections"][:3]}
    assert tool.run({"plan": PLAN, "outline": shorter}, CTX).error is None
    assert len(ws.page.outline.sections) == 3
    s = ws.required[0]
    WriteSectionTool(ws).run({"section_id": s, "heading": "h", "role": "explain",
                              "depth": "quick", "cue": "c?", "answer": "一句答案", "body": "b"}, CTX)
    got = tool.run({"plan": PLAN, "outline": ZSCORE_OUTLINE}, CTX)
    assert got.error is not None and "不能再改" in got.content and len(ws.page.outline.sections) == 3


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def test_a_scripted_page_write_outlines_then_writes_every_section(env):
    """plan_page (with the outline) → write_section × n → finish, no other model call."""
    pid = page1(env)
    snap = env[0].load("u1")
    spec = page_spec(snap.state, snap.k, pid, user_id="u1")
    inp = refine_input(snap, pid, NOW, WikiSpecSource(env[0]), mode="page", outline_spec=spec)
    assert inp.needs_outline and inp.page is None and inp.required == ()
    ds = DeepScript().plan(outline=OUTLINE).write(
        turn(section("s1", role="organizer", depth="quick", heading="从你会的出发")),
        turn(section("s2")), turn(section("s3", role="recap", depth="quick")), turn(finish()))
    outlined = []
    res = PageWriter(llm=ds.llm(), checker=FakeChecker(), library=library()).write(
        inp, on_outline=outlined.append)
    assert [p.outline.page_id for p in outlined] == [pid]
    assert res.outcome.page is outlined[0]
    assert [s.section_id for s in res.outcome.draft.sections] == ["s1", "s2", "s3"]
    assert ds.count("deep.plan") == 0 and ds.count("deep.write") == 5 and ds.pending() == {}
    first = next(r for r in ds.requests if r.purpose == "deep.write").messages[0].text
    assert "这一页还没有大纲。首次调用 plan_page 时，同时提交 outline。" in first
    assert "规划要求" in first and "出题规则" in first and "⟦" not in first
    assert "这些段都要写" not in first and "提交后检查目标内容是否覆盖" in first
    second = [r for r in ds.requests if r.purpose == "deep.write"][1]
    assert second.messages[0].text == first                 # one first message per round
