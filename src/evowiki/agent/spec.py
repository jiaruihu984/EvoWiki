"""What differs between agents that share one model/tool loop (`agent.loop.run_loop`): the limits,
how a request is built from the state, and a few hooks. Everything else is the loop's."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from evowiki.agent.tools import Tool, ToolResult
from evowiki.agent.trace import NO_TRACE, TraceSink
from evowiki.llm.chat import ChatMessage, ChatRequest, ToolCall

CapPolicy = Literal["force_answer", "stop"]


@dataclass(frozen=True)
class LoopLimits:
    max_tool_calls: int
    on_cap: CapPolicy                 # assistant: force_answer (next call tool_choice="none"); deep: stop
    text_is_answer: bool              # assistant: a reply without tool calls is the answer; deep: nudge
    max_nudges: int = 0               # deep 2: replies without tool calls re-prompted with nudge_text
    nudge_text: str = ""
    max_truncated: int = 0            # deep 1: replies cut at max_tokens retried (state.truncated += 1)
    truncated_text: str = ""          # appended as a user message before such a retry (never an edit
                                      # of the system prompt or earlier messages: the cached prefix stays)
    tool_error_max: int = 12000       # chars of a tool exception shown to the model (deep 2000)
    refused_text: str = "本轮工具次数用完了。"


@dataclass(frozen=True)
class ElidePolicy:
    """Old long read results and write-call bodies become stubs after `keep_turns` model calls
    (never when the history holds signed thinking blocks)."""
    read_tools: frozenset[str]
    write_tools: frozenset[str]
    keep_turns: int
    stub_over: int
    read_stub: str
    write_stub: str


@dataclass(frozen=True)
class RequestState:
    messages: tuple[ChatMessage, ...]          # already elided
    tool_choice: Literal["auto", "none"]
    turn: int                                  # 1-based model-call count of this loop
    truncated: int                             # truncated replies retried so far


AfterTool = Callable[[ToolCall, Tool | None, ToolResult], tuple[ToolResult, Sequence[object]]]


@dataclass(frozen=True)
class Restart:
    """two_phase tool exposure (llm.exposure, stage 3 T8): the tool list a request carries changes
    only when a model call's tool calls were ALL `loaders` (load_skill / read_ref) and changed the
    set (`changed()`): that call and its results are folded into one user-side note
    (`note(results)`, no echo, no signature) and the step is asked again with the new list (a new
    model call of the same turn; `refresh()` is called first). In a mixed batch the loaders'
    results get `later` appended (their tools come next turn). `active()`: two_phase is in force
    (it may switch on mid-turn: `rejected(exc)` names a provider error refusing a changed list,
    then `downgrade()` is called once and the step retried, the last pure-load call folded)."""
    loaders: frozenset[str]
    active: Callable[[], bool]
    changed: Callable[[], bool]
    refresh: Callable[[], None]
    note: Callable[[Sequence[str]], str]
    later: str
    rejected: Callable[[BaseException], bool] = field(default=lambda _e: False)
    downgrade: Callable[[], None] = field(default=lambda: None)


@dataclass
class AgentSpec:
    name: str
    tools: Sequence[Tool]
    limits: LoopLimits
    request: Callable[[RequestState], ChatRequest]
    elide: ElidePolicy | None = None
    finished: Callable[[], bool] = field(default=lambda: False)   # checked after each tool batch
    cancelled: Callable[[], bool] = field(default=lambda: False)  # checked before each model call
    after_tool: AfterTool | None = None        # may replace the result and add events to yield
    # the request as the gateway sends it (`Gateway.as_sent`: the user's guidance applied); the
    # loop sends and traces this one
    sent: Callable[[ChatRequest], ChatRequest] = field(default=lambda r: r)
    trace: TraceSink = NO_TRACE                # gets every model and tool step of a run ...
    run_id: str = ""                           # ... with this id ("" = not traced); the caller
                                               # starts and finishes the run (agent/trace.py)
    # what a read / network tool brings back (pages, materials, web text) is fenced as material,
    # never the user's words (audit 2026-10-27 D01): the agents that read the learner's material
    fence_reads: bool = False
    # read tools whose results are the system's own rules, never material (load_skill / read_ref)
    unfenced: frozenset[str] = frozenset()
    # the execution gate (stage 3, B2.3 (c)): a tool the current request did not offer is refused
    # (`not_loaded`) and never runs. None = every tool in `tools` may run (the writer, the hooks)
    allowed: Callable[[str], bool] | None = None
    # what a refused (not offered) call says, by tool name: what to load first
    not_loaded: Callable[[str], str] = field(
        default=lambda name: f"{name} 还没载入：先用 load_skill 载入它所在的技能")
    # two_phase tool exposure (see Restart); None = the request's tools are the spec's business
    restart: "Restart | None" = None
