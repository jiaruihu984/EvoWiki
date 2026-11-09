"""The model <-> tools loop both agents run on (the page writer's `write_round`, the reading
assistant's `run_turn`): one request per model call, tool results back to the model, until the
model answers, a tool says it is finished, or the tool cap is reached. What differs between the
agents is in the `AgentSpec`; the loop streams `ToolStarted` / `ToolFinished` (and whatever the
spec's `after_tool` adds) and returns a `LoopResult`. Nothing here persists by itself: inside a
durable workflow (the page writer in the service) the read / action tools run as recorded steps
(`agent.durable`), outside one they are plain calls. With a `spec.run_id` the spec's trace sink gets
every model call (a failed one too) and every tool result, numbered in the order they happened.

`first`: tool calls the caller makes itself before the first model call (no routing decision by the
model: the reading assistant's learn_goal turn runs new_goal). They run exactly like the model's
calls (events, durable step, `after_tool`, trace, the tool count); a result with an error stops the
run (`FirstToolRefused`). Their results reach the model as text appended to the last (user)
message, `first_note(call, result)` each, not as a tool-call history: no model made those calls,
and a provider that signs its own tool calls (Anthropic thinking, DeepSeek reasoning) would refuse
an unsigned one. A `first` call may pause (the diagnosis's 摸底 grid): the run ends on its card with
no model call; when his next message resumes it the tool's `resume` result reaches the model the
same way (`first_note`), since no placeholder exists for a call no model made.

Hardening copied from DeepTutor (HKUDS/DeepTutor, Apache-2.0, agents/loop/agent_loop.py):
- a model call cut by a cancellation (`RequestCancelled`: the spec's `cancelled`, or the gateway's
  DBOS workflow check) is `Cancelled`, never retried;
- a model call that already streamed text the learner saw is never retried (the gateway's
  `converse`; here: a truncated reply with `partial_text` is not re-asked, it fails visibly);
- a pausing tool (`ToolSpec.pauses`, e.g. ask_user) ends the run on its question (DeepTutor's
  `pause_for_user`, without its wait): the loop yields `Paused`, runs the rest of that batch, and
  returns stop "paused" with `LoopResult.paused` and the messages so far, the paused call's result
  an empty placeholder (`PLACEHOLDER`). Nothing waits: the caller stores them, and the learner's
  next message resumes the run (`run_loop(resume=Resume(...))` over those messages): the tool's
  `resume(args, ctx, pause, reply)` runs first, as a recorded step, its result fills the
  placeholder (the call's result, in protocol) and the run goes on. One question per batch: a
  second pausing call in it is refused (`ONE_QUESTION`); a pause from a tool that does not
  declare `pauses` / has no `resume` fails closed (`PauseUnavailable`)."""

import logging
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from evowiki.agent import durable
from evowiki.agent.errors import Cancelled, FirstToolRefused, PauseUnavailable, ToolLimitExceeded
from evowiki.agent.spec import AgentSpec, ElidePolicy, RequestState, Restart
from evowiki.agent.tools import Pause, PausingTool, Reply, Tool, ToolContext, ToolResult
from evowiki.llm import run_context
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn, ToolCall
from evowiki.llm.deadline import RequestCancelled, cancel_scope
from evowiki.llm.gateway import ProviderOutputError
from evowiki.llm.models import LLMCall
from evowiki.llm.usage import BudgetExceededError
from evowiki.sources.config import fence

log = logging.getLogger(__name__)

StopReason = Literal["answered", "finished", "tool_cap", "no_tool_calls", "paused"]
# the paused call's result until the learner's next message resumes the run (never sent)
PLACEHOLDER = ""
# model-facing: a second pausing call in the batch that already asked
ONE_QUESTION = "一次只问一个问题：先等他回答上一个。"
UNPARSED_ARGS = "这次工具调用的参数不是合法 JSON（字符串要加引号），没有执行，改好后重新调用。你写的是：{raw}"
UNPARSED_SHOWN = 300


@dataclass(frozen=True)
class ToolStarted:
    call_id: str
    name: str
    effect: str | None
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolFinished:
    call_id: str
    name: str
    effect: str | None
    result: ToolResult


@dataclass(frozen=True)
class Paused:
    """A pausing tool asked the learner: the run ends after this batch (stop "paused")."""
    call: ToolCall
    pause: Pause


