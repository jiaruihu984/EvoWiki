"""The chat side of possibly conflicting memories (assistant/tools/conflicts.py; the 冲突 Skill):
read_memory_conflicts lists his open groups; resolve_memory_conflict settles one from his words
this turn (keep one / both, or his new wording), at once and undoable; a turn opened by 「处理」
(intent resolve_memory_conflicts) has the Skill loaded and his groups read before the first model
call. Real Postgres and ledger, scripted models: no paid call."""

from dataclasses import replace

import pytest

from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import CONFLICTS_HEAD, run_turn
from evowiki.assistant.skills import preload
from evowiki.assistant.tools.conflicts import (
    NOT_NOW,
    ReadMemoryConflictsTool,
    ResolveMemoryConflictTool,
)
from evowiki.llm.chat import ChatTurn
from evowiki.memory.conflicts import RESOLVE_KIND, ConflictResolveExecutor, ConflictService
from tests.assistant.test_action_tools import INFO, chat_turns, turn_deps
from tests.assistant.test_stated_preferences import ctx, own_items, prefs_env, remember
from tests.memory.test_conflicts import _script
from tests.wikiapp.test_review_flow import LATER

RULE = "以后都先给例子再讲定义"
RULE2 = "讲定义前不要举例子"
WHY = "一条要先举例，一条不要举例"


@pytest.fixture
def world(pool):
    env = prefs_env(pool)
    for said in (RULE, RULE2):
        assert remember(env, ctx(said), quote=said, text=said).error is None
    svc = ConflictService(pool, env.repo, env.profile, clock=lambda: LATER, new_id=env.ids)
    env.ledger.register(RESOLVE_KIND, ConflictResolveExecutor(
        pool, env.repo, env.profile, clock=lambda: LATER, new_id=env.ids))
    mine = {i.text: i.item_id for i in own_items(env)}
    svc.check("u1", _script([{"ids": [mine[RULE], mine[RULE2]], "why": WHY}]).gateway(), None)
    [g] = svc.open("u1")
    deps = replace(env.deps, conflicts=svc)
    env.read, env.resolve = ReadMemoryConflictsTool(deps), ResolveMemoryConflictTool(deps)
    env.svc, env.group, env.mine = svc, g.conflict_id, mine
    return env


def _say(env, words, **args):
    return env.resolve.run({"conflict_id": env.group, "quote": words, **args},
                           ctx(words, "我想先看看", mid="m9"))


def test_read_lists_each_group_with_its_entries(world):
    out = world.read.run({}, ctx("看看")).content
    assert world.group in out and WHY in out and RULE in out and RULE2 in out
    assert "聊天里说的" in out


def test_keep_one_from_his_words_now_is_done_and_undoable(world):
    res = _say(world, "留第一条", keep=world.mine[RULE])
    assert res.error is None, res.content
    assert res.action is not None and res.action.kind == RESOLVE_KIND
    assert [i.text for i in own_items(world)] == [RULE]
    world.ledger.undo("u1", res.action.action_id)
    assert sorted(i.text for i in own_items(world)) == sorted([RULE, RULE2])
    assert [g.conflict_id for g in world.svc.open("u1")] == [world.group]


def test_both_and_the_grounding(world):
    earlier = world.resolve.run({"conflict_id": world.group, "quote": "我想先看看", "keep": "both"},
                                ctx("两条都要", "我想先看看", mid="m9"))
    assert earlier.error is not None and NOT_NOW in earlier.content       # not his words now
    nowhere = world.resolve.run({"conflict_id": world.group, "quote": "随便", "keep": "both"},
                                ctx("两条都要", mid="m9"))
    assert nowhere.error is not None
    both = _say(world, "两条都要", keep="both")
    assert both.error is None and both.action is None and world.svc.open("u1") == []
    again = _say(world, "两条都要", keep="both")
    assert again.error is not None                                        # settled already


def test_his_new_wording_replaces_the_group_at_once_and_undo_takes_it_back(world):
    said = "简单的公式直接给，复杂的先举例"
    res = _say(world, said, replace_with=said)
    assert res.error is None, res.content
    assert [(i.text, i.origin, i.authority) for i in own_items(world)] == [
        (said, "user_chat", "user")]
    decided = {d.rule_id: d.action for d in world.state().rule_decisions}
    assert decided[world.mine[RULE]] == decided[world.mine[RULE2]] == "revoked"
    assert world.svc.open("u1") == []
    world.ledger.undo("u1", res.action.action_id)
    assert sorted(i.text for i in own_items(world)) == sorted([RULE, RULE2])
    assert [g.conflict_id for g in world.svc.open("u1")] == [world.group]


def test_keep_and_replace_together_are_refused(world):
    res = _say(world, "留第一条", keep=world.mine[RULE], replace_with="别的")
    assert res.error is not None and own_items(world) and world.svc.open("u1")


def test_the_reteach_intent_preloads_page_editing():
    assert "edit-wiki" in preload(learn_goal=False, session_attachments=False,
                                  message_attachments=False, wrong_answer=False,
                                  open_blocks=False, reteach=True).skills


def test_the_handle_intent_preloads_the_skill_and_reads_the_groups_first(tmp_path, world):
    assert "conflicts" in preload(learn_goal=False, session_attachments=False,
                                  message_attachments=False, wrong_answer=False,
                                  open_blocks=False, conflicts=True).skills
    prov = chat_turns(ChatTurn(text="先看第一组。"))
    deps = turn_deps(tmp_path, world, prov, [world.read, world.resolve])
    ev = list(run_turn(deps, INFO, MessageIn(text="处理待确认的记忆",
                                             intent="resolve_memory_conflicts")))
    assert ev[-1].type == "done"
    first = prov.chat_calls[0]
    last = first.messages[-1].text
    assert CONFLICTS_HEAD in last and WHY in last and world.group in last
    assert "【技能·待确认的记忆】" in first.system
