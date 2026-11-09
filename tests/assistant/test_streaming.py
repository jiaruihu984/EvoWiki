"""Streaming of the reading assistant's answer (spec 2026-10-15): committed text with its position
(`TextDelta.at`), text between tool rounds taken back (only the last call answers), live text through
the in-process bus merged by the follower, resume from a cursor, and an answer that breaks off
mid-stream (kept, marked interrupted). Scripted providers only."""

import threading

import pytest

from evowiki.assistant import turn_runs
from evowiki.assistant.contracts import MessageIn, TextDelta
from evowiki.assistant.live import LiveBus
from evowiki.assistant.loop import BUSY_SWITCH, TurnDeps, run_turn
from evowiki.assistant.service import AssistantService
from evowiki.assistant.session import MemorySessionStore
from evowiki.assistant.turn_runs import DbosTurnRuns, Follower
from evowiki.llm.chat import ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.providers import (
    TIMEOUT_TEXT,
    ProviderBusyError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from evowiki.llm.scripted import ScriptedChatProvider, ScriptedStreamFailure
from tests.assistant.fakes import INFO, T0, Data, Ids, tools
from tests.assistant.test_loop import CFG, call

MSG = MessageIn(text="问")


def fold(events, start=""):
    """The client's rule (plugin turn.ts): keep answer[:at], append text; at None appends."""
    answer = start
    for e in events:
        if e.type == "text_delta":
            answer = (answer if e.at is None else answer[:e.at]) + e.text
    return answer


def build(tmp_path, script, *, chunk=2, on_chunk=None, counter=None):
    outs = iter(script)

    def answer(m, r):
        if counter is not None:
            counter.append(1)
        out = next(outs)
        if isinstance(out, BaseException):
            raise out
        return out

    prov = ScriptedChatProvider("scripted", answer, stream_chunk=chunk, on_chunk=on_chunk)
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})
    store = MemorySessionStore()
    store.create(INFO)
    return TurnDeps(gateway=gw, tools=tools(), store=store, data=Data(), new_id=Ids(), clock=lambda: T0)


class Recorder:
    """A live source that records each sink's (base_index, base_at, prefix) and text."""

    def __init__(self):
        self.sinks = []

    def __call__(self, base_index, base_at, prefix):
        rec = {"base": (base_index, base_at, prefix), "text": []}
        self.sinks.append(rec)

        class S:
            def text(self, chunk):
                rec["text"].append(chunk)
        return S()


def logged(deps, type_):
    return [e.payload for e in deps.store.entries("s1") if e.type == type_]


# ---- the turn's events ----

def test_text_between_tool_rounds_is_taken_back_and_only_the_last_call_answers(tmp_path):
    """S3 live run 2026-10-28: the narration of every call that went on to call a tool was glued
    into the answer (「这是一条他明确说的……我来记下」, in the third person). It streams live, is
    taken back with an empty text at its position, and only the last call's text is the answer."""
    deps = build(tmp_path, [ChatTurn(text="这是一条他明确说的偏好，我来记下。",
                                     tool_calls=(call("read_k", 1),)),
                            ChatTurn(text="方差大的方向。")])
    live = Recorder()
    ev = list(run_turn(deps, INFO, MSG, live=live))
    deltas = [(i, e) for i, e in enumerate(ev) if e.type == "text_delta"]
    assert [(e.at, e.text) for _, e in deltas] == [(0, ""), (0, "方差大的方向。")]
    # each call's sink: the stream position and answer position its text would take
    assert [s["base"] for s in live.sinks] == [(i, 0, "") for i, _ in deltas]
    assert ["".join(s["text"]) for s in live.sinks] == ["这是一条他明确说的偏好，我来记下。",
                                                       "方差大的方向。"]
    # the client folding the live text and the events ends with the answer alone
    shown = fold([TextDelta(at=0, text="这是一条他明确说的偏好，我来记下。"), *ev])
    assert shown == "方差大的方向。" == logged(deps, "assistant")[0]["text"]
    assert [e.type for e in ev][-1:] == ["done"]          # nothing recorded: no observed event


def test_without_live_text_the_narration_is_never_sent(tmp_path):
    deps = build(tmp_path, [ChatTurn(text="我查一下。", tool_calls=(call("read_k", 1),)),
                            ChatTurn(text="答")])
    ev = list(run_turn(deps, INFO, MSG))
    assert [(e.at, e.text) for e in ev if e.type == "text_delta"] == [(0, "答")]


