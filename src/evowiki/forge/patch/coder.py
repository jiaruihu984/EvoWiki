"""The L2 coder (design §4; plan P3 T9): the same `agent.loop.run_loop` as the L1 coder, with the L2
tools over a real git worktree (`patch.tools`), the deep tier (the caller's converse) and its own
prompt. SWE-bench practice: reproduce first (a new failing test), then the smallest fix, then the
related old tests. The coder's claims are never trusted: `build_patch` re-runs everything on a
fresh checkout of the committed change (DGM). No connection is held here; the caller runs this
inside a durable (virtual) workflow."""

from collections.abc import Callable, Generator
from dataclasses import replace
from typing import TYPE_CHECKING

from evowiki.agent.loop import Converse, LoopResult, run_loop
from evowiki.agent.spec import AgentSpec, LoopLimits, RequestState
from evowiki.agent.tools import ToolContext
from evowiki.agent.trace import TraceSink, finish_failed
from evowiki.forge import limits
from evowiki.forge.models import Evidence, PatchRequest
from evowiki.forge.patch.tools import PatchHolder, build_tools
from evowiki.forge.project_context import PROJECT_CONTEXT
from evowiki.llm.chat import ChatMessage, ChatRequest, ToolDef

if TYPE_CHECKING:
    from evowiki.harness.pack import HarnessPack

PROMPT_VERSION = "forge-patch@v3"
PURPOSE = "forge.patch"
AGENT = "forge"
NUDGE = "请继续用工具完成，做完调用 submit。"

LIMITS = LoopLimits(max_tool_calls=limits.L2_MAX_TOOL_CALLS, on_cap="stop", text_is_answer=False,
                    max_nudges=2, nudge_text=NUDGE, tool_error_max=2000)

SYSTEM = """你是 EvoWiki 的代码修复工程师。你在一个 git 工作区里修 EvoWiki 的 Python 代码里的一个问题，\
用提供的工具读代码、改文件、跑测试，做完调用 submit。你只能通过工具做事；回复里的文字不会被执行。

## 做法（必须按顺序）
1. 先看开场的根因、相关文件和项目地图；直接读相关代码核对。只有定位不够时才用 grep 和 outline 扩大搜索。
2. 先写一个能复现问题的测试：新建文件 `tests/forge_<短名>/test_<内容>.py`（同目录放一个空的 `__init__.py`），\
用 run_cmd 跑 `pytest <这个文件>`，确认它现在失败。这个测试在改动前必须失败、改动后必须通过。
3. 再改最少的代码让它通过。
4. 然后跑相关的已有测试（`pytest tests/<包名>`），确认没有改坏别的。
5. 全部通过后调用 submit，用一两句话说明改了什么、新测试证明了什么。

## 能改什么
- 只能改 `src/evowiki/` 下的 deep、assistant、bot、render、content、sources、wiki、goals、nodes 这几个目录，\
以及在 `tests/` 下新建测试文件。
- 已有的测试、store/、service/、llm/、harness/、forge/、配置文件和依赖都不能改，这类修改会被拒绝。
- 不要加依赖，不要加数据库迁移。

## 规则
- 改文件前先读它；工具结果里的错误（找不到文本、语法错误、路径被拒绝等）说明这次修改没有生效，按提示改正后重试。
- 绝不为了通过而削弱测试，绝不改已有测试。
- run_cmd 只能运行 pytest（tests/ 下的文件）、ruff check、git diff/status/log。
- 保持改动小而清楚，不顺手重构，不加用不到的功能。
"""


def limits_for(pack: "HarnessPack | None") -> LoopLimits:
    """LIMITS, with the tool-call cap from the user's harness pack when one is given: its
    `H4.forge.patch.max_tool_calls` may only lower L2_MAX_TOOL_CALLS (`harness.apply.forge_limits`)."""
    if pack is None:
        return LIMITS
    from evowiki.harness.apply import forge_limits
    return replace(LIMITS, max_tool_calls=forge_limits(pack, "patch", limits.L2_MAX_TOOL_CALLS))


def first_message(request: PatchRequest, evidence: Evidence | None = None, *,
                  max_tool_calls: int = limits.L2_MAX_TOOL_CALLS) -> ChatMessage:
    hints = "\n".join(f"- {p}" for p in request.paths_hint)
    parts = [f"## 要解决的问题和已诊断的根因\n{request.goal}",
             f"## 出错的情况和轨迹结论\n{request.failing_case}"]
    if hints:
        parts.append(f"## 可能相关的文件\n{hints}")
    if evidence is not None and evidence.note.strip():
        parts.append(f"## 证据\n{evidence.note.strip()}")
    parts.append(PROJECT_CONTEXT)
    parts.append("先写复现测试并看到它失败，再改代码让它通过，然后跑相关的已有测试，最后 submit。"
                 f"工具调用最多 {max_tool_calls} 次：读代码不超过三分之一，找到改动的地方就动手。"
                 "次数用完后，已有改动仍会交给构建检查；新测试必须在改动前失败、改动后通过，"
                 "相关旧测试也必须通过，后续门控照常判定。")
    return ChatMessage(role="user", text="\n\n".join(parts))


def coder_spec(holder: PatchHolder, *, request: PatchRequest, trace: TraceSink, run_id: str,
               sent: Callable[[ChatRequest], ChatRequest],
               loop_limits: LoopLimits = LIMITS) -> AgentSpec:
    tools = build_tools(holder)
    defs = tuple(ToolDef(name=t.spec.name, description=t.spec.description,
                         parameters=t.spec.parameters) for t in tools)

    def request_of(state: RequestState) -> ChatRequest:
        return ChatRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=request.title,
                           system=SYSTEM, messages=state.messages, tools=defs,
                           tool_choice=state.tool_choice)

    return AgentSpec(name=AGENT, tools=tools, limits=loop_limits, request=request_of,
                     finished=lambda: holder.submitted is not None, sent=sent, trace=trace,
                     run_id=run_id)


def _drain(gen: Generator[object, None, LoopResult]) -> LoopResult:
    while True:
        try:
            next(gen)          # ToolStarted / ToolFinished: nobody streams a forge run
        except StopIteration as stop:
            return stop.value


def run_patch_coder(request: PatchRequest, converse: Converse, *, holder: PatchHolder,
                    trace: TraceSink, run_id: str, user_id: str,
                    sent: Callable[[ChatRequest], ChatRequest] = lambda r: r,
                    evidence: Evidence | None = None,
                    pack: "HarnessPack | None" = None) -> LoopResult:
    """Run the coder to the end over `holder` (the worktree is changed in place; `build_patch`
    decides). With a `run_id` the run is traced as agent "forge" for `user_id`; an exception
    finishes the trace as failed and is re-raised. `pack`: the user's harness pack, which may
    lower the tool-call cap (`limits_for`); None = the forge's own cap."""
    spec = coder_spec(holder, request=request, trace=trace, run_id=run_id, sent=sent,
                      loop_limits=limits_for(pack))
    ctx = ToolContext(user_id=user_id, session_id=f"forge:{run_id or request.title}")
    if run_id:
        trace.start(run_id, user_id=user_id, agent=AGENT, subject=request.title, pack_version=0,
                    input={"request": request.model_dump(mode="json")})
    try:
        result = _drain(run_loop(spec, converse, ctx, [first_message(
            request, evidence, max_tool_calls=spec.limits.max_tool_calls)]))
    except BaseException as exc:
        if run_id:
            finish_failed(trace, run_id, exc)
        raise
    if run_id:
        trace.finish(run_id, stop=result.stop, error=None)
    return result
