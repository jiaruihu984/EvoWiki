"""The loop's hardening (DeepTutor agent_loop): no retry after visible output, and the generic pause
of a pausing tool (ask_user, confirm's pick): the run ENDS on the question (nothing waits, no
further model call); the learner's next message resumes it from the stored messages, the tool's
`resume` making the call's result. Fakes only."""

from typing import Any

import pytest

from evowiki.agent import durable
from evowiki.agent.errors import PauseUnavailable
from evowiki.agent.loop import (
    ONE_QUESTION,
    PLACEHOLDER,
    Paused,
    Resume,
    ToolFinished,
    ToolStarted,
    run_loop,
)
from evowiki.agent.spec import AgentSpec, LoopLimits
from evowiki.agent.tools import Pause, Reply, ToolContext, ToolResult, ToolSpec
from evowiki.llm.chat import ChatMessage
from evowiki.llm.gateway import ProviderOutputError
from tests.agent.test_loop import CTX, Echo, Script, drive, make_spec, reply, run, tc


class Asker:
    """A pausing tool with its own payload type; `resume` turns the reply into the result."""

    spec = ToolSpec(name="asker", description="ask", parameters={"type": "object"},
                    effect="read", pauses=True)

    def __init__(self):
        self.resumed: list[tuple[dict[str, Any], Pause, Reply]] = []

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content="asked", pause=Pause(kind="asker", payload={"q": args["q"]}))

    def resume(self, args, ctx, pause, reply):
        self.resumed.append((args, pause, reply))
        return ToolResult(content=f"he said {reply.text}")


def _spec(tool, **kw):
    base, states = make_spec()
    return AgentSpec(name="t", tools=[tool, Echo()], limits=base.limits, request=base.request,
                     **kw), states


def test_a_pause_ends_the_run_with_no_further_model_call():
    tool = Asker()
    spec, _ = _spec(tool)
    script = Script(reply(tc("asker", q="基础？"), text="先问一句"), reply(text="never"))
    events, res = run(spec, script)
    assert [type(e) for e in events] == [ToolStarted, Paused]
    assert res.stop == "paused" and res.text == "先问一句" and len(script.requests) == 1
    assert res.paused == Paused(tc("asker", q="基础？"), Pause(kind="asker", payload={"q": "基础？"}))
    # the call's result is a placeholder until his next message
    assert res.messages[-1] == ChatMessage(role="tool", tool_call_id="asker-1", text=PLACEHOLDER)
    assert tool.resumed == [] and res.tool_calls == 1


def test_the_rest_of_the_batch_runs_and_a_second_question_is_refused():
    spec, _ = _spec(Asker())
    script = Script(reply(tc("asker", 1, q="a"), tc("echo", text="x"), tc("asker", 2, q="b")))
    events, res = run(spec, script)
    assert res.stop == "paused" and res.paused.call.id == "asker-1"
    tools = {m.tool_call_id: m for m in res.messages if m.role == "tool"}
    assert tools["asker-1"].text == PLACEHOLDER and tools["echo-1"].text == "x"
    assert tools["asker-2"].text == ONE_QUESTION and tools["asker-2"].is_error
    assert sum(isinstance(e, Paused) for e in events) == 1


def test_resume_puts_his_message_in_the_placeholder_and_the_run_goes_on():
    tool = Asker()
    spec, _ = _spec(tool)
    _, paused = run(spec, Script(reply(tc("asker", q="基础？"))))
    script = Script(reply(text="好，那从头讲"))
    events, res = drive(run_loop(spec, script, CTX, paused.messages,
                                 resume=Resume(paused.paused.call, paused.paused.pause,
                                               Reply(text="零基础"))))
    assert [type(e) for e in events] == [ToolStarted, ToolFinished]
    assert tool.resumed == [({"q": "基础？"}, Pause(kind="asker", payload={"q": "基础？"}),
                             Reply(text="零基础"))]
    # the model reads the reply as the paused call's result, in protocol (never fenced: his words)
    sent = script.requests[0].messages
    assert sent[:-1] == paused.messages[:-1]
    assert sent[-1].role == "tool" and sent[-1].tool_call_id == "asker-1"
    assert sent[-1].text == "he said 零基础"
    assert res.stop == "answered" and res.text == "好，那从头讲"


