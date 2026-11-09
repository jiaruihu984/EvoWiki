"""Declarative hook steps (design 2026-10-21 §3.3; rulings R4 / R7 / R8): an H9 `delegate` or an
H11 `self_check` step runs as a small agent on `agent.loop.run_loop` with a subset of the main
agent's read tools, and returns text the main agent puts into its own context (`context.notes` /
`context.must_fix`). The caller picks `converse` (and so the tier) and decides where the output
goes; this module never picks a model and never blocks anything — a self-check's findings are
must-fix lines for the existing extra round, not a gate.

A hook model call that raises makes the main run fail (R7): nothing here catches it."""

from collections.abc import Callable, Collection, Generator, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from evowiki.agent.loop import Converse, LoopResult, run_loop
from evowiki.agent.spec import AgentSpec, LoopLimits, RequestState
from evowiki.agent.tools import Tool, ToolContext, ToolResult, ToolSpec
from evowiki.agent.trace import NO_TRACE, TraceSink, finish_failed
from evowiki.harness.keys import HOOK_OUTPUT_MAX, REPORT_NOTE_MAX
from evowiki.harness.pack import HarnessInvalid
from evowiki.harness.steps import HookStep
from evowiki.llm.chat import ChatMessage, ChatRequest, ToolDef
from evowiki.llm.models import LLMCall

PROMPT_VERSION = "hook@v1"

# model-facing only (approved 2026-10-21); never shown to the user
HOOK_SYSTEM = (
    "你在为另一个写作者或助手准备材料，自己不写页面内容。"
    "只用给你的工具，按下面的说明做。"
    "有 report 工具时，做完调用 report 交回结果：没发现问题 ok 为 true，"
    "每个问题写一条 notes。没有 report 工具时，直接用简短的文字回答，写给那个写作者看。")

CLIP_MARK = "…（已截断）"
REPORT_RECEIVED = "已收到"


@dataclass(frozen=True)
class HookOutput:
    at: str
    notes: tuple[str, ...]          # delegate: one entry (its text, clipped); self_check: report.notes
    ok: bool                        # self_check: report.ok (True when it found nothing); delegate: True
    calls: tuple[LLMCall, ...]


class _ReportArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: StrictBool
    notes: list[str] = Field(default_factory=list)


_REPORT_SPEC = ToolSpec(
    name="report",
    description="交回检查结果。ok：没发现问题时为 true；notes：每个要改的问题一条，"
                f"每条不超过 {REPORT_NOTE_MAX} 字。",
    parameters={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "description": "没发现问题时为 true"},
            "notes": {"type": "array", "description": "每个要改的问题一条",
                      "items": {"type": "string", "minLength": 1,
                                "maxLength": REPORT_NOTE_MAX}},
        },
        "required": ["ok", "notes"],
        "additionalProperties": False,
    },
    effect="read",
    # "draft", not the derived "read": it only fills the output being built, a pure function of its
    # args. A "read" tool runs as a recorded durable step, so on a workflow replay its body would
    # not run, the report would be lost and `finished` would never become true (a different step
    # order on recovery). As "draft" it runs in the workflow body on every replay.
    category="draft",
)


class _Report:
    """The self-check's closing tool: records (ok, notes) in `got` and answers 已收到. Bad args
    raise, so the loop hands the error back to the model."""

    spec = _REPORT_SPEC

    def __init__(self, got: list[tuple[bool, tuple[str, ...]]]) -> None:
        self.got = got

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            a = _ReportArgs.model_validate(args)
        except ValidationError as exc:
            raise ValueError(f"report 的参数不对：{exc.errors()[0]['msg']}") from exc
        notes = tuple(n.strip() for n in a.notes)
        if any(not n or len(n) > REPORT_NOTE_MAX for n in notes):
            raise ValueError(f"notes 每条要有内容，不超过 {REPORT_NOTE_MAX} 字")
        self.got.append((a.ok, notes))
        return ToolResult(content=REPORT_RECEIVED)


def report_tool(got: list[tuple[bool, tuple[str, ...]]]) -> Tool:
    return _Report(got)


def clip(text: str) -> str:
    """The text stripped and cut to HOOK_OUTPUT_MAX characters (R8: a context bound)."""
    t = text.strip()
    return t if len(t) <= HOOK_OUTPUT_MAX else t[:HOOK_OUTPUT_MAX] + CLIP_MARK