@dataclass(frozen=True)
class Resume:
    """Go on with a paused run: its call, its pause and the learner's next message. The messages
    given to `run_loop` are the paused run's, with the call's `PLACEHOLDER` result."""
    call: ToolCall
    pause: Pause
    reply: Reply


@dataclass(frozen=True)
class LoopResult:
    stop: StopReason
    text: str                                  # the answer (stop == "answered"), else the last reply text
    calls: tuple[LLMCall, ...]
    tool_calls: int                            # tools executed (refused ones not counted)
    messages: tuple[ChatMessage, ...]          # full, un-elided history
    paused: Paused | None = None               # stop == "paused": the question that ended the run


Converse = Callable[[ChatRequest], tuple[ChatTurn, LLMCall]]


def run_tool(tool: Tool, args: dict[str, Any], ctx: ToolContext, error_max: int) -> ToolResult:
    try:
        return tool.run(args, ctx)
    except BudgetExceededError:
        raise
    except Exception as exc:  # noqa: BLE001 - a tool failure goes back to the model
        return ToolResult(content=f"工具出错：{exc}"[:error_max], error=str(exc))


def _framed(tool: Tool | None, result: ToolResult) -> str:
    """A tool result as the model reads it, in an agent with `fence_reads`: what a read-tier
    tool brings back (pages, materials, web text) is fenced as material; an action's own report
    (add_source's too, though it fetches: tier direct) and an error are the system's and stay
    plain."""
    if (tool is None or result.error is not None or tool.spec.tier != "read"
            or tool.spec.pauses):         # a pausing tool's result carries the learner's reply
        return result.content
    return fence(result.content)


def _stub_args(args: dict[str, Any], policy: ElidePolicy) -> dict[str, Any]:
    return {k: policy.write_stub if isinstance(v, str) and len(v) > policy.stub_over else v
            for k, v in args.items()}


def _stub_writes(m: ChatMessage, policy: ElidePolicy) -> ChatMessage:
    """An old assistant message with the big arguments of its write calls (bodies, HTML) stubbed —
    in `tool_calls` and in the provider's own echo (Anthropic tool_use blocks)."""
    ids = {c.id for c in m.tool_calls if c.name in policy.write_tools}
    if not ids:
        return m
    calls = tuple(c.model_copy(update={"args": _stub_args(c.args, policy)}) if c.id in ids else c
                  for c in m.tool_calls)
    echo = m.echo
    if isinstance(echo, list):
        echo = [{**b, "input": _stub_args(b.get("input") or {}, policy)}
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") in ids
                else b for b in echo]
    return m.model_copy(update={"tool_calls": calls, "echo": echo})


def _anthropic_history(messages: Sequence[ChatMessage]) -> bool:
    """An Anthropic echo is always a list of content blocks; DeepSeek's is a dict or None."""
    return any(m.role == "assistant" and isinstance(m.echo, list) for m in messages)


def elide(messages: Sequence[ChatMessage], aged: Mapping[int, int], turn: int,
          policy: ElidePolicy) -> tuple[ChatMessage, ...]:
    """The history as sent. Messages older than `keep_turns` turns are shortened: a long read result
    becomes a stub (the model can read again), and so do the bodies / HTML of write calls (they are
    in the draft already) — so re-sending them every turn does not eat the page budget.
    Never for an Anthropic history, thinking or not: a thinking signature is bound to the exact
    content before it (a shortened earlier message gets a 400), and prompt caching reads a
    byte-stable prefix (an interior edit makes every later turn miss and re-write the whole history
    at the cache-write rate)."""
    if _anthropic_history(messages):
        return tuple(messages)
    out = []
    for i, m in enumerate(messages):
        if i in aged and turn - aged[i] > policy.keep_turns:
            m = (m.model_copy(update={"text": policy.read_stub}) if m.role == "tool"
                 else _stub_writes(m, policy))
        out.append(m)
    return tuple(out)


FirstNote = Callable[[ToolCall, ToolResult], str]


