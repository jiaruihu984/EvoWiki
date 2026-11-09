"""A round that fails mid-way keeps the LLM calls it already made: the page writer's `calls` list
and the assistant turn's call count. (B1b rewrites the lines that append to these lists; these
tests pin the behaviour across that change.)"""

import pytest

from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import run_turn
from evowiki.deep.agent import RefineFailed, refine
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import ProviderUnavailableError
from evowiki.llm.usage import Account
from evowiki.wikiapp.repo import MemoryRepo
from tests.assistant.test_loop import INFO, setup
from tests.deep.fakes import DeepScript, FakeChecker, library, section, turn
from tests.deep.test_writer import outline_input
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_page import organized


def test_writer_round_failing_midway_keeps_its_earlier_calls():
    env = (MemoryRepo(["u1"]), Script(), Ids())
    pid = organized(env)
    ds = DeepScript().plan().write(turn(section("s1", role="organizer", depth="quick")),
                                   ProviderUnavailableError("down"))
    gw, _ = ds.llm().gateway(None)
    calls: list = []
    with pytest.raises(RefineFailed) as e:
        refine(outline_input(env, pid), gw, Account(user_id="u1", group="page"), FakeChecker(),
               calls=calls, library=library())
    assert e.value.failure == "llm_unavailable"
    assert [c.purpose for c in calls] == ["deep.write", "deep.write"]   # plan_page and write turn 1


class _Then:
    """An iterator over scripted turns; an exception in it is raised when reached."""

    def __init__(self, *items):
        self.items = iter(items)

    def __iter__(self):
        return self

    def __next__(self):
        out = next(self.items)
        if isinstance(out, Exception):
            raise out
        return out


def test_assistant_turn_failing_midway_logged_its_first_call(tmp_path):
    deps, _, _ = setup(tmp_path, _Then(
        ChatTurn(tool_calls=(ToolCall(id="c1", name="read_k", args={}),)),
        ProviderUnavailableError("down")))
    ev = list(run_turn(deps, INFO, MessageIn(text="问")))
    assert ev[-1].type == "error" and ev[-1].code == "llm_unavailable"
    assert not any(e.type == "done" for e in ev)
    # the first model call is in the call log, and its tool result is in the session log
    assert len((tmp_path / "c.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert [e.type for e in deps.store.entries("s1")].count("tool") == 1


def test_assistant_turn_counts_every_call_once(tmp_path):
    deps, _, _ = setup(tmp_path, [ChatTurn(tool_calls=(ToolCall(id="c1", name="read_k", args={}),)),
                                  ChatTurn(text="答")])
    done = list(run_turn(deps, INFO, MessageIn(text="问")))[-1]
    assert done.type == "done" and done.llm_calls == 2 and done.tool_calls == 1