def test_a_resumed_run_may_ask_again():
    spec, _ = _spec(Asker())
    _, first = run(spec, Script(reply(tc("asker", 1, q="a"))))
    _, again = drive(run_loop(spec, Script(reply(tc("asker", 2, q="b"))), CTX, first.messages,
                              resume=Resume(first.paused.call, first.paused.pause,
                                            Reply(text="x"))))
    assert again.stop == "paused" and again.paused.call.id == "asker-2"
    assert [m.text for m in again.messages if m.role == "tool"] == ["he said x", PLACEHOLDER]


def test_a_pause_from_a_tool_that_does_not_declare_it_fails_closed():
    class Sneaky(Asker):
        spec = ToolSpec(name="asker", description="ask", parameters={"type": "object"},
                        effect="read")

    spec, _ = _spec(Sneaky())
    with pytest.raises(PauseUnavailable):
        run(spec, Script(reply(tc("asker", q="?")), reply(text="ok")))


def test_resume_without_a_placeholder_fails_closed():
    spec, _ = _spec(Asker())
    with pytest.raises(ValueError, match="placeholder"):
        drive(run_loop(spec, Script(reply(text="ok")), CTX, [ChatMessage(role="user", text="hi")],
                       resume=Resume(tc("asker", q="?"), Pause(kind="asker", payload={}),
                                     Reply(text="x"))))


def test_in_a_workflow_the_ask_and_the_resume_are_steps(monkeypatch):
    names: list[str] = []
    real = durable.step

    def spy(name, fn):
        names.append(name)
        return real(name, fn)
    monkeypatch.setattr(durable, "step", spy)
    spec, _ = _spec(Asker())
    with durable.virtual_workflow("pause-1"):
        _, paused = run(spec, Script(reply(tc("asker", q="?"))))
    with durable.virtual_workflow("pause-2"):
        drive(run_loop(spec, Script(reply(text="ok")), CTX, paused.messages,
                       resume=Resume(paused.paused.call, paused.paused.pause, Reply(text="a"))))
    assert names == ["tool:asker", "tool:asker.resume"]


def test_resume_errors_go_back_to_the_model():
    class Broken(Asker):
        def resume(self, args, ctx, pause, reply):
            raise ValueError("bad reply")

    spec, _ = _spec(Broken())
    _, paused = run(spec, Script(reply(tc("asker", q="?"))))
    script = Script(reply(text="ok"))
    events, _ = drive(run_loop(spec, script, CTX, paused.messages,
                               resume=Resume(paused.paused.call, paused.paused.pause,
                                             Reply(text="x"))))
    [fin] = [e for e in events if isinstance(e, ToolFinished)]
    assert fin.result.error == "bad reply" and script.requests[0].messages[-1].is_error


def test_a_truncated_reply_that_streamed_visible_text_is_not_retried():
    """max_truncated would retry it; text the learner saw would repeat, so it fails visibly."""
    limits = LoopLimits(max_tool_calls=5, on_cap="stop", text_is_answer=False, max_truncated=2,
                        truncated_text="接着写")
    spec, states = make_spec(limits=limits)
    shown = ProviderOutputError("cut", status="truncated")
    shown.partial_text = "已经显示的半句"
    with pytest.raises(ProviderOutputError) as info:
        run(spec, Script(shown, reply(text="ok")))
    assert info.value is shown and len(states) == 1


def test_events_of_other_tools_are_unchanged_by_pausing():
    spec, _ = _spec(Asker())
    events, _ = run(spec, Script(reply(tc("echo", text="a")), reply(text="ok")))
    assert [type(e) for e in events] == [ToolStarted, ToolFinished]
    assert not any(isinstance(e, Paused) for e in events)