def _fold_loads(msgs: list[ChatMessage], rs: "Restart") -> bool:
    """two_phase: the last model call, when its tool calls were all loads (and every load
    worked), and its results become one user-side note (no echo, no signature); True when folded.
    `rs.refresh()` then lets the next request carry the new tool list."""
    i = next((j for j in range(len(msgs) - 1, -1, -1) if msgs[j].role == "assistant"), None)
    if i is None or not msgs[i].tool_calls:
        return False
    calls, results = msgs[i].tool_calls, msgs[i + 1:]
    if (not all(c.name in rs.loaders for c in calls) or len(results) != len(calls)
            or any(m.role != "tool" or m.is_error for m in results)):
        return False
    note = rs.note([m.text for m in results])
    del msgs[i:]
    if msgs and msgs[-1].role == "user" and not msgs[-1].tool_calls:
        last = msgs[-1]
        msgs[-1] = last.model_copy(update={"text": f"{last.text}\n\n{note}"})
    else:
        msgs.append(ChatMessage(role="user", text=note))
    rs.refresh()
    return True


def run_loop(spec: AgentSpec, converse: Converse, ctx: ToolContext,
             messages: Sequence[ChatMessage], *, first: Sequence[ToolCall] = (),
             first_note: FirstNote | None = None,
             resume: Resume | None = None) -> Generator[object, None, LoopResult]:
    limits = spec.limits
    by_name = {t.spec.name: t for t in spec.tools}
    msgs = list(messages)
    calls: list[LLMCall] = []
    aged: dict[int, int] = {}        # index of a long read result / a write call -> its turn
    executed = nudges = turns = truncated = 0
    traced = spec.run_id != ""
    seq = 0                          # the run's step number: model and tool steps in one order

    # the run as the current one (llm/run_context.py) while a model call or a tool runs, never
    # across a yield: JSON calls made inside are appended to it, numbered with the loop's steps
    bound = run_context.loop_run(spec.run_id, ctx.user_id, spec.name, spec.trace)

    def step() -> int:
        nonlocal seq
        if bound is not None:
            return bound.next_seq()
        seq += 1
        return seq

    paused: list[Paused] = []        # the question that ends the run (one per run)

    def result_of(stop: StopReason, text: str) -> LoopResult:
        return LoopResult(stop, text, tuple(calls), executed, tuple(msgs),
                          paused[0] if paused else None)

    def recorded(tool: Tool, name: str, run: Callable[[], ToolResult]) -> ToolResult:
        if tool.spec.category in ("read", "action") and durable.in_workflow():
            # recorded: a replay returns what the tool read / did, never runs it again
            def once() -> dict:
                with run_context.binding(bound):
                    return run().model_dump(mode="json")
            return ToolResult.model_validate(durable.step(name, once))
        with run_context.binding(bound):
            return run()

    def pausing(tool: Tool | None, name: str) -> PausingTool:
        if tool is None or not tool.spec.pauses or not hasattr(tool, "resume"):
            raise PauseUnavailable(name)
        return tool  # type: ignore[return-value]

    def finish(tc: ToolCall, tool: Tool | None, result: ToolResult
               ) -> Generator[object, None, ToolResult]:
        """A call's result: its event, `after_tool`, the trace, the count."""
        nonlocal executed
        yield ToolFinished(tc.id, tc.name, tool.spec.effect if tool is not None else None, result)
        if spec.after_tool is not None:
            result, extra = spec.after_tool(tc, tool, result)
            yield from extra
        if traced:
            spec.trace.tool(spec.run_id, step(), tc.id, tc.name, tc.args, result)
        executed += 1
        return result

    def execute(tc: ToolCall) -> Generator[object, None, ToolResult]:
        """One tool call: its events, the (recorded) run, `after_tool`, the trace, the count. A
        question to the learner (a pausing tool's `pause`) is `Paused` instead of finished: traced
        and counted, no `after_tool` (the caller shows and keeps it)."""
        nonlocal executed
        tool = by_name.get(tc.name)
        effect = tool.spec.effect if tool is not None else None
        yield ToolStarted(tc.id, tc.name, effect, tc.args)
        if tc.unparsed is not None:
            result = ToolResult(content=UNPARSED_ARGS.format(raw=tc.unparsed[:UNPARSED_SHOWN]),
                                error="arguments are not JSON")
        elif tool is None:
            result = ToolResult(content=f"没有这个工具：{tc.name}",
                                error=f"unknown tool {tc.name}")
        elif spec.allowed is not None and not spec.allowed(tc.name):
            # not offered by the request the model answered: nothing runs (B2.3 (c))
            result = ToolResult(content=spec.not_loaded(tc.name),
                                error=f"tool not loaded {tc.name}")
        else:
            result = recorded(tool, f"tool:{tc.name}",
                              lambda t=tool, a=tc.args: run_tool(t, a, ctx,
                                                                 limits.tool_error_max))
            if result.pause is not None and result.error is None:
                pausing(tool, tc.name)
                if paused:            # the batch already asked: one question at a time
                    result = ToolResult(content=ONE_QUESTION, error="a second question")
                else:
                    paused.append(Paused(tc, result.pause))
                    yield paused[0]
                    if traced:
                        spec.trace.tool(spec.run_id, step(), tc.id, tc.name, tc.args, result)
                    executed += 1
                    return result
        return (yield from finish(tc, tool, result))

    def resumed(r: Resume) -> Generator[object]:
        """The paused call, answered by his next message: the tool's `resume` (a recorded step)
        makes its result, which fills the call's placeholder."""
        t = pausing(by_name.get(r.call.name), r.call.name)
        at = next((i for i, m in enumerate(msgs)
                   if m.role == "tool" and m.tool_call_id == r.call.id), None)
        if at is None and (first_note is None or not msgs or msgs[-1].role != "user"):
            raise ValueError(f"no placeholder result for the paused call {r.call.id}")
        yield ToolStarted(r.call.id, r.call.name, t.spec.effect, r.call.args)

        def run() -> ToolResult:
            try:
                return t.resume(r.call.args, ctx.model_copy(update={"call_id": r.call.id}),
                                r.pause, r.reply)
            except BudgetExceededError:
                raise
            except Exception as exc:  # noqa: BLE001 - like run_tool: back to the model
                return ToolResult(content=f"工具出错：{exc}"[:limits.tool_error_max],
                                  error=str(exc))
        result = recorded(t, f"tool:{r.call.name}.resume", run)
        result = yield from finish(r.call, t, result)
        if at is None:                # a `first` call: no call in the history to answer
            last = msgs[-1]
            msgs[-1] = last.model_copy(update={"text": "\n\n".join(
                [last.text, first_note(r.call, result)])})
            return
        text = (_framed(t, result) if spec.fence_reads and r.call.name not in spec.unfenced
                else result.content)
        msgs[at] = msgs[at].model_copy(update={"text": text, "is_error": result.error is not None})

    if first:
        if first_note is None or not msgs or msgs[-1].role != "user":
            raise ValueError("first tool calls need first_note and a last user message")
        notes = []
        for tc in first:
            result = yield from execute(tc)
            if paused:                # the run ends on this card, no model call: his message resumes
                break
            if result.error is not None:
                raise FirstToolRefused(tc.name, result.error)
            notes.append(first_note(tc, result))
        last = msgs[-1]
        msgs[-1] = last.model_copy(update={"text": "\n\n".join([last.text, *notes])})
        if paused:
            return result_of("paused", "")
    if resume is not None:
        yield from resumed(resume)

    said = False
    while True:
        if spec.cancelled():
            raise Cancelled("cancelled")
        turns += 1
        choice: Literal["auto", "none"] = (
            "none" if limits.on_cap == "force_answer" and executed >= limits.max_tool_calls
            else "auto")
        sent = elide(msgs, aged, turns, spec.elide) if spec.elide else tuple(msgs)
        req = spec.sent(spec.request(RequestState(sent, choice, turns, truncated)))
        try:
            # the spec's cancel also aborts the call in flight (llm.deadline), not only the next one
            with cancel_scope(spec.cancelled), run_context.binding(bound), run_context.own_turn():
                turn, c = converse(req)
        except Exception as exc:
            if traced:          # a failed call is a step too (no turn), then it raises or retries
                try:
                    spec.trace.model(spec.run_id, step(), req, None, None, str(exc))
                except Exception as err:      # the provider's error stays the one raised
                    log.exception("trace model step failed for run %s", spec.run_id)
                    exc.add_note(f"trace model step failed: {err}")
            if isinstance(exc, RequestCancelled):   # stopped mid-call: never retried
                raise Cancelled("cancelled") from exc
            rs = spec.restart
            if (rs is not None and not rs.active() and rs.rejected(exc)
                    and not getattr(exc, "partial_text", None)):
                # the provider refused the changed tool list: two_phase from now on, the last
                # pure-load call folded, the step asked again (once: active() is now true)
                rs.downgrade()
                _fold_loads(msgs, rs)
                continue
            # nothing to add after a tool: the turn's text is already out (Anthropic's end_turn with
            # no content after a tool result, 1 of 1549 calls in evalset run 11041904a6d7)
            if (limits.text_is_answer and isinstance(exc, ProviderOutputError)
                    and exc.status == "empty_output" and said):
                return result_of("answered", "")
            # a reply that ran out of output tokens is retried; the note goes after the history as
            # a user message and stays there (append-only: the cached prefix and any signed
            # thinking before it are untouched). Not when it streamed text the learner saw
            # (`partial_text`): a second reply would repeat it, so it fails visibly instead
            if (not isinstance(exc, ProviderOutputError) or exc.status != "truncated"
                    or truncated >= limits.max_truncated
                    or getattr(exc, "partial_text", None)):
                raise
            truncated += 1
            if limits.truncated_text:
                msgs.append(ChatMessage(role="user", text=limits.truncated_text))
            continue
        if traced:
            spec.trace.model(spec.run_id, step(), req, turn, c.id, None)
        if bound is not None:
            bound.last_call = c          # what a tool's rejection refers to (run_context.reject)
        calls.append(c)
        if not turn.tool_calls:
            if limits.text_is_answer:
                return result_of("answered", turn.text)
            if nudges >= limits.max_nudges:
                return result_of("no_tool_calls", turn.text)
            nudges += 1
            msgs.append(ChatMessage(role="assistant", text=turn.text, echo=turn.echo))
            msgs.append(ChatMessage(role="user", text=limits.nudge_text))
            continue
        if choice == "none":
            raise ToolLimitExceeded(f"model called {len(turn.tool_calls)} tool(s) after the cap")
        if spec.elide and any(tc.name in spec.elide.write_tools for tc in turn.tool_calls):
            aged[len(msgs)] = turns
        msgs.append(ChatMessage(role="assistant", text=turn.text, tool_calls=turn.tool_calls,
                                echo=turn.echo))
        said = said or bool(turn.text.strip())
        rs = spec.restart
        loads = rs is not None and rs.active() and any(
            tc.name in rs.loaders for tc in turn.tool_calls)
        only_loads = loads and all(tc.name in rs.loaders for tc in turn.tool_calls)  # type: ignore[union-attr]
        for tc in turn.tool_calls:
            if executed >= limits.max_tool_calls:
                msgs.append(ChatMessage(role="tool", tool_call_id=tc.id, is_error=True,
                                        text=limits.refused_text))
                if traced:
                    spec.trace.tool(spec.run_id, step(), tc.id, tc.name, tc.args,
                                    ToolResult(content=limits.refused_text,
                                               error="tool cap reached"))
                continue
            result = yield from execute(tc)
            if paused and paused[0].call is tc:     # its result comes with his next message
                msgs.append(ChatMessage(role="tool", tool_call_id=tc.id, text=PLACEHOLDER))
                continue
            if (spec.elide and tc.name in spec.elide.read_tools
                    and len(result.content) > spec.elide.stub_over):
                aged[len(msgs)] = turns
            text = (_framed(by_name.get(tc.name), result)
                    if spec.fence_reads and tc.name not in spec.unfenced else result.content)
            if loads and not only_loads and tc.name in rs.loaders and result.error is None:  # type: ignore[union-attr]
                text += rs.later  # type: ignore[union-attr]
            msgs.append(ChatMessage(role="tool", tool_call_id=tc.id, text=text, parts=result.images,
                                    is_error=result.error is not None))
        if paused:                    # the run ends on its question; his next message resumes it
            return result_of("paused", turn.text)
        if only_loads and rs is not None and rs.changed() and _fold_loads(msgs, rs):
            continue                  # two_phase: the same step again, with the new tools
        if spec.finished():
            return result_of("finished", turn.text)
        if executed >= limits.max_tool_calls and limits.on_cap == "stop":
            return result_of("tool_cap", turn.text)
