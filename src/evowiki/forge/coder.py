"""The coding agent of the tool forge (design 2026-10-22 §4, plan T6): an `AgentSpec("forge")` on
the shared `agent.loop.run_loop` with the Claude-Code-style workspace tools of `coding_tools`.
LATM maker practice: the examples from the evidence are given up front and the agent writes the
tests from them first. The coder's own claims are never trusted: `build` re-runs everything.

`examples.json` is the evidence, not the coder's to change: `write_file` / `edit_file` on it come
back as error results. No connection is held here; the caller runs this inside a durable
(virtual) workflow so `run_tests` / `try_tool` are recorded steps."""

import json
from collections.abc import Callable, Generator, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from evowiki.agent.loop import Converse, LoopResult, run_loop
from evowiki.agent.spec import AgentSpec, LoopLimits, RequestState
from evowiki.agent.tool_helpers import fail
from evowiki.agent.tools import Tool, ToolContext, ToolResult
from evowiki.agent.trace import TraceSink, finish_failed
from evowiki.forge.coding_tools import WorkspaceHolder, build_tools
from evowiki.forge.limits import CODER_MAX_NUDGES, CODER_MAX_TOOL_CALLS
from evowiki.forge.models import ToolExample, ToolRequest
from evowiki.forge.sandbox import Sandbox
from evowiki.forge.workspace import Workspace
from evowiki.llm.chat import ChatMessage, ChatRequest, ToolDef

if TYPE_CHECKING:
    from evowiki.harness.pack import HarnessPack

PROMPT_VERSION = "forge-tool@v2"   # v2: host-call section + cassettes in examples.json (P2 T5)
PURPOSE = "forge.tool"
AGENT = "forge"
READ_ONLY = frozenset({"examples.json"})
STUB = "def run(args: dict) -> dict:\n    raise NotImplementedError\n"
START_FILES = ("tool.py", "examples.json")
NUDGE = "请继续用工具完成，做完调用 submit。"

LIMITS = LoopLimits(max_tool_calls=CODER_MAX_TOOL_CALLS, on_cap="stop", text_is_answer=False,
                    max_nudges=CODER_MAX_NUDGES, nudge_text=NUDGE, tool_error_max=2000)

SYSTEM = """你是 EvoWiki 的工具锻造工程师。你在一个只有几个文件的内存工作区里，为 EvoWiki 的 agent 写一个小工具，\
用提供的工具读、写、改文件，在沙箱里跑测试，做完调用 submit。你只能通过工具做事；回复里的文字不会被执行。

## 工具的形态（必须遵守）
- `tool.py`：定义 `def run(args: dict) -> dict`，参数和返回值都是 JSON 能表示的 dict。纯函数：同样的参数永远得到同样的结果。
- 只用 Python 标准库，不 import 第三方包。
- 不访问网络、不读写文件、不起子进程、不读环境变量、不用随机数或当前时间。沙箱会拦下这些操作，工具会直接失败。
- 出错时不要抛异常吞掉信息：参数不合法就返回说明问题的 dict（例如 {"ok": false, "error": "..."}），除非样例要求别的形状。
- `test_tool.py`：`import tool`，写若干 `def test_xxx():`，函数体里用普通 assert（不用 pytest 的 fixture 和插件）。
- `schema.json`：参数的 JSON Schema，顶层 `"type": "object"`，有 `"description"`（一两句话说明这个工具做什么、什么时候用），\
`"properties"` 里每个参数有类型和说明，`"required"` 列出必填参数。
- `examples.json` 是证据里来的样例（args → expect），只读，不能改。构建时会逐个按 schema 校验 args，再要求 run(args) 的输出和 expect 完全相等。

## 做法
1. 先读 `examples.json`，弄清输入输出的确切形状（键名、类型、大小写）。
2. 先写测试：把每个样例写成一个测试，再补几个边界情况（空输入、非法输入）。
3. 写 `schema.json`，再在 `tool.py` 里实现 `run`。
4. 用 run_tests 跑测试；失败就读失败详情，改代码（小改动用 edit_file，old_string 要唯一），再跑，直到全部通过。\
不要为了通过而改测试里来自样例的期望值。
5. 用 try_tool 拿样例参数试跑一次，确认输出和 expect 一样。
6. 全部通过后调用 submit，用一两句话说明做了什么。

## 规则
- 改文件前先读它；工具结果里的错误（找不到文本、语法错误等）说明这次修改没有生效，按提示改正后重试。
- 文件名只能是 xxx.py、schema.json、examples.json、README.md。
- 保持简单：一个清楚的实现，不加用不到的功能。
"""


class _ReadOnlyGuard:
    """A write tool that refuses the read-only files (the evidence) and otherwise delegates."""

    def __init__(self, tool: Tool):
        self._tool = tool
        self.spec = tool.spec

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if args.get("path") in READ_ONLY:
            return fail(f"{args['path']} 是证据里来的样例，只读，不能改")
        return self._tool.run(args, ctx)


def coder_tools(holder: WorkspaceHolder) -> list[Tool]:
    return [_ReadOnlyGuard(t) if t.spec.name in ("write_file", "edit_file") else t
            for t in build_tools(holder)]


def _example(e: ToolExample) -> dict[str, Any]:
    """An example as the coder sees it; with host calls, its cassette shows their exact shapes."""
    d = e.model_dump(mode="json")
    if not e.host:
        d.pop("host", None)
    return d