def test_a_call_that_breaks_off_keeps_its_text_and_marks_the_error_interrupted(tmp_path):
    deps = build(tmp_path, [ChatTurn(text="先看页面。", tool_calls=(call("read_k", 1),)),
                            ScriptedStreamFailure("方差是", ProviderTransientError("cut"))])
    ev = list(run_turn(deps, INFO, MSG, live=Recorder()))
    # the narration is taken back (empty text), the read's row, then what the cut call streamed
    assert [e.type for e in ev] == ["text_delta", "tool_start", "tool_end", "text_delta",
                                    "error"]
    err = ev[-1]
    assert err.interrupted and err.code == "llm_unavailable"
    assert fold(ev) == "方差是"
    (te,) = logged(deps, "turn_error")
    assert te["partial"] == "方差是"
    assert logged(deps, "assistant") == []                        # never logged as an answer


def test_a_busy_failure_before_any_text_is_not_interrupted_and_clears_live_text(tmp_path):
    deps = build(tmp_path, [ProviderBusyError("busy", provider="deepseek")])
    ev = list(run_turn(deps, INFO, MSG, live=Recorder()))
    assert [e.type for e in ev] == ["text_delta", "error"]
    assert (ev[0].at, ev[0].text) == (0, "")                      # drops uncommitted live text
    assert ev[-1].code == "llm_busy" and not ev[-1].interrupted
    assert ev[-1].detail == BUSY_SWITCH
    assert "partial" not in logged(deps, "turn_error")[0]


def test_a_timeout_is_its_own_error_with_its_line_not_busy(tmp_path):
    deps = build(tmp_path, [ProviderTimeoutError("stalled", provider="deepseek")])
    ev = list(run_turn(deps, INFO, MSG, live=Recorder()))
    assert (ev[-1].code, ev[-1].retryable, ev[-1].detail) == ("llm_timeout", True, TIMEOUT_TEXT)
    assert logged(deps, "turn_error")[0]["code"] == "llm_timeout"


def test_an_inline_failure_without_live_text_emits_no_text(tmp_path):
    deps = build(tmp_path, [ProviderTransientError("down")])
    ev = list(run_turn(deps, INFO, MSG))
    assert [e.type for e in ev] == ["error"] and not ev[-1].interrupted


# ---- the follower's merge (no DBOS: its live side only) ----

def test_follower_hands_out_live_text_only_at_its_cursor():
    bus = LiveBus()
    f = Follower("turn:s1:m1", after=2, bus=bus)
    sink = bus.sink("turn:s1:m1", 3, 10, "\n\n")
    sink.text("早")
    assert f._live() is None                          # that call commits at 3; the client is at 2
    f.cursor = 3
    assert f._live() == TextDelta(at=10, text="\n\n早")
    sink.text("上")
    sink.text("好")
    assert f._live() == TextDelta(at=13, text="上好")  # coalesced, from where it left off
    assert f._live() is None
    f.cursor = 4                                      # committed: the snapshot is stale now
    sink.text("安")
    assert f._live() is None
    bus.end("turn:s1:m1")
    assert bus.snapshot("turn:s1:m1") is None


def test_live_text_folds_to_the_committed_text():
    bus = LiveBus()
    f = Follower("w", bus=bus)
    sink = bus.sink("w", 0, 0, "")
    shown = []
    for chunk in ("方差", "大的", "方向"):
        sink.text(chunk)
        shown.append(f._live())
    committed = TextDelta(at=0, text="方差大的方向")
    assert fold(shown) == "方差大的方向" == fold([*shown, committed])   # resend changes nothing


# ---- DBOS (Postgres) ----

@pytest.fixture
def dbos_env(db_url, tmp_path):
    from evowiki.agent import dbos_app

    def make(script, **kw):
        deps = build(tmp_path, script, **kw)

        class Applier:
            def apply(self, user_id, draft):
                return ()
        svc = AssistantService(deps, Applier(), DbosTurnRuns())
        turn_runs.install(svc)
        return svc

    dbos_app.launch(db_url, app_version="t")
    yield make
    dbos_app.shutdown()
    turn_runs.uninstall()


