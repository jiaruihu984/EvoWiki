"""Tool categories (B1b Task 2): the page writer's tools and the assistant's carry the right one."""

from unittest.mock import MagicMock

from evowiki.assistant.tools import build_tools as assistant_tools
from evowiki.deep.contracts import RefinePlan
from evowiki.deep.tools import Workspace, build_tools
from evowiki.wikiapp.refine_spec import TeachingSpec
from tests.deep.fakes import PLAN, FakeChecker
from tests.deep.test_series_prompt import _page

EXPECTED = {"plan_page": "draft", "read_k": "read", "read_page": "read", "read_spec": "read", "write_section": "draft",
            "place_component": "draft", "read_ref": "read", "draw_visual": "draft", "write_block": "draft", "render_check": "draft",
            "finish": "draft"}


def test_deep_tools_categories():
    ws = Workspace(_page("p1", "学会 z-score", ("zscore",), ("zscore",)),
                   components={"c": MagicMock()})
    plan = RefinePlan(known="已知", observable=PLAN["observable"],
                      one_at_a_time=tuple(PLAN["one_at_a_time"]), contrast=PLAN["contrast"],
                      spoken_answer=PLAN["spoken_answer"])
    tools = build_tools(ws, user_id="u1", page_id="p1", title="t", kview=MagicMock(), names={},
                        teaching=TeachingSpec(version=1, items=()), checker=FakeChecker(), known=plan.known)
    assert {t.spec.name: t.spec.category for t in tools} == EXPECTED


def test_assistant_tools_categories(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    tools = assistant_tools(pages=MagicMock(), kview=MagicMock())
    assert {n: t.spec.category for n, t in tools.items()} == {
        "search_wiki": "read", "read_page": "read", "read_k": "read",
        "web_search": "read", "fetch_url": "read", "propose_page_edit": "action"}
