"""The assistant's side of possibly conflicting memories (memory/conflicts.py; the 「冲突」 Skill):
`read_memory_conflicts` lists his open groups; `resolve_memory_conflict` settles one in the chat
(「处理」 on the 个性化 card opens a turn with intent resolve_memory_conflicts that reads them
first). His words this turn are his yes and the evidence (`quote`, decided in code like
remember_preference: a quote not from this message is refused); it takes effect at once, and
keeping one / replacing them is undoable (the ledger row's 撤销, the `undo` tool)."""

from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from evowiki.agent.tool_helpers import fail, run_guarded, spec
from evowiki.agent.tools import ToolContext, ToolResult
from evowiki.assistant.contracts import MemoryConflictOut
from evowiki.memory.conflicts import CATEGORY_ZH, where_text

SOURCE_ZH = {"chat": "聊天里说的", "consolidation": "从对话里整理的", "rule_page": "规则页手写的",
             "profile": "档案里写的"}
NONE_OPEN = "现在没有待确认的记忆"
OFF = "没能处理待确认的记忆"
NOT_NOW = "quote 要一字不改地抄他这一轮说的话：只有他这一轮自己说了怎么处理才能执行"
ONE_WAY = "keep 和 replace_with 只能给一个"
BOTH = "both"


def conflicts_text(groups: list[MemoryConflictOut], zone: str = "Asia/Shanghai") -> str:
    """The open groups as the model reads them (and as the intent's first call shows them)."""
    if not groups:
        return NONE_OPEN
    out = [f"待确认的记忆 {len(groups)} 组："]
    for n, g in enumerate(groups, 1):
        out.append(f"\n第 {n} 组 [{g.conflict_id}]：{g.why}")
        for e in g.items:
            when = e.date.astimezone(ZoneInfo(zone)).date().isoformat() if e.date else "日期不详"
            where = f"，{where_text(e)}" if e.where else ""
            said = f"；原话「{e.quote}」" if e.quote else ""
            out.append(f"- [{e.id}]（{CATEGORY_ZH[e.category]}，{SOURCE_ZH[e.source]}，{when}"
                       f"{where}）{e.text}{said}")
    return "\n".join(out)


class _NoArgs(BaseModel):
    pass


class ReadMemoryConflictsTool:
    """`read_memory_conflicts()`: his open groups (read only)."""

    def __init__(self, deps: Any):
        self._d = deps
        self.spec = spec("read_memory_conflicts", "列出待确认的记忆：可能互相矛盾的几组（他的规则、档案、"
                         "定下的决定），每组带每条的原文、类别、日期、来源和 id。", _NoArgs, "read")

    @property
    def args_model(self) -> type[BaseModel]:
        return _NoArgs

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        svc = getattr(self._d, "conflicts", None)
        if svc is None:
            return fail(OFF)
        return run_guarded(_NoArgs, args,
                           lambda a: ToolResult(content=conflicts_text(svc.open(ctx.user_id), self._d.timezone(ctx.user_id))))


class ResolveConflictArgs(BaseModel):
    conflict_id: str = Field(min_length=1, max_length=200, description="read_memory_conflicts 里的组 id")
    keep: str = Field(default="", max_length=200, description=(
        "他要保留哪一条：那条的 id；他说两条都保留就写 both"))
    replace_with: str = Field(default="", max_length=300, description=(
        "他没选其中一条、而是说了一条新的（例如「简单的公式直接给，复杂的先举例」）：就用他原话里的那句；"
        "这一组原来的几条都会撤下"))
    section: str = Field(default="", max_length=20, description=(
        "replace_with 是关于他自己的事（我是谁 / 想学什么 / 怎么学比较好）时写那一栏；讲解规则留空"))
    domain: str = Field(default="", max_length=60, description="replace_with 的规则只用在这个领域时写")
    quote: str = Field(min_length=1, max_length=400, description=(
        "他这一轮说怎么处理的原话，一字不改地照抄（例如「留第一条」「两条都要」）"))


class ResolveMemoryConflictTool:
    """`resolve_memory_conflict(conflict_id, keep | replace_with, quote, section?, domain?)`:
    `ConflictService.resolve` (keep one / both, or his new wording), grounded in his words this
    turn; at once, undoable (except keep both, which changes nothing)."""

    def __init__(self, deps: Any):
        self._d = deps
        self.spec = spec(
            "resolve_memory_conflict", "按他这一轮说的处理一组待确认的记忆：保留其中一条（keep=那条的 id，"
            "其余撤下）、两条都保留（keep=both），或换成他说的新说法（replace_with，原来的都撤下）。"
            "当场生效，保留一条和换新说法都可以撤销。", ResolveConflictArgs, "propose", "action",
            acts=True, tier="direct")

    @property
    def args_model(self) -> type[BaseModel]:
        return ResolveConflictArgs

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(ResolveConflictArgs, args, lambda a: self._run(a, ctx))

    def _run(self, a: ResolveConflictArgs, ctx: ToolContext) -> ToolResult:
        from evowiki.assistant.tools.actions import card_of, remember_route
        from evowiki.memory.conflicts import Replacement

        svc = getattr(self._d, "conflicts", None)
        if svc is None:
            return fail(OFF)
        keep, new = a.keep.strip(), " ".join(a.replace_with.split())
        if bool(keep) == bool(new):
            return fail(ONE_WAY)
        route = remember_route(a.quote, ctx)
        if isinstance(route, str):
            return fail(route)
        prov, ev, _ = route
        if prov != "explicit_current":
            return fail(NOT_NOW)
        try:
            if new:
                status, action_id = svc.resolve(ctx.user_id, a.conflict_id, replace_with=Replacement(
                    text=new, quote=ev.quote or "", origin_ref=f"{ctx.session_id}/{ctx.message_id}",
                    section=a.section.strip() or None, domain=a.domain.strip() or None))
            elif keep == BOTH:
                status, action_id = svc.resolve(ctx.user_id, a.conflict_id, keep_both=True)
            else:
                status, action_id = svc.resolve(ctx.user_id, a.conflict_id, keep=keep)
        except LookupError:
            return fail("这组记忆已经处理过或不存在")
        except ValueError as e:
            return fail(f"没处理成：{e}")
        if action_id is None:
            return ToolResult(content="两条都保留了，以后不再提这一组。")
        ledger = getattr(self._d, "ledger", None)
        row = ledger.get(ctx.user_id, action_id) if ledger is not None else None
        title = row.summary if row is not None else status
        return ToolResult(content=f"已处理（{action_id}，可以用 undo 撤销）：{title}",
                          action=card_of(row) if row is not None else None)