def examples_json(request: ToolRequest) -> str:
    return json.dumps([_example(e) for e in request.examples],
                      ensure_ascii=False, indent=2) + "\n"


def host_section(request: ToolRequest, host_docs: Mapping[str, str] | None = None) -> str:
    """The prompt section on host calls; empty when the tool may not call any host tool."""
    if not request.host_tools:
        return ""
    docs = host_docs or {}
    tools = "\n".join(f"- `{t}`：{docs.get(t, '').strip() or '（无说明）'}"
                      for t in request.host_tools)
    first = request.host_tools[0]
    return ("## 读取已有数据（只读）\n"
            f"这个工具可以调用 agent 已有的只读工具：\n{tools}\n\n"
            f"用法：`import host`，然后 `text = host.call(\"{first}\", {{...参数...}})`，返回值是文本（str）。"
            "只能调用上面列出的工具，同一组参数总按同样的顺序调用（工具会被重新执行，调用顺序必须每次一样）。"
            "调用失败会抛出 `host.HostError`，需要时捕获它并返回说明问题的 dict。"
            "一次运行最多调用几次，不要循环大量读取。\n"
            "测试里没有真实数据：在 test_tool.py 里先 `host.install(lambda tool, args: \"...\")` 提供假的返回文本，"
            "再调用 tool.run。样例里的 `host` 列表是这个样例实际发生的调用（tool、args、返回内容），"
            "构建时会严格按它回放：调用的工具、参数、次数和顺序都必须和记录一致。\n\n")


def start_workspace(request: ToolRequest, seed: Mapping[str, str] | None = None) -> Workspace:
    """The coder's starting files: the seed (a parent candidate's files) or the stub `tool.py`;
    `examples.json` is always the request's examples."""
    files = dict(seed) if seed else {"tool.py": STUB}
    files["examples.json"] = examples_json(request)
    return Workspace.of(files)


def first_message(request: ToolRequest, ws: Workspace,
                  host_docs: Mapping[str, str] | None = None) -> ChatMessage:
    names = "\n".join(f"- {p}" for p in sorted(ws.files))
    examples = "\n".join(json.dumps(_example(e), ensure_ascii=False) for e in request.examples)
    text = (f"要做的工具：`{request.name}`（给 {request.agent} agent 用）\n\n"
            f"显示名：{request.title}\n\n"
            f"## 用途\n{request.purpose}\n\n"
            f"## 样例（args → expect，每行一个；也在 examples.json 里）\n{examples}\n\n"
            f"{host_section(request, host_docs)}"
            f"## 工作区现有文件\n{names}\n\n"
            "先读 examples.json，从样例写测试，再写 schema.json 和 tool.py，测试全部通过后 submit。")
    return ChatMessage(role="user", text=text)


def limits_for(pack: "HarnessPack | None") -> LoopLimits:
    """LIMITS, with the tool-call cap from the user's harness pack when one is given: its
    `H4.forge.max_tool_calls` may only lower CODER_MAX_TOOL_CALLS (`harness.apply.forge_limits`)."""
    if pack is None:
        return LIMITS
    from evowiki.harness.apply import forge_limits
    return replace(LIMITS, max_tool_calls=forge_limits(pack, "tool", CODER_MAX_TOOL_CALLS))


def coder_spec(holder: WorkspaceHolder, *, request: ToolRequest, trace: TraceSink, run_id: str,
               sent: Callable[[ChatRequest], ChatRequest],
               loop_limits: LoopLimits = LIMITS) -> AgentSpec:
    tools = coder_tools(holder)
    defs = tuple(ToolDef(name=t.spec.name, description=t.spec.description,
                         parameters=t.spec.parameters) for t in tools)

    def request_of(state: RequestState) -> ChatRequest:
        return ChatRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=request.name,
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


def run_coder(request: ToolRequest, converse: Converse, *, sandbox: Sandbox, trace: TraceSink,
              run_id: str, user_id: str, seed: Mapping[str, str] | None = None,
              sent: Callable[[ChatRequest], ChatRequest] = lambda r: r,
              host_docs: Mapping[str, str] | None = None,
              pack: "HarnessPack | None" = None,
              ) -> tuple[Workspace, LoopResult]:
    """Run the coder to the end. Returns the final workspace (submitted or not: `build` decides)
    and the loop result. With a `run_id` the run is traced as agent "forge" for `user_id`; an
    exception finishes the trace as failed and is re-raised. `pack`: the proposing user's harness
    pack, which may lower the tool-call cap (`limits_for`); None = the forge's own cap."""
    holder = WorkspaceHolder(ws=start_workspace(request, seed), sandbox=sandbox)
    spec = coder_spec(holder, request=request, trace=trace, run_id=run_id, sent=sent,
                      loop_limits=limits_for(pack))
    ctx = ToolContext(user_id=user_id, session_id=f"forge:{run_id or request.name}")
    if run_id:
        trace.start(run_id, user_id=user_id, agent=AGENT, subject=request.name, pack_version=0,
                    input={"request": request.model_dump(mode="json")})
    try:
        result = _drain(run_loop(spec, converse, ctx, [first_message(request, holder.ws, host_docs)]))
    except BaseException as exc:
        if run_id:
            finish_failed(trace, run_id, exc)
        raise
    if run_id:
        trace.finish(run_id, stop=result.stop, error=None)
    return holder.ws, result
