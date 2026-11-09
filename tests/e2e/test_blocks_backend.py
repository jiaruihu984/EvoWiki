"""The blocks walkthrough backend's scripted assistant turn calls the real tools with arguments the
tools accept (an argument the tool forbids makes the tool refuse, and the fake would still say it
worked — the walkthrough then waits for a card that never comes). No LLM, no database."""

import pytest

from evowiki.assistant.tools.actions import EditGoalBlocksArgs, NewGoalArgs
from evowiki.goals.blocks import Block, BlockOp, apply_block_ops
from evowiki.llm.chat import ChatMessage, ChatRequest, ToolDef
from tests.e2e.blocks_backend import assistant_turn, build_script

# stage 3: a plain chat turn offers the core tools; plan-goal's (new_goal, edit_goal_blocks) come
# with a load_skill, or up front when code preloads plan-goal (learn_goal, an open blocks draft)
PLAN_GOAL = ("new_goal", "edit_goal_blocks")


def _turn(*texts: str, tools: tuple[str, ...] = ()):
    req = ChatRequest(purpose="assistant.turn", prompt_version="t", subject="s", system="",
                      messages=tuple(ChatMessage(role="user", text=t) for t in texts),
                      tools=tuple(ToolDef(name=n, description=n, parameters={"type": "object"})
                                  for n in tools))
    return assistant_turn(build_script(), req)


def test_the_merge_call_is_valid_and_keeps_the_block_the_learner_merged_into():
    # an open blocks draft preloads plan-goal: edit_goal_blocks is offered from the first call
    out = _turn("我要学黑壳虾", "水质与环境和开缸与日常合成一块", tools=PLAN_GOAL)
    (call,) = out.tool_calls
    assert call.name == "edit_goal_blocks"
    args = EditGoalBlocksArgs.model_validate(call.args)     # extra="forbid": a stray key fails here
    (op,) = args.ops
    blocks = (Block(name="分类与鉴别", level="基础"), Block(name="水质与环境", level="基础"),
              Block(name="开缸与日常", level="核心"))
    merged, dropped = apply_block_ops(blocks, [BlockOp(op="merge", names=tuple(op.names),
                                                       into=op.names[0])])
    assert dropped == [] or dropped == ()
    assert [(b.name, b.level) for b in merged] == [("分类与鉴别", "基础"), ("开缸与日常", "核心")]


@pytest.mark.parametrize("text", ["我要学黑壳虾\n背景：学过高中生物"])
def test_the_first_call_is_a_valid_new_goal(text):
    # a plain chat message carries no structured signal: plan-goal is loaded first
    (load,) = _turn(text).tool_calls
    assert load.name == "load_skill" and load.args == {"skills": ["plan-goal"]}
    out = _turn(text, tools=PLAN_GOAL)
    (call,) = out.tool_calls
    assert call.name == "new_goal" and not out.text   # assistant@v23: the card's lead is the tool's
    NewGoalArgs.model_validate(call.args)


def test_the_limits_edit_is_a_valid_edit_goal_blocks():
    out = _turn("我要学黑壳虾，想尽快读到第一页", "其实不急", tools=PLAN_GOAL)
    (call,) = out.tool_calls
    args = EditGoalBlocksArgs.model_validate(call.args)
    assert args.limits == [] and args.ops == []


def test_s1_reply_has_the_goal_record():
    from tests.e2e.blocks_backend import intake_reply
    out = intake_reply("把学习目标分成几大块\n学习目标：「我要学黑壳虾」\n他为这个目标说的原话：\n"
                       "（以下是资料内容）\n<<<资料内容开始>>>\n完全零基础，想尽快读到第一页\n"
                       "<<<资料内容结束>>>\nframing")
    assert out["stated_beginner"] and out["beginner_quote"] == "完全零基础"
    assert out["limits"] == ["尽快读到第一页"]


def test_the_scripted_intake_answers_the_real_blocks_prompt():
    """The blocks prompt's wording moved (「…把学习目标分成几大块」): the fail-closed script raised
    Unscripted (a BaseException) inside new_goal's step and the e2e turns hung PENDING."""
    from evowiki.goals.blocks import blocks_prompt
    from tests.e2e.blocks_backend import intake_reply
    out = intake_reply(blocks_prompt("我要学黑壳虾", ["完全零基础"], [], "zh"))
    assert out["blocks"] and out["stated_beginner"]
