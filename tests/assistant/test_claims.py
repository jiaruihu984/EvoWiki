"""The final answer's word against the turn's tool results (assistant.claims). The sentences are real
answers logged by the rig runs of 2026-11-03 / 04."""

import pytest

from evowiki.agent.text_visuals import has_character_diagram
from evowiki.agent.tools import ToolResult
from evowiki.assistant.claims import (
    NOT_LOOKED,
    NOTHING_CHANGED,
    ONLY_DRAFTED,
    ClaimCheck,
    claim_in,
    unbacked_in,
)
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import DRAFT_STOP, DRAFT_TRIES, CheckedTool, card_words, run_turn
from evowiki.llm.chat import ChatTurn
from tests.assistant.fakes import FakeTool
from tests.assistant.test_loop import INFO, setup


def test_a_character_diagram_is_taken_back_and_reasked_through_the_existing_answer_guard(tmp_path):
    diagram = "┌────┐\n│ 均值 │\n└────┘"
    deps, prov, _ = setup(tmp_path, [ChatTurn(text=diagram), ChatTurn(text="均值是这些数的平衡位置。")])
    list(run_turn(deps, INFO, MessageIn(text="这种时候给我图")))
    assert len(prov.chat_calls) == 2
    assert _answer(deps) == "均值是这些数的平衡位置。"
    assert _entry(deps)["reasked"] == [{"why": "character_diagram", "said": ""}]


def test_a_repeated_character_diagram_is_refused_but_code_and_tables_are_allowed(tmp_path):
    diagram = "+-----+\n| box |\n+-----+"
    deps, prov, _ = setup(tmp_path, [ChatTurn(text=diagram), ChatTurn(text=diagram)])
    list(run_turn(deps, INFO, MessageIn(text="画图")))
    assert _answer(deps) == "这张示意图没有生成成功，请重试配图。" and len(prov.chat_calls) == 2
    allowed = "```python\ns = '┌──┐'\n```\n\n| x | y |\n| --- | --- |\n| 1 | 2 |"
    deps, prov, _ = setup(tmp_path, [ChatTurn(text=allowed)])
    list(run_turn(deps, INFO, MessageIn(text="看代码和表格")))
    assert _answer(deps) == allowed and len(prov.chat_calls) == 1


def test_the_character_diagram_refusal_uses_the_turns_existing_english_language(tmp_path):
    diagram = "+-----+\n| box |\n+-----+"
    deps, prov, _ = setup(tmp_path, [ChatTurn(text=diagram), ChatTurn(text=diagram)])
    deps.data.language = lambda _uid: "en"
    list(run_turn(deps, INFO, MessageIn(text="Draw this diagram")))
    assert _answer(deps) == "The illustration could not be generated. Please try again."
    assert len(prov.chat_calls) == 2


def test_a_character_diagram_with_a_pausing_question_is_removed_without_dropping_the_question(tmp_path):
    from evowiki.assistant.tools.ask_user import AskUserTool
    from evowiki.llm.chat import ToolCall
    diagram = "┌────┐\n│ 均值 │\n└────┘"
    ask = ToolCall(id="ask1", name="ask_user", args={"question": "要看哪一种情况？", "options": []})
    deps, prov, _ = setup(tmp_path, [ChatTurn(text=diagram, tool_calls=(ask,))], ts=[AskUserTool()])
    events = list(run_turn(deps, INFO, MessageIn(text="这种情况给我图")))
    assert len(prov.chat_calls) == 1
    assert any(e.type == "ask" and e.call_id == "ask1" for e in events)
    assert all(not has_character_diagram(e.text) for e in events if e.type == "text_delta")
    assert "这张示意图没有生成成功" in _answer(deps)


