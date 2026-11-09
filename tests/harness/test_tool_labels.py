"""Harness rows in plain Chinese: no internal tool name reaches the change list (task 11b), and
no internal identifier in any row kind phase 3b renders: H3, H8, H9, H10, H11, H12 (final fix I2)."""

import re

from evowiki.harness import tool_labels
from evowiki.harness.change_source import before_note, summary
from evowiki.harness.diff import KeyOp, PackDiff, diff_to_json
from evowiki.harness.keys import TOGGLE_TOOLS
from evowiki.harness.round import _lines

IDENT = re.compile(r"(?<![A-Za-z])[a-z_]{3,}")   # a word like Wiki is not an identifier


def test_every_toggleable_tool_has_both_labels():
    for agent, tools in TOGGLE_TOOLS.items():
        for t in tools:
            off, on = tool_labels.off_line(agent, t), tool_labels.on_line(agent, t)
            assert off and on and off != on, (agent, t)
            assert not IDENT.search(off + on), (agent, t, off, on)
    assert tool_labels.off_line("deep", "read_spec") == "写页面时不再查看讲解规则和范例页"
    assert tool_labels.on_line("deep", "read_spec") == "写页面时会查看讲解规则和范例页"


def _ops():
    ops = [(f"H3.{a}.{t}", v) for a, ts in TOGGLE_TOOLS.items() for t in ts for v in (False, True)]
    return [*ops, ("H3.deep.check_balanced", True), ("H3.assistant.check_balanced", False)]


def test_no_tool_identifier_in_summary_or_asked_lines():
    for name, v in _ops():
        diff = PackDiff((KeyOp(name, v),))
        before = {name: not v}
        text = summary(diff_to_json(diff, before), None, None)
        lines = " ".join(str(ln) for ln in _lines(diff, before)).replace("ctx", "")
        assert not IDENT.search(text), (name, text)
        for ln in _lines(diff, before)[1:]:
            assert not IDENT.search(ln["text"]), (name, ln)
        assert lines


def test_h12_tool_rows_never_show_the_identifier():
    for v in ({"candidate_id": "fc_a"}, None):
        op = KeyOp("H12.tool.deep.check_balanced", v)
        before = {op.name: {"candidate_id": "fc_a"}}
        for titles in (None, {"fc_a": "判断方程式是否配平"}):
            text = summary(diff_to_json(PackDiff((op,)), before), None, None, titles)
            assert not IDENT.search(text), text
            assert not IDENT.search(_lines(PackDiff((op,)), before, titles=titles)[1]["text"])


def test_before_state_goes_to_the_tooltip():
    d = diff_to_json(PackDiff((KeyOp("H3.deep.read_spec", False),)), {"H3.deep.read_spec": True})
    assert before_note(d) == "改之前：写页面时会查看讲解规则和范例页"
    d = diff_to_json(PackDiff((KeyOp("H3.deep.read_spec", True),)), {"H3.deep.read_spec": False})
    assert before_note(d) == "改之前：写页面时不再查看讲解规则和范例页"
    assert before_note(diff_to_json(PackDiff((KeyOp("H10.deep.write.tier", "good"),)), {})) == ""


STEP = {"at": "deep.before_write", "kind": "delegate", "tier": "fast", "tools": ["read_page"],
        "instruction": "先读相关页面写摘要", "output_to": "context.notes", "max_tool_calls": 3}
EXAMPLE = {"name": "拖拽排序", "use": "排列步骤时", "block": {"kind": "order"}}
NAMES = {"fc_a": "判断方程式是否配平", "fc_p": "交互块渲染前规范化",
         "component:quiz-card": "选择题卡片"}


def _every_kind():
    """(op, before) pairs of every row kind 3b renders, each value set / changed / back to default."""
    tool = {"candidate_id": "fc_a"}
    out = [(KeyOp("H3.deep.read_spec", False), {}), (KeyOp("H3.deep.read_spec", None), {}),
           (KeyOp("H3.deep.check_balanced", True), {}),
           (KeyOp("H12.tool.deep.check_balanced", tool), {}),
           (KeyOp("H12.tool.deep.check_balanced", None), {"H12.tool.deep.check_balanced": tool}),
           (KeyOp("H12.patch.fc_p", {"candidate_id": "fc_p", "commit": "abcdef1"}), {}),
           (KeyOp("H8.deep.evoui_examples", [EXAMPLE]), {}),
           (KeyOp("H8.component.quiz-card.presets", [{"name": "a"}]), {}),
           (KeyOp("H8.component.quiz-card.note", "讲完定义后用"), {}),
           (KeyOp("H8.component.quiz-card.note", None), {"H8.component.quiz-card.note": "旧"}),
           (KeyOp("H9.deep.before_write", [STEP]), {}),
           (KeyOp("H9.assistant.before_answer", None), {"H9.assistant.before_answer": [STEP]}),
           (KeyOp("H11.deep.after_write", [STEP]), {})]
    for part in (["items"], ["exemplar"], ["items", "exemplar"], [], None):
        out += [(KeyOp("H8.deep.spec.plan", part), {}), (KeyOp("H8.deep.spec.write", part), {})]
    for name in ("H10.deep.plan.tier", "H10.deep.write.tier", "H10.assistant.turn.tier",
                 "H10.bot.plan.tier", "H10.forge.code.tier"):
        for v, b in (("good", None), ("fast", "good"), ("deep", "fast"), (None, "deep")):
            out.append((KeyOp(name, v), {name: b}))
            think = name.replace(".tier", ".thinking")
            out.append((KeyOp(think, v is not None), {think: None}))
    return out


def test_no_identifier_in_any_3b_row_kind():
    for op, before in _every_kind():
        diff = PackDiff((op,))
        for titles in (None, NAMES):
            for costs in ((None, None), (0.01, 0.02)):
                text = summary(diff_to_json(diff, before), *costs, titles)
                assert not IDENT.search(text), (op, text)
                for ln in _lines(diff, before, costs, titles)[1:]:
                    assert not IDENT.search(ln["text"]), (op, ln)
