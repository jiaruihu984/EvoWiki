"""read_page in the writer must show the draft workspace: a section write_section accepted reads back
as written (outline flag and section text), and a new section appears in the outline."""

from evowiki.agent.tools import ToolContext
from evowiki.deep.contracts import RefinePlan
from evowiki.deep.tools import Workspace, build_tools
from evowiki.goals.contracts import OutlineSection, PageOutline, SectionRole
from evowiki.goals.fold import Page, states_for
from evowiki.teach.contracts import TeachingSpec
from tests.deep.fakes import PLAN, FakeChecker

CTX = ToolContext(user_id="u1", session_id="t", page_id="p1")


def _tools():
    outline = PageOutline(page_id="p1", goal="g", concepts=("c1",), targets=("c1",),
                          sections=(OutlineSection(id="s1", heading="先看问题", role=SectionRole.EXPLAIN),))
    page = Page(outline=outline, states=states_for(outline), texts={}, snapshot={})
    ws = Workspace(page, required=("s1",))
    plan = RefinePlan(known="学习者没学过", observable=PLAN["observable"], one_at_a_time=tuple(PLAN["one_at_a_time"]),
                      contrast=PLAN["contrast"], spoken_answer=PLAN["spoken_answer"])
    tools = build_tools(ws, user_id="u1", page_id="p1", title="T", kview=None,
                        names={}, teaching=TeachingSpec.model_validate({"version": 1, "items": []}),
                        checker=FakeChecker(), known=plan.known)
    ws.plan = plan                        # plan_page accepted
    return {t.spec.name: t for t in tools}


def test_read_page_shows_a_section_write_section_accepted():
    t = _tools()
    w = t["write_section"].run({"section_id": "s1", "heading": "先看问题", "role": "explain",
                                "depth": "example", "cue": "问一点", "answer": "一句答案", "body": "正文 ABC"}, CTX)
    assert w.error is None and w.content.startswith("已写入草稿")
    one = t["read_page"].run({"section_id": "s1"}, CTX)
    assert one.error is None and "正文 ABC" in one.content, one.content
    outline = t["read_page"].run({}, CTX)
    assert "未写" not in outline.content, outline.content


def test_read_page_outline_lists_a_new_section():
    t = _tools()
    t["write_section"].run({"section_id": "s0", "heading": "开场", "role": "organizer",
                            "depth": "example", "cue": "问一点", "answer": "一句答案", "body": "开场白"}, CTX)
    assert "s0" in t["read_page"].run({}, CTX).content
    assert t["read_page"].run({"section_id": "s0"}, CTX).error is None