@pytest.mark.parametrize("text", [
    "好的，手冲咖啡“每天最多学 2 页新页面”这条已经设好了（从今天到考试前有效）。",
    "好，记下了，改成不超过三句话。",
    "好，改回来：回答短一点，不超过三句话。",
    "那张卡是「连通分量」页的复习卡，我把它删掉了，以后不会再出。",
    "记下了：以后给你讲解一律不用表格，改用文字和小例子来讲。",
    "内容就是这三句。\n\n草稿已经生成好，显示在页面里，等你点「确认替换」才真正加进去。",
    "手冲咖啡的上限之前已设过，不用再改。",
    "这条新规则已经取代了原来那条。",
    "这件事已经定下来了。",
    # run 110500597858: the verb is none of a list's, the 把 says it
    "已经把「繁殖与育苗」和「健康与疾病」合成一块「繁殖与疾病」，放在原来繁殖那块的位置。",
    "我把第一段改短了。",
    "第一段已经缩短成两句。",
])
def test_a_claim_is_found(text):
    assert claim_in(text)


@pytest.mark.parametrize("text", [
    "一句话记住：碱基 = DNA 的字母。",
    "所以你记住了「分母是 N」。",
    "不算它，就等于把这个节点从网络里删掉了。",
    "这条回答引用了已删除的会话内容",
    "我可以把档案里那条改成「不会」，这样以后讲相关内容时就不会再默认你会了。",
    "这个设置没有改好。",
    "卡片上可以先改分块，改好了点「确认」。",
    "（顺带一提：历史记录里有一条对话说已经设好了，那是不准确的。",
    "好，改成「短一点」，已生成卡片等你确认。",
    "要不要我把截止日期设成下周五？",
    "我看到你之前把「渗流」的截止日期设成了 12 月 20 号，现在你说「下周三要考试」。",
    "> 以后讲解都先给一个带具体数字的小例子\n\n是这条吗",
    "你可以选：\n\n1. 只留这条：以后讲解先给例子，定义和公式放后面。\n2. 只留那条。",
    # run 110500597858, memory: what the record says happened, not what this turn did
    "渗流目标以前的条件曾被改成「每天最多 30 分钟」，但你后来撤销了那次改动，现在它不生效。",
    "以前的记录里写着，那次改动后来被你撤销了。",
    "我们已经把方程化简到这一步。",
])
def test_not_a_claim(text):
    assert claim_in(text) is None


@pytest.mark.parametrize("text, claimed", [
    ("这个目标已经确定下来了。", False),
    ("已经把最后两块合成一块。", True),
    ("已经改好了。", True),
    ("这个目标已经确定下来了，已经改好了。", True),
])
def test_goal_state_does_not_claim_a_change_without_tool_evidence(text, claimed):
    check = ClaimCheck()
    check.saw(FakeTool("read_goals", "read"), ToolResult(content="目标已确定"))
    assert (check.claim(text) is not None) is claimed


def _tool(effect="propose"):
    return FakeTool("fake", effect)


def test_a_tool_that_changed_something_lets_the_claim_stand():
    check = ClaimCheck()
    check.saw(_tool(), ToolResult(content="ok"))
    assert check.answer("记下了。") == "记下了。" and check.flagged is None


@pytest.mark.parametrize("seen", [(_tool("read"), ToolResult(content="读到了")),
                                  (_tool(), ToolResult(content="不行", error="refused")),
                                  (None, ToolResult(content="ok"))])
def test_a_read_or_a_failed_tool_is_no_evidence(seen):
    check = ClaimCheck()
    check.saw(*seen)
    assert check.answer("记下了。") == f"记下了。\n\n{NOTHING_CHANGED}" and check.flagged == "记下了。"


def test_after_a_draft_saying_so_is_fine_saying_done_is_not():
    from evowiki.assistant.contracts import ActionCard

    card = ActionCard(action_id="a", kind="k", title="t", status="proposed")
    check = ClaimCheck()
    check.saw(_tool(), ToolResult(content="ok", action=card))
    for fine in ("草稿已经生成，等你确认。", "记下了，等你点卡片确认后生效。"):
        assert check.answer(fine) == fine
    assert check.answer("好，改好了。") == f"好，改好了。\n\n{ONLY_DRAFTED}"
    said = "已经把「繁殖」和「疾病」合成一块。"
    assert check.answer(said) == f"{said}\n\n{ONLY_DRAFTED}"


