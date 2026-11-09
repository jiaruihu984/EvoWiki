"""run_loop reports its model and tool steps to the spec's TraceSink (agent/trace.py), numbered in
order, only when the spec carries a run id."""

from typing import Any

import pytest

from evowiki.agent.loop import run_loop
from evowiki.agent.spec import LoopLimits
from evowiki.agent.tools import ToolResult
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn
from evowiki.llm.gateway import ProviderOutputError, ProviderUnavailableError
from tests.agent.test_loop import CTX, Script, drive, make_spec, reply, tc


class Recorder:
    """An in-memory TraceSink: every call as a tuple."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def start(self, run_id: str, *, user_id: str, agent: str, subject: str, pack_version: int,
              input: dict) -> None:
        self.calls.append(("start", run_id, user_id, agent, subject, pack_version, input))

    def model(self, run_id: str, seq: int, request: ChatRequest, turn: ChatTurn | None,
              call_id: str | None, error: str | None) -> None:
        self.calls.append(("model", run_id, seq, request, turn, call_id, error))

    def tool(self, run_id: str, seq: int, call_id: str, name: str, args: dict,
             result: ToolResult) -> None:
        self.calls.append(("tool", run_id, seq, call_id, name, args, result))

    def finish(self, run_id: str, *, stop: str | None, error: str | None) -> None:
        self.calls.append(("finish", run_id, stop, error))


def _run(sink: Recorder, *items: Any, run_id: str = "r1", **kw: Any):
    spec, _ = make_spec(trace=sink, run_id=run_id, **kw)
    script = Script(*items)
    return drive(run_loop(spec, script, CTX, [ChatMessage(role="user", text="q")])), script


def test_model_tool_model_in_order():
    sink = Recorder()
    (_, result), script = _run(sink, reply(tc("echo", text="hi")), reply(text="答"))
    assert result.stop == "answered"
    assert [(c[0], c[2]) for c in sink.calls] == [("model", 1), ("tool", 2), ("model", 3)]
    m1, t2, m3 = sink.calls
    assert m1[1] == t2[1] == m3[1] == "r1"
    # the request the loop sent, the turn it got, the llm_call id
    assert m1[3] is script.requests[0] and m3[3] is script.requests[1]
    assert m1[4].tool_calls[0].name == "echo" and m1[5] == "c1" and m1[6] is None
    assert t2[3:6] == ("echo-1", "echo", {"text": "hi"})
    assert t2[6] == ToolResult(content="hi")


def test_seq_is_deterministic():
    runs = []
    for _ in range(2):
        sink = Recorder()
        _run(sink, reply(tc("echo", 1, text="a"), tc("echo", 2, text="b")), reply(text="答"))
        runs.append([(c[0], c[2], c[3] if c[0] == "tool" else None) for c in sink.calls])
    assert runs[0] == runs[1] == [("model", 1, None), ("tool", 2, "echo-1"),
                                  ("tool", 3, "echo-2"), ("model", 4, None)]


def test_failed_model_call_is_recorded_then_raised():
    sink = Recorder()
    with pytest.raises(RuntimeError, match="down"):
        _run(sink, RuntimeError("down"))
    [(kind, _, seq, _, turn, call_id, error)] = sink.calls
    assert (kind, seq, turn, call_id, error) == ("model", 1, None, None, "down")


def test_truncated_retry_is_a_step():
    sink = Recorder()
    limits = LoopLimits(max_tool_calls=5, on_cap="force_answer", text_is_answer=True,
                        max_truncated=1, truncated_text="写少一点")
    _run(sink, ProviderOutputError("cut", status="truncated"), reply(text="答"), limits=limits)
    assert [(c[0], c[2], c[6]) for c in sink.calls] == [("model", 1, "cut"), ("model", 2, None)]


class FailingModelSink(Recorder):
    """A sink whose failed-model-step write raises (a trace store outage)."""

    def model(self, run_id: str, seq: int, request: ChatRequest, turn: ChatTurn | None,
              call_id: str | None, error: str | None) -> None:
        if error is not None:
            raise RuntimeError("trace store down")
        super().model(run_id, seq, request, turn, call_id, error)


def test_trace_failure_does_not_mask_the_provider_error():
    sink = FailingModelSink()
    with pytest.raises(ProviderUnavailableError, match="provider down") as info:
        _run(sink, ProviderUnavailableError("provider down"))
    assert "trace model step failed: trace store down" in info.value.__notes__


def test_trace_failure_does_not_stop_the_truncated_retry():
    sink = FailingModelSink()
    limits = LoopLimits(max_tool_calls=5, on_cap="force_answer", text_is_answer=True,
                        max_truncated=1, truncated_text="写少一点")
    (_, result), _ = _run(sink, ProviderOutputError("cut", status="truncated"), reply(text="答"),
                          limits=limits)
    assert result.stop == "answered" and result.text == "答"
    assert [(c[0], c[2]) for c in sink.calls] == [("model", 2)]


def test_trace_failure_on_a_successful_call_raises():
    class Broken(Recorder):
        def model(self, *a: Any) -> None:
            raise RuntimeError("trace store down")

    with pytest.raises(RuntimeError, match="trace store down"):
        _run(Broken(), reply(text="答"))


def test_refused_tool_is_recorded():
    sink = Recorder()
    limits = LoopLimits(max_tool_calls=1, on_cap="force_answer", text_is_answer=True,
                        refused_text="用完了")
    _run(sink, reply(tc("echo", 1, text="a"), tc("echo", 2, text="b")), reply(text="答"),
         limits=limits)
    refused = sink.calls[2]
    assert refused[:5] == ("tool", "r1", 3, "echo-2", "echo")
    assert refused[6].content == "用完了" and refused[6].error is not None


def test_no_run_id_no_trace():
    sink = Recorder()
    _run(sink, reply(tc("echo", text="hi")), reply(text="答"), run_id="")
    assert sink.calls == []
