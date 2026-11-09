"""The goal record (S1, D07 / D09) through the assistant's tools and the goal_new executor: a chat
edit keeps the stated beginner and can change the limits, `words` must be his, set_goal_limits
refuses what changes nothing, and a block added on the card of a beginner's goal is not
diagnosed (neither before nor after the confirm)."""

from evowiki.agent.tools import ToolContext
from evowiki.assistant.tools.actions import WORDS_NOT_QUOTED
from evowiki.goals.intake import OUTLINE_PURPOSE
from tests.assistant.test_goal_blocks_tools import BLOCKS, GOAL, env  # noqa: F401 — the fixture

SAID = "我想养黑壳虾，完全零基础"
BEGINNER = {**BLOCKS, "stated_beginner": True, "beginner_quote": "完全零基础"}


def beginner_row(env):  # noqa: F811 — the fixture
    env.scripts["s"].push(OUTLINE_PURPOSE, BEGINNER)
    ctx = ToolContext(user_id="u1", session_id="s1", user_words=(SAID,))
    res = env.tools["new_goal"].run({"goal": GOAL, "words": ["完全零基础"]}, ctx)
    assert res.error is None, res.content
    return env.ledger.get("u1", res.action.action_id)


def test_a_chat_edit_keeps_the_stated_beginner_and_does_not_diagnose_an_added_block(env):  # noqa: F811
    row = beginner_row(env)
    assert row.content["beginner"] == "完全零基础"
    assert all(not b["diagnose"] and b["said"] == ["完全零基础"] for b in row.content["blocks"])
    res = env.run("edit_goal_blocks", ops=[{"op": "add", "name": "造景", "covers": ["水草"]}])
    assert res.error is None, res.content
    new = env.ledger.get("u1", res.action.action_id)
    assert new.content["beginner"] == "完全零基础"
    added = next(b for b in new.content["blocks"] if b["name"] == "造景")
    assert (added["diagnose"], added["said"], added["said_level"]) == (
        False, ["完全零基础"], "unlearned")


def test_a_chat_edit_of_the_limits_changes_only_the_limits(env):  # noqa: F811
    env.scripts["s"].push(OUTLINE_PURPOSE, {**BLOCKS, "limits": ["想尽快养起来"]})
    old = env.ledger.get("u1", env.run("new_goal", goal=GOAL).action.action_id)
    assert old.content["limits"] == ["想尽快养起来"]
    res = env.run("edit_goal_blocks", limits=["只用周末", " ", "只用周末"])
    assert res.error is None, res.content
    new = env.ledger.get("u1", res.action.action_id)
    assert new.content["limits"] == ["只用周末"]
    assert new.content["blocks"] == old.content["blocks"]
    assert "这个目标的条件改成了：只用周末" in res.content
    cleared = env.ledger.get("u1", env.run("edit_goal_blocks", limits=[]).action.action_id)
    assert cleared.content["limits"] == []


def test_words_that_are_not_his_are_refused_and_nothing_is_written(env):  # noqa: F811
    n = len(env.rows())
    res = env.run("new_goal", goal=GOAL, words=["我学过一点"])      # he wrote no such thing here
    assert res.error is not None and WORDS_NOT_QUOTED in res.content
    assert len(env.rows()) == n
    assert env.scripts["s"].calls(OUTLINE_PURPOSE) == 0              # refused before S1


def test_set_goal_limits_refuses_an_unknown_goal_and_the_same_limits(env):  # noqa: F811
    unknown = env.run("set_goal_limits", goal_id="map-nope", limits=["只用周末"])
    assert unknown.error is not None and "没有这个大目标：map-nope" in unknown.content
    env.scripts["s"].push(OUTLINE_PURPOSE, {**BLOCKS, "limits": ["想尽快养起来"]})
    row = env.ledger.get("u1", env.run("new_goal", goal=GOAL).action.action_id)
    done = env.ledger.approve("u1", row.action_id)
    st = env.state()
    map_id = done.result["map_id"] or st.drafts[done.result["draft_id"]].map_id
    same = env.run("set_goal_limits", goal_id=map_id, limits=["想尽快养起来", " "])
    assert same.error is not None and "这个目标的条件已经是这样了" in same.content
    changed = env.run("set_goal_limits", goal_id=map_id, limits=[])
    assert changed.error is None, changed.content
    assert "它不是上限" in changed.content and "不要说设好了或已生效" in changed.content
    assert env.state().maps[map_id].limits == ()
    # the undo puts back the limits the change replaced
    assert env.run("undo", action_id=changed.action.action_id).error is None
    assert env.state().maps[map_id].limits == ("想尽快养起来",)


def test_a_block_added_on_the_card_of_a_beginners_goal_is_not_diagnosed(env):  # noqa: F811
    row = beginner_row(env)
    done = env.ledger.approve("u1", row.action_id, draft={"pages": [
        {"key": "b1"}, {"key": "b2"}, {"key": "b3"}, {"title": "造景"}]})
    assert done.status == "executed", done.detail
    added = done.content["blocks"][-1]
    assert (added["name"], added["diagnose"], added["said"]) == ("造景", False, ["完全零基础"])
    st = env.state()
    m = st.maps[done.result["map_id"] or st.drafts[done.result["draft_id"]].map_id]
    assert m.beginner == "完全零基础"
    assert all(not n.diagnose and n.said == ("完全零基础",) for n in m.nodes)


def _blocks(e, map_id):
    return [n.name for n in e.state().maps[map_id].children(None) if n.kind == "region"]


def test_a_chat_edit_of_the_path_is_a_ledger_row_and_undoes_as_one(env):  # noqa: F811
    """Run 110500597858 act-edit-goal-blocks: edit_path_blocks changed the map with no row, so nothing could undo it."""
    env.scripts["s"].push(OUTLINE_PURPOSE, BLOCKS)
    row = env.ledger.get("u1", env.run("new_goal", goal=GOAL).action.action_id)
    done = env.ledger.approve("u1", row.action_id)
    map_id = done.result["map_id"] or env.state().drafts[done.result["draft_id"]].map_id
    before = _blocks(env, map_id)
    res = env.run("edit_path_blocks", goal_id=map_id, ops=[
        {"op": "add", "name": "合成的一块", "position": 1},
        {"op": "rename", "name": before[-1], "new_name": "改名的一块"},
        {"op": "drop", "name": "没有这一块"}])
    assert res.error is None, res.content
    assert res.action.status == "executed" and res.action.can_undo and "没有这一块" in res.content
    assert _blocks(env, map_id)[0] == "合成的一块" and "改名的一块" in _blocks(env, map_id)
    assert "合成的一块" in res.action.title
    assert env.ledger.undo("u1", res.action.action_id).status == "undone"
    assert _blocks(env, map_id) == before
    nothing = env.run("edit_path_blocks", goal_id=map_id, ops=[{"op": "drop", "name": "没有这一块"}])
    assert nothing.error is not None and _blocks(env, map_id) == before
    assert env.run("edit_path_blocks", goal_id="map-nope", ops=[{"op": "drop", "name": "x"}]).error is not None