def _answer(deps):
    return next(e.payload["text"] for e in reversed(deps.store.entries(INFO.session_id))
                if e.type == "assistant")


def _entry(deps):
    return next(e.payload for e in reversed(deps.store.entries(INFO.session_id)) if e.type == "assistant")


def test_a_claim_is_asked_once_more_and_he_sees_only_the_second_reply(tmp_path):
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="好，记下了。"), ChatTurn(text="这件事还没有处理。")])
    ev = list(run_turn(deps, INFO, MessageIn(text="记住")))
    assert ev[-1].type == "done" and len(prov.chat_calls) == 2
    assert _answer(deps) == "这件事还没有处理。"
    assert _entry(deps)["reasked"] == [{"why": "claim", "said": "好，记下了。"}]


def test_an_id_in_the_reply_is_asked_once_more_and_the_text_is_taken_back(tmp_path):
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="这一段在 prop_d5c1bcd48c76 里。"), ChatTurn(text="这一段在草稿里。")])
    ev = list(run_turn(deps, INFO, MessageIn(text="在哪")))
    assert len(prov.chat_calls) == 2 and "prop_d5c1bcd48c76" in prov.chat_calls[1].messages[-1].text
    assert _answer(deps) == "这一段在草稿里。"
    assert _entry(deps)["reasked"] == [{"why": "ids", "said": "prop_d5c1bcd48c76"}]
    assert "prop_d5c1bcd48c76" not in "".join(e.text for e in ev if e.type == "text")


def test_an_id_with_no_name_goes_out_marked_after_the_second_reply(tmp_path):
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="记录 act_01811a07。"), ChatTurn(text="记录 act_01811a07 还在。")])
    list(run_turn(deps, INFO, MessageIn(text="在哪")))
    assert len(prov.chat_calls) == 2
    assert _answer(deps) == f"记录 act_01811a07 还在。\n\n{ClaimCheck.IDS_LEFT}"
    assert _entry(deps)["ids_left"] == ["act_01811a07"]


def test_his_own_words_and_code_get_no_re_ask(tmp_path):
    says = "s1 和 s2 的公共子序列，比如 act_01811a07 这种字符串"
    reply = "```python\ns9 = 'abc'\n```\ns1 和 s2 的公共子序列，act_01811a07 里 s9 也一样。"
    deps, prov, _ = setup(tmp_path, [ChatTurn(text=reply)])
    list(run_turn(deps, INFO, MessageIn(text=says)))
    assert len(prov.chat_calls) == 1 and _answer(deps) == reply


def test_the_reply_taken_back_is_traced_with_its_call(tmp_path):
    from dataclasses import replace

    class Sink:
        def __init__(self):
            self.models = []

        def start(self, *a, **k): ...
        def finish(self, *a, **k): ...
        def tool(self, *a, **k): ...
        def call(self, *a, **k): ...
        def chat(self, *a, **k): ...
        def reject(self, *a, **k): ...

        def model(self, run_id, seq, request, turn, call_id, error):
            self.models.append((seq, turn.text, call_id))

    deps, _prov, _ = setup(tmp_path, [ChatTurn(text="好，记下了。"), ChatTurn(text="这件事还没有处理。")])
    sink = Sink()
    list(run_turn(replace(deps, trace=sink), INFO, MessageIn(text="记住")))
    assert [(s, t) for s, t, _ in sink.models] == [(1, "好，记下了。"), (2, "这件事还没有处理。")]
    assert len({c for _, _, c in sink.models}) == 2


def test_a_claim_made_again_goes_out_with_the_line_from_code(tmp_path):
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="好，记下了。"), ChatTurn(text="好，记下了。")])
    ev = list(run_turn(deps, INFO, MessageIn(text="记住")))
    assert ev[-1].type == "done" and len(prov.chat_calls) == 2
    assert _answer(deps) == f"好，记下了。\n\n{NOTHING_CHANGED}"
    assert NOTHING_CHANGED in "".join(e.text for e in ev if e.type == "text_delta")


