"""The page writer and the reading assistant start and finish a trace run around their loop, with
a run id that a durable replay finds again; a failed finish never hides the run's own error."""

import logging
import re
from dataclasses import replace

import pytest

from evowiki.agent import durable
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import run_turn
from evowiki.deep.agent import PROMPT_VERSION, RefineFailed, refine
from evowiki.deep.skill import version as skill_version
from evowiki.deep.writer import PageWriter
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import ProviderUnavailableError
from evowiki.llm.providers import ProviderTransientError
from evowiki.llm.usage import Account
from evowiki.wikiapp.repo import MemoryRepo
from tests.agent.test_call_accounting import _Then
from tests.agent.test_loop_trace import Recorder
from tests.assistant.test_loop import INFO, setup
from tests.deep.fakes import DeepScript, FakeChecker, finish, library, section, turn
from tests.deep.test_writer import FALLBACK, outline_input, write_all
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_page import organized


class BadFinish(Recorder):
    """A sink whose finish fails (after recording the call)."""

    def finish(self, run_id, *, stop, error):
        super().finish(run_id, stop=stop, error=error)
        raise RuntimeError("trace db down")


def _env():
    env = (MemoryRepo(["u1"]), Script(), Ids())
    return env, organized(env)


def _refine(trace, *writes):
    env, pid = _env()
    gw, _ = DeepScript().plan().write(*(writes or write_all())).llm().gateway(None)
    refine(outline_input(env, pid), gw, Account(user_id="u1", group="page"), FakeChecker(),
           library=library(), trace=trace)
    return pid


def test_a_write_round_is_a_run():
    sink = Recorder()
    pid = _refine(sink)
    start, *steps, finish = sink.calls
    rid = start[1]
    assert re.fullmatch(rf"refine:{pid}:[0-9a-f]{{32}}:write:0:w0", rid)
    assert start[2:6] == ("u1", "deep.write", pid, 0)
    assert start[6]["page_id"] == pid and start[6]["round"] == 0 and start[6]["must_fix"] == []
    assert set(start[6]) == {"page_id", "title", "round", "must_fix", "snapshot", "mode",
                             "instruction", "scope", "spec_version", "page", "domain",
                             "prompt_version", "skill", "language"}  # What a replay pins.
    assert start[6]["language"] == "zh"
    assert start[6]["prompt_version"] == PROMPT_VERSION and start[6]["skill"] == skill_version()
    # plan_page, the three sections, finish: five model + tool steps
    assert [s[0] for s in steps] == ["model", "tool"] * 5 and steps[-1][4] == "finish"
    assert steps[1][4] == "plan_page"
    assert all(s[1] == rid for s in steps) and [s[2] for s in steps] == list(range(1, 11))
    assert finish == ("finish", rid, "finished", None)


def test_in_a_workflow_the_run_id_is_the_workflow_s():
    sink = Recorder()
    with durable.virtual_workflow("wf-1"):
        _refine(sink)
    assert sink.calls[0][1] == "wf-1:write:0:w0"


def test_an_assistant_turn_is_a_run(tmp_path):
    deps, _, _ = setup(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="read_k", args={}),)),
                                  ChatTurn(text="答")])
    deps.trace = sink = Recorder()
    list(run_turn(deps, INFO, MessageIn(text="问")))
    start, m1, t2, m3, finish = sink.calls
    mid = next(e for e in deps.store.entries("s1") if e.type == "user").payload["message_id"]
    rid = f"assistant:s1:{mid}"      # the question's message id
    assert start == ("start", rid, "u1", "assistant.turn", "s1", 0,
                     {"session_id": "s1", "page_id": "p1", "message_id": mid})
    assert [(s[0], s[1], s[2]) for s in (m1, t2, m3)] == [
        ("model", rid, 1), ("tool", rid, 2), ("model", rid, 3)]
    assert finish == ("finish", rid, "answered", None)


def test_a_failed_assistant_turn_finishes_with_the_error(tmp_path):
    deps, _, _ = setup(tmp_path, _Then(ProviderUnavailableError("down")))
    deps.trace = sink = Recorder()
    list(run_turn(deps, INFO, MessageIn(text="问")))
    start, model, finish = sink.calls
    assert model[0] == "model" and model[4] is None and model[6] == "down"
    assert finish == ("finish", start[1], None, "ProviderUnavailableError")


def test_each_writer_of_a_round_is_its_own_run():
    env, pid = _env()
    sink = Recorder()
    ds = DeepScript().plan().write(
        turn(section("s1", role="organizer", depth="quick")),
        ProviderTransientError("429 rate limited"),          # the primary goes away mid-round
        turn(section("s2")), turn(section("s3", role="recap", depth="quick")), turn(finish()))
    llm = replace(ds.llm(), fallback=FALLBACK, trace=sink)
    PageWriter(llm=llm, checker=FakeChecker(), library=library()).write(outline_input(env, pid))
    runs = {}
    for c in sink.calls:
        runs.setdefault(c[1], []).append(c)
    primary, fallback = runs
    assert primary.endswith(":write:0:w0") and fallback == primary[:-1] + "1"
    p, f = runs[primary], runs[fallback]
    # plan_page and s1 on the primary, then the 429
    assert [(c[0], c[2]) for c in p[1:-1]] == [("model", 1), ("tool", 2), ("model", 3),
                                               ("tool", 4), ("model", 5)]
    assert p[5][4] is None and "429" in p[5][6]
    assert p[-1] == ("finish", primary, None, "ProviderTransientError")
    assert f[0][0] == "start" and [c[2] for c in f[1:-1]] == list(range(1, 7))
    assert f[-1] == ("finish", fallback, "finished", None)


def test_a_failed_finish_never_hides_the_writer_s_error(caplog):
    with (caplog.at_level(logging.ERROR, logger="evowiki.agent.trace"),
          pytest.raises(RefineFailed) as e):
        _refine(BadFinish(), ProviderTransientError("503"))
    assert e.value.failure == "llm_unavailable"
    assert any("trace finish" in n and "trace db down" in n
               for n in getattr(e.value.__cause__, "__notes__", []))
    assert "finish failed" in caplog.text


def test_a_failed_finish_of_a_finished_round_fails_the_refine():
    with pytest.raises(RefineFailed) as e:
        _refine(BadFinish())
    assert e.value.failure == "internal" and "trace db down" in e.value.detail


def test_a_failed_finish_never_hides_the_turn_s_error(tmp_path):
    deps, _, _ = setup(tmp_path, _Then(ProviderUnavailableError("down")))
    deps.trace = BadFinish()
    ev = list(run_turn(deps, INFO, MessageIn(text="问")))
    assert ev[-1].type == "error" and ev[-1].code == "llm_unavailable"


def test_a_failed_finish_of_an_answered_turn_fails_the_turn(tmp_path):
    deps, _, _ = setup(tmp_path, [ChatTurn(text="答")])
    deps.trace = BadFinish()
    ev = list(run_turn(deps, INFO, MessageIn(text="问")))
    assert ev[-1].type == "error" and not any(e.type == "done" for e in ev)


def test_a_disconnect_stays_a_disconnect_when_finish_fails(tmp_path):
    deps, _, _ = setup(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="read_k", args={}),)),
                                  ChatTurn(text="答")])
    deps.trace = sink = BadFinish()
    gen = run_turn(deps, INFO, MessageIn(text="问"))
    next(gen)
    gen.close()                      # no RuntimeError: GeneratorExit is what propagated
    assert sink.calls[-1][2:] == (None, "GeneratorExit")
