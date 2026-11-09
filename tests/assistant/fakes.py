"""Shared fakes and scripted scenarios for the reading assistant tests."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from evowiki.agent.tools import ProposalDraft, Source, ToolContext, ToolResult, ToolSpec
from evowiki.assistant.ports import PageSectionView, PageView
from evowiki.assistant.session import SessionInfo, SessionStore
from evowiki.goals.context import PageDigest

T0 = datetime(2026, 10, 8, 9, tzinfo=UTC)
INFO = SessionInfo(session_id="s1", user_id="u1", page_id="p1", note_concept_id=None,
                   created_at=T0)
DRAFT = {"kind": "add_section", "page_id": "p1", "heading": "补充", "text": "正文", "why": "刚问过"}


def run(store: SessionStore) -> SessionStore:
    """One answered turn with a tool call and a (later rejected) proposal, then a failed turn."""
    store.create(INFO)
    a = lambda i, t, p: store.append("s1", f"e{i}", t, p, T0 + timedelta(seconds=i))
    a(1, "user", {"message_id": "m1", "text": "问一", "selection": {"text": "选中", "heading": "h"}})
    a(2, "tool", {"message_id": "m1", "call_id": "c1", "name": "read_k", "effect": "read",
                  "args": {}, "ok": True, "content": "K", "sources": []})
    a(3, "proposal", {"proposal_id": "pr1", "message_id": "m1", "draft": DRAFT})
    a(4, "assistant", {"message_id": "a1", "text": "答一", "sources": [], "proposals": ["pr1"],
                       "tool_calls": 1, "llm_calls": 2})
    a(5, "observed", {"message_id": "m1", "observations": 1, "changes": []})
    a(6, "user", {"message_id": "m2", "text": "问二", "selection": None})
    a(7, "turn_error", {"message_id": "m2", "code": "llm_unavailable", "detail": "x"})
    a(8, "decision", {"proposal_id": "pr1", "status": "rejected", "applied": [], "detail": ""})
    return store


# ---- turn loop fakes (Task 7) ----

PAGE = PageView(page_id="p1", title="z 分数", concepts=("z_score", "mean"), sections=(
    PageSectionView(section_id="s1", heading="公式", body="z = (x − μ)/σ，用均值和标准差做单位。"),
    PageSectionView(section_id="s2", heading="解读", body="z = 2 表示高出两个标准差。")))


class Data:
    def __init__(self):
        self.pages_touched = 0

    def page(self, user_id, page_id):
        return PAGE if page_id == "p1" else None

    def pages(self, user_id):
        return [PageDigest(page_id="p2", title="均值", concepts=("mean",), summary="μ 的算法")]

    def k_text(self, user_id, concept_ids):
        return "- z_score.interpret 解读结果：不会"

    def guidelines(self, user_id):
        return ["先给公式再讲直觉"]

    def language(self, user_id):
        return "zh"

    def prereqs(self, user_id, concept_ids):
        return ("mean",)

    def named_nodes(self, user_id, text):
        return ("n_grinder 磨豆机：把咖啡豆磨成粉的工具",) if "磨豆机" in text else ()


@dataclass
class FakeTool:
    name: str
    effect: str
    result: ToolResult | None = None
    raises: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    ctxs: list[ToolContext] = field(default_factory=list)   # what each call was given

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=f"{self.name} 工具",
                        parameters={"type": "object", "properties": {}}, effect=self.effect)

    def run(self, args, ctx: ToolContext) -> ToolResult:
        self.calls.append(args)
        self.ctxs.append(ctx)
        if self.raises:
            raise self.raises
        return self.result or ToolResult(content=f"{self.name} 的结果")


def tools():
    return [FakeTool("read_k", "read"),
            FakeTool("web_search", "network", ToolResult(
                content="维基：z 分数……", sources=(Source(title="维基", url="https://w/z"),))),
            FakeTool("propose_page_edit", "propose", ToolResult(
                content="已生成", proposal=ProposalDraft(kind="add_section", page_id="p1",
                                                      heading="补充", text="正文", why="刚问过")))]


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n:03d}"


def ungrounded(tools):
    """The action tools without their `Grounded` wrapper: tests of what a tool does, not of the
    learner's-words check (tests/assistant/test_grounded_actions.py)."""
    from evowiki.assistant.tools.actions import Grounded
    return [t.tool if isinstance(t, Grounded) else t for t in tools]


def peek_then(text: str = "答"):
    """A scripted chat model that first calls read_k (so a test reads the turn's ToolContext from
    that FakeTool's `ctxs`), then answers `text`."""
    from evowiki.llm.chat import ChatTurn, ToolCall

    def answer(m, r):
        if any(x.role == "tool" for x in r.messages):
            return ChatTurn(text=text)
        return ChatTurn(tool_calls=(ToolCall(id="peek", name="read_k", args={}),))
    return answer
