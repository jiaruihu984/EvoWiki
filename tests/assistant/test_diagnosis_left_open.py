"""He left a diagnosis half answered (user ruling 2026-11-05): what he did not answer is no information, the goal waits, and
going back to the goal goes back to the diagnosis at what is left. Memory repo, scripted chat; nothing paid."""

from dataclasses import replace

import pytest

from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import run_turn
from evowiki.assistant.tools.check import CheckPayload
from evowiki.assistant.tools.read_more import ReadDeps, build_read_tools
from evowiki.assistant.tools.rough import AskRoughTool, SkipDiagnosisTool
from evowiki.harness.tool_names import TOOLS
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.wikiapp.goal import enter_region, record_rough, rough_blocks, stop_draft
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import WikiConflict
from tests.assistant.test_confirm_starts_diagnosis import (  # noqa: F401
    ask_check_of,
    confirm,
    ctx,
    env,
    names,
    opened,
    turn_env,
)
from tests.wikiapp.test_goal import NOW


def half_marked(env):  # noqa: F811
    """Two of the four blocks marked and submitted; the diagnosis still has questions."""
    repo, s, ids = env
    did = opened(env).draft.draft_id
    _, blocks = rough_blocks(repo.load("u1"), did, NOW)
    record_rough(repo, CountingGateway(s.gateway()), "u1", did, "c1", {blocks[0][0]: "know", blocks[1][0]: "partly"},
                 now=NOW, new_id=ids)
    assert did not in repo.load("u1").state.closed_drafts
    return did, blocks


def with_skip(env, deps):  # noqa: F811
    repo, s, ids = env
    skip = SkipDiagnosisTool(repo, lambda _u: CountingGateway(s.gateway()), lambda: NOW, ids)
    return replace(deps, tools=[*deps.tools, skip])


def reads(repo):
    return {t.spec.name: t for t in build_read_tools(ReadDeps(repo=repo, clock=lambda: NOW))}


def test_going_back_shows_only_the_blocks_he_left(env, tmp_path):  # noqa: F811
    repo = env[0]
    did, blocks = half_marked(env)
    deps, info, _store, chat = turn_env(env, tmp_path, ask_check_of(repo))
    ev = list(run_turn(deps, info, MessageIn(text="继续摸底", intent="diagnose", draft_id=did)))
    ask = next(e for e in ev if e.type == "ask")
    assert ask.kind == "ask_rough" and [b["id"] for b in ask.payload["blocks"]] == [b for b, _ in blocks[2:]]
    assert chat.seen == []


def test_start_in_another_conversation_ends_nothing(env, tmp_path):  # noqa: F811
    """「开始吧」 with no card here: skip_diagnosis is not offered, the diagnosis stays open, the answer is the model's text."""
    repo = env[0]
    did, _blocks = half_marked(env)
    turns = iter([ChatTurn(tool_calls=(ToolCall(id="l1", name="load_skill", args={"skills": ["assess"]}),)),
                  ChatTurn(tool_calls=(ToolCall(id="s1", name="skip_diagnosis", args={"draft_id": did}),)),
                  ChatTurn(text="你想接着学哪个目标？")])
    deps, info, _store, chat = turn_env(env, tmp_path, lambda: next(turns))
    deps = with_skip(env, deps)
    before = repo.load("u1").state.drafts[did]
    list(run_turn(deps, info, MessageIn(text="开始吧")))
    assert all("skip_diagnosis" not in names(r) for r in chat.seen)
    state = repo.load("u1").state
    assert did not in state.closed_drafts and state.drafts[did] == before


def test_skip_stays_with_the_card_on_screen(env, tmp_path):  # noqa: F811
    """A card he answers still offers it: 不问了 over a card ends the diagnosis at once."""
    repo = env[0]
    res = opened(env)
    deps, info, _store, chat = turn_env(env, tmp_path, ask_check_of(repo))
    deps = with_skip(env, deps)
    list(run_turn(deps, info, confirm(res)))
    list(run_turn(deps, info, MessageIn(text="这块讲什么？")))
    assert "skip_diagnosis" in names(chat.seen[0])


def test_no_tool_picks_a_question_on_its_own():
    assert "next_check" not in TOOLS["assistant"]


@pytest.mark.parametrize("tool", ["read_goals", "today_plan"])
def test_the_goal_tools_name_what_is_left_and_the_draft(env, tool):  # noqa: F811
    repo = env[0]
    did, blocks = half_marked(env)
    text = reads(repo)[tool].run({}, ctx()).content
    line = next(ln for ln in text.splitlines() if did in ln)
    assert "摸底" in line and blocks[2][1] in line


def test_the_goal_tools_say_nothing_once_it_ended(env):  # noqa: F811
    repo, s, ids = env
    res = opened(env)
    stop_draft(repo, CountingGateway(s.gateway()), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    assert "摸底" not in reads(repo)["read_goals"].run({}, ctx()).content


def test_no_block_is_entered_while_the_opening_diagnosis_is_open(env):  # noqa: F811
    repo, s, ids = env
    did, blocks = half_marked(env)
    map_id = repo.load("u1").state.drafts[did].map_id
    with pytest.raises(WikiConflict):
        enter_region(repo, CountingGateway(s.gateway()), "u1", map_id, blocks[2][0], now=NOW, new_id=ids)


def test_ask_rough_in_another_conversation_gives_the_same_grid(env):  # noqa: F811
    """He said 接着学这个目标 elsewhere: the tool the goal tools named shows what is left."""
    repo, s, ids = env
    did, blocks = half_marked(env)
    out = AskRoughTool(repo, lambda _u: CountingGateway(s.gateway()), lambda: NOW, ids).run({"draft_id": did}, ctx())
    assert out.pause is not None
    assert [b["id"] for b in out.pause.payload["blocks"]] == [b for b, _ in blocks[2:]]


def test_a_question_card_names_its_draft():
    """The goal goes on after a diagnosis answered in another conversation: the card says whose it is."""
    assert "draft_id" in CheckPayload.model_fields