def clip_notes(notes: Sequence[str]) -> tuple[str, ...]:
    """Notes whose joined text (one per line) stays within HOOK_OUTPUT_MAX (R8): the note that
    crosses the bound is cut and marked, later ones are dropped."""
    out: list[str] = []
    room = HOOK_OUTPUT_MAX
    for n in notes:
        if len(n) > room:
            out.append(n[:room] + CLIP_MARK)
            break
        out.append(n)
        room -= len(n) + 1
        if room < 0:
            break
    return tuple(out)


def _drain(gen: Generator[object, None, LoopResult]) -> LoopResult:
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


def check_tools(step: HookStep, available: Collection[str]) -> None:
    """HarnessInvalid when the step names a tool the main agent's run lacks."""
    missing = [n for n in step.tools if n not in available]
    if missing:
        raise HarnessInvalid(f"{step.at} 这一步要用的工具这次运行里没有：{'、'.join(missing)}")


def run_hook(step: HookStep, *, brief: str, tools: Sequence[Tool], converse: Converse,
             ctx: ToolContext, sent: Callable[[ChatRequest], ChatRequest] = lambda r: r,
             trace: TraceSink = NO_TRACE, run_id: str = "", user_id: str = "",
             pack_version: int = 0) -> HookOutput:
    """One hook step as a small agent (spec §3.3). Tools: the subset of `tools` named in
    step.tools (a name missing from `tools` raises HarnessInvalid — the main agent's run lacks
    it), none when step.max_tool_calls is 0, plus `report` for a self_check. Request: purpose
    f"hook.{step.at}", PROMPT_VERSION, HOOK_SYSTEM, one user message = instruction + brief.
    Limits: force_answer at the cap, text is the answer. A self_check's cap is
    step.max_tool_calls + 1: calling `report` counts against the cap, so one slot is reserved for
    it (otherwise reads spending the whole cap would force a text answer and the model could never
    report); its first `report` ends the step.

    Output — delegate: the final text, clipped (R8), as one note; ok True. Self-check: the last
    report's (ok, notes), clipped as a whole to HOOK_OUTPUT_MAX; without a report, its text as
    the note with ok False (R7), or nothing with ok True when the text is empty. Traced as its own run (agent f"hook.{step.at}") when
    `run_id` is set; a model error finishes that run failed and propagates."""
    by_name = {t.spec.name: t for t in tools}
    check_tools(step, by_name)
    checking = step.kind == "self_check"
    got: list[tuple[bool, tuple[str, ...]]] = []
    own: list[Tool] = [by_name[n] for n in step.tools] if step.max_tool_calls > 0 else []
    if checking:
        own.append(report_tool(got))
    cap = step.max_tool_calls + 1 if checking else step.max_tool_calls   # + report's slot
    defs = tuple(ToolDef(name=t.spec.name, description=t.spec.description,
                         parameters=t.spec.parameters) for t in own)
    purpose = f"hook.{step.at}"
    subject = ctx.page_id or ctx.session_id

    def request(state: RequestState) -> ChatRequest:
        return ChatRequest(purpose=purpose, prompt_version=PROMPT_VERSION, subject=subject,
                           system=HOOK_SYSTEM, messages=state.messages, tools=defs,
                           tool_choice=state.tool_choice)

    spec = AgentSpec(name=purpose, tools=own,
                     limits=LoopLimits(max_tool_calls=cap, on_cap="force_answer",
                                       text_is_answer=True),
                     request=request, finished=lambda: bool(got), sent=sent, trace=trace,
                     run_id=run_id)
    if run_id:
        trace.start(run_id, user_id=user_id, agent=purpose, subject=subject,
                    pack_version=pack_version,
                    input={"at": step.at, "kind": step.kind, "tier": step.tier,
                           "tools": list(step.tools), "max_tool_calls": step.max_tool_calls,
                           "instruction": step.instruction, "brief": brief})
    try:
        result = _drain(run_loop(spec, converse, ctx,
                                 [ChatMessage(role="user", text=f"{step.instruction}\n\n{brief}")]))
    except BaseException as exc:
        if run_id:
            finish_failed(trace, run_id, exc)
        raise
    if run_id:
        trace.finish(run_id, stop=result.stop, error=None)
    text = clip(result.text)
    if checking and got:
        ok, notes = got[-1]
        notes = clip_notes(notes)
    else:
        # a self-check without report and with empty text gives ok=True on purpose (R7): there is
        # nothing to act on, and it is not a failure (its trace records the empty answer)
        ok, notes = (not (checking and text)), ((text,) if text else ())
    return HookOutput(at=step.at, notes=notes, ok=ok, calls=result.calls)
