"""harness/tool_names.TOOLS is static (a pack is checked without importing an agent); this keeps it
equal to the tools and arguments the agents really build, with every optional tool present."""

from unittest.mock import MagicMock

from evowiki.assistant.tools import build_tools as assistant_tools
from evowiki.assistant.tools.actions import build_action_tools
from evowiki.assistant.tools.check import AskCheckTool
from evowiki.assistant.tools.evidence import RecordEvidenceTool
from evowiki.assistant.tools.read_more import ReadDeps, build_read_tools
from evowiki.assistant.tools.rough import AskRoughTool, SkipDiagnosisTool
from evowiki.assistant.turn_skills import inert_tools
from evowiki.deep.contracts import RefinePlan
from evowiki.deep.tools import Workspace, build_tools
from evowiki.harness.tool_names import TOOLS
from evowiki.wikiapp.refine_spec import TeachingSpec
from tests.deep.fakes import PLAN, FakeChecker
from tests.deep.test_series_prompt import _page


def _names(tools):
    return {t.spec.name: tuple(sorted(t.spec.parameters.get("properties") or {}))
            for t in tools}


def _deep():
    ws = Workspace(_page("p1", "学会 z-score", ("zscore",), ("zscore",)),
                   components={"c": MagicMock()},          # with components: place_component too
                   image_search=MagicMock())                # with an image search: find / place_image
    plan = RefinePlan(known="已知", observable=PLAN["observable"],
                      one_at_a_time=tuple(PLAN["one_at_a_time"]), contrast=PLAN["contrast"],
                      spoken_answer=PLAN["spoken_answer"])
    return build_tools(ws, user_id="u1", page_id="p1", title="t", kview=MagicMock(), names={},
                       teaching=TeachingSpec(version=1, items=()), checker=FakeChecker(),
                       known=plan.known, sources=MagicMock())   # with materials: the source tools too


def _assistant(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    more = [*build_read_tools(ReadDeps(MagicMock(), MagicMock())),
            *build_action_tools(MagicMock()),
            RecordEvidenceTool(MagicMock(), MagicMock(), MagicMock()),
            AskCheckTool(MagicMock(), MagicMock(), MagicMock(), MagicMock()),
            AskRoughTool(MagicMock(), MagicMock(), MagicMock(), MagicMock()),
            SkipDiagnosisTool(MagicMock(), MagicMock(), MagicMock(), MagicMock())]
    built = assistant_tools(pages=MagicMock(), kview=MagicMock(), more=more,
                            sources=MagicMock(), sandbox=MagicMock(),
                            memory=MagicMock()).values()  # + materials, run_python, memory
    # stage 3: the Skill / confirmation tools the turn adds (assistant.turn_skills)
    s3 = inert_tools()
    return [*built, *s3]


def test_registry_matches_the_tools_the_agents_build(monkeypatch):
    # TOOLS still lists the Bot's old planner tools ("bot": suggest, no_action): harness/ is a
    # frozen dir and keys.py names them, but the Bot builds no tools any more
    assert {k: dict(v) for k, v in TOOLS.items() if k != "bot"} == {
        # plan_page (stage 4) is not in the frozen registry (harness/ is read-only): an H2 key for
        # it cannot be stored, and it has no H3 toggle (it is never off)
        "deep": {k: v for k, v in _names(_deep()).items() if k != "plan_page"},
        "assistant": _names(_assistant(monkeypatch)),
    }