def test_dbos_live_text_reaches_the_client_while_the_model_still_generates(dbos_env):
    """The provider is held after its first chunk: the follower already hands that chunk out
    (it is inside the step: only the live bus can carry it). Then the committed text, the same
    answer, and `done`; a client resuming from any cursor folds to the same answer."""
    from dbos import DBOS

    gate = threading.Event()

    def hold(i):
        if i == 1:
            assert gate.wait(30), "never released"

    svc = dbos_env([ChatTurn(text="方差大的方向，信息最多。")], chunk=3, on_chunk=hold)
    it = svc.send("u1", "s1", MSG)
    first = next(it)
    assert first == TextDelta(at=0, text="方差大")       # live, before the step returned
    assert it.cursor == 0                                  # live text does not move the cursor
    gate.set()
    rest = list(it)
    events = [first, *rest]
    assert [e.type for e in rest][-1:] == ["done"]
    answer = fold(events)
    assert answer == "方差大的方向，信息最多。"
    stored = [e.payload for e in svc.turn_deps.store.entries("s1") if e.type == "assistant"]
    assert stored[0]["text"] == answer
    wid = DBOS.list_workflows(workflow_id_prefix="turn:s1:", load_input=False,
                              load_output=False)[0].workflow_id
    mid = wid.split(":", 2)[2]
    # the stream itself: the committed text once, no live text
    durable_events = [turn_runs.decode_event(x) for x in DBOS.read_stream(wid, "events")]
    assert [e.type for e in durable_events] == ["text_delta", "done"]
    # resume from every cursor: what was shown plus what follows folds to the same answer
    for n in range(len(durable_events)):
        resumed = list(svc.resume("u1", "s1", mid, n))
        assert resumed == durable_events[n:]
        assert fold(resumed, start=fold(durable_events[:n])) == answer


def test_dbos_a_resuming_client_gets_the_live_text_of_the_call_in_flight(dbos_env):
    gate = threading.Event()

    def hold(i):
        if i == 1:
            assert gate.wait(30), "never released"

    svc = dbos_env([ChatTurn(text="一二三四五六")], chunk=2, on_chunk=hold)
    it = svc.send("u1", "s1", MSG)
    try:
        assert next(it) == TextDelta(at=0, text="一二")
        it.close()                                        # the connection drops
        again = svc.resume("u1", "s1", it.message_id, 0)
        assert next(again) == TextDelta(at=0, text="一二")  # the live text so far, from 0
    finally:
        gate.set()
    rest = list(again)
    assert fold(rest, start="一二") == "一二三四五六" and rest[-1].type == "done"


def test_dbos_an_unknown_turn_cannot_be_resumed(dbos_env):
    svc = dbos_env([ChatTurn(text="答")])
    with pytest.raises(LookupError):
        svc.resume("u1", "s1", "msg_nope", 0)
    with pytest.raises(PermissionError):
        svc.resume("u2", "s1", "msg_nope", 0)


def test_inline_assistant_has_nothing_to_resume(tmp_path):
    deps = build(tmp_path, [ChatTurn(text="答")])

    class Applier:
        def apply(self, user_id, draft):
            return ()
    with pytest.raises(LookupError):
        AssistantService(deps, Applier()).resume("u1", "s1", "m", 0)


# ---- a long tool's step lines (LiveBus.status) ----

def test_follower_hands_out_a_tool_step_line_once_per_change():
    from evowiki.assistant.contracts import StatusLine
    bus = LiveBus()
    f = Follower("w", bus=bus)
    assert f._status() is None
    bus.status("w", "正在把目标分成几大块")
    assert f._status() == StatusLine(text="正在把目标分成几大块")
    assert f._status() is None                       # once
    bus.status("w", "正在把目标分成几大块")          # the same line again: no change
    assert f._status() is None
    bus.status("w", "正在检查分块")
    assert f._status() == StatusLine(text="正在检查分块")
    bus.status("w", "")                              # cleared: nothing to show
    assert f._status() is None
    bus.status("w", "正在检查分块")
    bus.end("w")
    assert bus.status_of("w") == (0, "")


def test_dbos_a_long_tool_says_its_steps_while_it_runs(dbos_env):
    """A tool that reports progress (ToolContext.progress) and is held: the client already gets
    the step line as a StatusLine (the tool runs inside a step: only the live bus carries it);
    the durable stream has no such line."""
    from dbos import DBOS

    from evowiki.agent.tools import ToolResult
    from evowiki.assistant.contracts import StatusLine
    from tests.assistant.fakes import FakeTool

    gate = threading.Event()

    class Slow(FakeTool):
        def run(self, args, ctx):
            ctx.progress("正在把目标分成几大块")
            assert gate.wait(30), "never released"
            return ToolResult(content="好了")

    svc = dbos_env([ChatTurn(tool_calls=(call("read_k"),)), ChatTurn(text="答")])
    svc.turn_deps.tools = [Slow("read_k", "read")]
    it = svc.send("u1", "s1", MSG)
    seen = []
    for ev in it:
        seen.append(ev)
        if ev == StatusLine(text="正在把目标分成几大块"):
            break
    gate.set()
    seen += list(it)
    assert seen[-1].type == "done"
    wid = DBOS.list_workflows(workflow_id_prefix="turn:s1:", load_input=False,
                              load_output=False)[0].workflow_id
    durable_events = [turn_runs.decode_event(x) for x in DBOS.read_stream(wid, "events")]
    assert all(e != StatusLine(text="正在把目标分成几大块") for e in durable_events)