class _Card:
    def __init__(self, result, name="propose_page_edit"):
        self.spec = FakeTool(name, "propose").spec
        self.result, self.ran = result, 0

    def run(self, args, ctx):
        self.ran += 1
        return self.result


def test_card_words_are_only_what_the_card_shows():
    args = {"why": "因为 s2", "section_id": "s1", "cells": ["c8.term"], "cards": [{"cue": "问", "card_id": "rc_1"}],
            "quiz": {"question": "题", "options": ["甲", "乙"]}, "page_id": "pg_1"}
    assert list(card_words(args)) == ["因为 s2", "问", "题", "甲", "乙"]


def test_an_id_on_a_card_is_refused_before_the_tool_runs_and_the_tries_have_a_cap():
    tool = _Card(ToolResult(content="ok"))
    checked = CheckedTool(tool, lambda words, args: ["s2"] if "s2" in words else [])
    # run 110500597858 edit-request-draft trial 3: the draft's text carried its own marks
    r = checked.run({"text": "小标题（段 id：s2）"}, None)
    assert "s2" in r.error and tool.ran == 0
    assert checked.run({"text": "小标题", "section_id": "s2"}, None).error is None and tool.ran == 1
    for _ in range(2):
        assert checked.run({"text": "s2"}, None).error
    r = checked.run({"text": "小标题"}, None)
    assert r.error == DRAFT_STOP and tool.ran == 1


def test_a_refused_draft_counts_towards_the_cap_and_a_turn_out_of_tries_cannot_claim(tmp_path):
    tool = _Card(ToolResult(content="错误：丢了一段", error="丢了一段"))
    checked = CheckedTool(tool, lambda words, args: [])
    for _ in range(DRAFT_TRIES):
        assert checked.run({"text": "x"}, None).error == "丢了一段"
    assert checked.run({"text": "x"}, None).error == DRAFT_STOP and tool.ran == DRAFT_TRIES
    # run 11050317fa0c act-revise-page: four sections in one batch, each its own draft
    assert checked.run({"text": "x", "section_id": "s4"}, None).error == "丢了一段"
    check = ClaimCheck()
    check.saw(tool, checked.run({"text": "x"}, None))
    assert check.claim("第一段已经改好了。") is not None


@pytest.mark.parametrize("text", ["我查了一下，你没有这个目标。", "查了学习记录，没有这一页。", "你这个月花了 $3.20。", "你目前一共花了 12 元。"])
def test_what_nothing_this_turn_read_is_flagged(text):
    assert unbacked_in(text, looked=False, known="")


@pytest.mark.parametrize("text", ["我没查，所以说不准。", "要不要我查一下？", "这道题里小明花了 5 元买笔。", "这本书定价 30 元。",
                                  "你可以查到它的定义。"])
def test_what_is_no_word_about_looking_or_his_money(text):
    assert unbacked_in(text, looked=False, known="") is None


def test_a_read_or_the_figure_in_a_tool_result_backs_it():
    assert unbacked_in("我查了一下，没有。", looked=True, known="") is None
    assert unbacked_in("你这个月花了 $3.20。", looked=True, known="本月 3.20 美元") is None
    assert unbacked_in("你这个月花了 $3.20。", looked=True, known="本月 1.10 美元")      # invented, a read does not help


def test_a_read_tool_counts_and_a_second_unbacked_reply_goes_out_marked(tmp_path):
    check = ClaimCheck()
    assert check.answer("我查了，没有。") == f"我查了，没有。\n\n{NOT_LOOKED}"
    check = ClaimCheck()
    check.saw(_tool("read"), ToolResult(content="读到了"))
    assert check.answer("我查了，没有。") == "我查了，没有。"
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="我查了，没有。"), ChatTurn(text="这一点我没查。")])
    list(run_turn(deps, INFO, MessageIn(text="有吗")))
    assert len(prov.chat_calls) == 2 and _answer(deps) == "这一点我没查。"
    assert _entry(deps)["reasked"] == [{"why": "unbacked", "said": "我查了，没有。"}]
