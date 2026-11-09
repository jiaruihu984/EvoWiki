"""The chat's stop (assistant.loop module doc): the turn ends itself, what was out stays, the next
message goes out at once; the answer is in the session's history and in no memory."""

import time

import pytest

from evowiki.assistant import turn_runs
from evowiki.assistant.compact import _answered
from evowiki.assistant.contracts import Done, ErrorEv, MessageIn, TextDelta
from evowiki.assistant.loop import STOPPED_NOTE, TurnDeps, run_turn
from evowiki.assistant.service import AssistantService
from evowiki.assistant.session import MemorySessionStore, fold
from evowiki.assistant.turn_runs import DbosTurnRuns, InlineTurnRuns
from evowiki.llm.chat import ChatTurn
from evowiki.llm.deadline import RequestCancelled, cancel_requested
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.memory.consolidate import _messages
from tests.assistant.fakes import INFO, T0, Data, Ids, tools
from tests.assistant.test_loop import CFG, call, setup, types

LONG = "这是一段很长的回答，" * 20


class Applier:
    def apply(self, user_id, draft):
        return ("s9",)


def run_stopping_at(deps, event_type):
    """The turn with a stop request as the first `event_type` goes out."""
    out = []
    for ev in run_turn(deps, INFO, MessageIn(text="问")):
        out.append(ev)
        if ev.type == event_type:
            deps.store.request_stop(INFO.session_id, msg_id(deps), T0)
    return out


def msg_id(deps):
    return next(e.payload["message_id"] for e in reversed(deps.store.entries(INFO.session_id))
                if e.type == "user")


def answers(deps):
    return [e.payload for e in deps.store.entries(INFO.session_id) if e.type == "assistant"]


def test_no_tool_starts_after_the_stop_and_the_one_that_ran_stays(tmp_path):
    two_reads = ChatTurn(tool_calls=(call("read_k", 1), call("read_k", 2)))
    deps, prov, _ = setup(tmp_path, [two_reads])
    out = run_stopping_at(deps, "tool_end")
    assert len(prov.chat_calls) == 1                     # no model call after it
    tool_entries = [e for e in deps.store.entries("s1") if e.type == "tool"]
    assert [e.payload["call_id"] for e in tool_entries] == ["c1"]
    assert out[-1].type == "done" and out[-1].stopped and out[-1].tool_calls == 1
    assert "error" not in types(out)
    assert answers(deps)[0]["stopped"] is True and answers(deps)[0]["text"] == ""


def test_no_model_call_after_a_tool_that_finished_before_the_stop(tmp_path):
    deps, prov, _ = setup(tmp_path, [ChatTurn(tool_calls=(call("read_k", 1),))])
    out = run_stopping_at(deps, "tool_end")
    assert len(prov.chat_calls) == 1
    assert out[-1].stopped


def stream_until_stopped(tmp_path, then=()):
    """A model that streams three pieces, then waits to be cut (`RequestCancelled`, as the real
    providers do between two chunks); later calls answer from `then`."""
    later = iter(then)
    rounds = []

    def answer(m, r):
        rounds.append(r)
        return next(later) if len(rounds) > 1 else ChatTurn(text=LONG)

    def on_chunk(i):
        if len(rounds) == 1 and i == 3:
            end = time.monotonic() + 20
            while not cancel_requested():
                assert time.monotonic() < end, "the call in flight was never cut"
                time.sleep(0.01)
            raise RequestCancelled()

    prov = ScriptedChatProvider("scripted", answer, stream_chunk=5, on_chunk=on_chunk)
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})
    store = MemorySessionStore()
    store.create(INFO)
    return TurnDeps(gateway=gw, tools=tools(), store=store, data=Data(), new_id=Ids(),
                    clock=lambda: T0), prov


@pytest.fixture
def dbos_env(db_url, tmp_path):
    from evowiki.agent import dbos_app

    deps, prov = stream_until_stopped(tmp_path, [ChatTurn(text="好，换个说法。")])
    svc = AssistantService(deps, Applier(), DbosTurnRuns())
    turn_runs.install(svc)
    dbos_app.launch(db_url, app_version="t")
    yield svc, prov
    dbos_app.shutdown()
    turn_runs.uninstall()


def test_dbos_stop_cuts_the_call_in_flight_keeps_the_text_and_frees_the_session(dbos_env):
    from dbos import DBOS

    svc, prov = dbos_env
    it = svc.send("u1", "s1", MessageIn(text="写长一点"))
    seen = []
    for ev in it:                                   # the follower that sent it stays on
        seen.append(ev)
        if isinstance(ev, TextDelta) and len(seen) == 1:
            assert ev.text != LONG
            svc.stop("u1", "s1")
    assert isinstance(seen[-1], Done) and seen[-1].stopped
    assert not any(isinstance(e, ErrorEv) for e in seen)

    entries = svc.turn_deps.store.entries("s1")
    assert [e.type for e in entries] == ["user", "assistant"]            # no turn_error
    logged = entries[-1].payload
    assert logged["stopped"] is True and logged["text"] and LONG.startswith(logged["text"])
    wid = next(iter(DBOS.list_workflows(workflow_id_prefix="turn:s1:", load_input=False,
                                        load_output=False))).workflow_id
    assert DBOS.get_workflow_status(wid).status == "SUCCESS"   # ended, so never recovered

    # the next message goes out at once and sees the cut answer with the note, as it stands
    assert list(svc.send("u1", "s1", MessageIn(text="短一点")))[-1].type == "done"
    history = [m.text for m in prov.chat_calls[-1].messages]
    assert logged["text"] + STOPPED_NOTE in history
    with pytest.raises(LookupError):                # nothing is running any more
        svc.stop("u1", "s1")


def test_inline_turns_cannot_be_stopped(tmp_path):
    deps, _, _ = setup(tmp_path, [])
    with pytest.raises(LookupError):
        AssistantService(deps, Applier(), InlineTurnRuns(deps)).stop("u1", "s1")


def test_a_stopped_answer_with_no_text_is_a_history_message_of_its_note_alone(tmp_path):
    deps, prov, _ = setup(tmp_path, [ChatTurn(tool_calls=(call("read_k", 1),)),
                                     ChatTurn(text="好。")])
    run_stopping_at(deps, "tool_end")
    assert fold(INFO, deps.store.entries("s1")).messages[-1].stopped
    list(run_turn(deps, INFO, MessageIn(text="再问")))
    assert STOPPED_NOTE.strip() in [m.text for m in prov.chat_calls[-1].messages]


def test_a_stopped_answer_goes_to_no_memory_and_no_summary(tmp_path):
    deps, _, _ = setup(tmp_path, [ChatTurn(tool_calls=(call("read_k", 1),))])
    run_stopping_at(deps, "tool_end")
    entries = deps.store.entries("s1")
    assert [[m.role for m in turn] for turn in _messages(entries)] == [["user"]]
    assert _answered(fold(INFO, entries).turns) == []
