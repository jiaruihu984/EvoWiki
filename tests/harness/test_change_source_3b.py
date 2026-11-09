"""change_source.summary lines for every key kind (harness phase 3b). Pure: no store."""

from evowiki.harness.change_source import summary
from evowiki.harness.diff import KeyOp, PackDiff, diff_to_json

STEP = {"at": "deep.before_write", "kind": "delegate", "tier": "fast", "tools": ["read_page"],
        "instruction": "先读相关页面写摘要", "output_to": "context.notes", "max_tool_calls": 3}
EXAMPLE = {"name": "拖拽排序", "use": "排列步骤时", "block": {"kind": "order"}}


def s(*ops, before=None, costs=(None, None), titles=None):
    return summary(diff_to_json(PackDiff(tuple(ops)), before or {}), *costs, titles=titles)


def test_h3_builtin_toggle():
    assert s(KeyOp("H3.deep.read_page", False)) == "写页面时不再查看已有的页面"
    assert s(KeyOp("H3.deep.read_page", True), before={"H3.deep.read_page": False}) == \
        "写页面时会查看已有的页面"


def test_h10_tier_carries_the_cost_note():
    got = s(KeyOp("H10.deep.write.tier", "good"), costs=(0.010, 0.006))
    assert got == "写页面用哪一档模型改为 质量优先（原来 默认值，重放一节平均约少花 $0.004）", got
    got = s(KeyOp("H10.assistant.turn.tier", "fast"), before={"H10.assistant.turn.tier": "good"})
    assert got == "助手回答用哪一档模型改为 速度优先（原来 质量优先）", got
    got = s(KeyOp("H10.deep.write.tier", None), before={"H10.deep.write.tier": "deep"})
    assert got == "写页面用哪一档模型改为 默认值（原来 最强）", got
    think = s(KeyOp("H10.deep.write.thinking", False), costs=(0.010, 0.010))
    assert "改为 关" in think and "花费差不多" in think, think


def test_h9_h11_steps():
    assert s(KeyOp("H9.deep.before_write", [STEP])) == "改了写页面：动笔前先做的几步（1 步）"
    assert s(KeyOp("H11.deep.after_write", None)) == "去掉了写页面：写完后自查的几步"


def test_h8_lines():
    assert s(KeyOp("H8.deep.evoui_examples", [EXAMPLE])) == "加了 1 个交互块示例"
    assert s(KeyOp("H8.deep.evoui_examples", [EXAMPLE]),
             before={"H8.deep.evoui_examples": [EXAMPLE, EXAMPLE]}) == "交互块示例改为 1 个"
    assert s(KeyOp("H8.deep.spec.write", ["items"])) == \
        "写页面时预先读教学规范的哪几部分改为 讲解规则条目"
    assert s(KeyOp("H8.deep.spec.plan", ["items", "exemplar"])) == \
        "写页面规划时读教学规范的哪几部分改为 讲解规则条目、范例页"
    assert s(KeyOp("H8.deep.spec.write", [])) == "写页面时预先读教学规范的哪几部分改为 不读"
    presets = KeyOp("H8.component.quiz-card.presets", [{"name": "a"}, {"name": "b"}])
    assert s(presets) == "交互组件「一个交互组件」的预设改为 2 个"         # no name known
    assert s(presets, titles={"component:quiz-card": "选择题卡片"}) == \
        "交互组件「选择题卡片」的预设改为 2 个"
    note = KeyOp("H8.component.quiz-card.note", "讲完定义后用")
    assert s(note, titles={"component:quiz-card": "选择题卡片"}) == "改了交互组件「选择题卡片」什么时候用"


def test_h12_lines():
    assert s(KeyOp("H12.tool.deep.check_balanced", {"candidate_id": "fc_a"})) == \
        "新增了工具「一个自己做的工具」"          # no title known: never the identifier
    assert s(KeyOp("H12.tool.deep.check_balanced", {"candidate_id": "fc_a"}),
             titles={"fc_a": "判断方程式是否配平"}) == "新增了工具「判断方程式是否配平」"
    assert s(KeyOp("H12.tool.deep.check_balanced", None),
             before={"H12.tool.deep.check_balanced": {"candidate_id": "fc_a"}},
             titles={"fc_a": "判断方程式是否配平"}) == "去掉了工具「判断方程式是否配平」"
    assert s(KeyOp("H12.patch.fc_x", {"candidate_id": "fc_x", "commit": "abcdef1"})) == "修改了代码"
    assert s(KeyOp("H12.patch.fc_x", {"candidate_id": "fc_x", "commit": "abcdef1"}),
             titles={"fc_x": "页面读取加缓存"}) == "修改了代码：页面读取加缓存"


def test_a_forged_opt_in_is_folded_into_its_tool_line():
    got = s(KeyOp("H3.deep.check_balanced", True),
            KeyOp("H12.tool.deep.check_balanced", {"candidate_id": "fc_a"}))
    assert got == "新增了工具「一个自己做的工具」"
    alone = s(KeyOp("H3.deep.check_balanced", False))     # no H12 op: its own line
    assert alone == "写页面时不再用一个自己做的工具"


def test_h4_still_carries_the_cost_note_and_others_do_not():
    got = s(KeyOp("H4.deep.max_tool_calls", 70), KeyOp("H3.deep.read_page", False),
            costs=(0.01, 0.02))
    assert got == ("写页面：每轮最多调用几次工具改为 70（原来 默认值，重放一节平均约多花 $0.010）；"
                   "写页面时不再查看已有的页面")
