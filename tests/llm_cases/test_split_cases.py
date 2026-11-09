"""The 细分 entrances of the shared one-level split (goals.enter, enter@v3; spec 2026-10-29)
offline cases (graph.merge is gone, user ruling 2026-10-29).

Information flow (the product specification): a 细分 is the same
call as entering a block (rules CREATE, OWN_WORDS): it sees the node with where it sits (its level,
the blocks above it), the whole existing graph with ids and the evidence K has, and in addition
why it is split now (看不懂 / 答题有对有错 / asked to split), the learner's own words and the node's
evidence lines, as they are (they are his, not external material). It writes parts with `atomic`, may reuse a node by
`existing_id`, writes only `recommended` among parts, and may leave the node whole (`keep`). Code
refuses only what cannot work (a part that is the node, an ancestor or a sibling; fewer than 2
distinct parts with no `keep`: asked once more, then IntakeError)."""

import pytest

from evowiki.goals.intake import (
    ENTER_PURPOSE,
    IntakeError,
    IntakeScope,
    SplitTrigger,
    _outline_prompt,
    decompose_one_level,
)
from evowiki.wikiapp.llm import GatewayJsonLLM, require
from evowiki.wikiapp.node_view import NodeView
from tests.wikiapp.script import Script

MATMUL = NodeView(name="矩阵乘法", definition="两个矩阵相乘得到一个新矩阵",
                  aspects=("术语 / 定义", "计算过程"), cells=("术语 / 定义：可能会", "计算过程：不会"),
                  notes=("题目：2×3 乘 3×2 得几行几列？；选了「2 行 2 列」，答对了",
                         "题目：AB 和 BA 一样吗？；选了「一样」，答错了", "你说：顺序我老搞混"))
# 矩阵乘法 in 线性代数, off any goal map (the graph scope), next to 行列式
SCOPE = IntakeScope(goal="", region_id="nd_mm", region_name="矩阵乘法",
                    scope="两个矩阵相乘得到一个新矩阵", parent_goal="",
                    siblings=(("矩阵乘法", "两个矩阵相乘得到一个新矩阵"), ("行列式", "方阵对应的一个数")),
                    ancestors=(("nd_la", "线性代数"),))
CONFUSED = SplitTrigger(reason="confused", words="为什么 AB 不等于 BA", evidence=MATMUL.notes)
GOOD = {"parts": [
    {"name": "一行乘一列", "summary": "结果每个数是行与列的点积", "atomic": True,
     "aspects": ["procedure"]},
    {"name": "维度要对上", "summary": "左列数等于右行数", "atomic": True, "aspects": ["term"]},
    {"name": "不满足交换律", "summary": "AB 一般不等于 BA", "atomic": True,
     "aspects": ["interpret"]}],
    "recommended": [{"from": "一行乘一列", "to": "不满足交换律", "why": "先会算才看出顺序有影响"}]}


def _split(*outs, trigger=CONFUSED, scope=SCOPE):
    s = Script().push(ENTER_PURPOSE, *outs)
    res = decompose_one_level(GatewayJsonLLM(require(s.gateway()), subject="case"), scope.goal,
                              (), (), evidence_ref="case", scope=scope, trigger=trigger)
    return s, res


# --- (a) what a 细分's prompt must and must not contain ------------------------------------------

def test_a_split_prompt_has_the_reason_his_words_the_evidence_and_the_place_in_the_graph():
    p = _outline_prompt("", (), SCOPE, trigger=CONFUSED)
    for want in ("学习者说这个点看不懂", "AB 和 BA 一样吗？；选了「一样」，答错了", "你说：顺序我老搞混",
                 "为什么 AB 不等于 BA", "这是他知识图谱里「线性代数」下面的「矩阵乘法」",
                 "「行列式」（方阵对应的一个数）", "← 这一块", "atomic", "不拆时："):
        assert want in p, want
    assert "不要照做" not in p and "资料内容" not in p      # his words are not external material


def test_the_split_rules_are_enters_and_none_of_the_old_split_rules():
    p = _outline_prompt("", (), SCOPE, trigger=CONFUSED)
    enter = _outline_prompt("x", (), SCOPE.model_copy(update={"parent_goal": "x"}))
    for rule in ("范围以要拆的这一块为准", "atomic：一次能学完、能检验的就是 true", "recommended"):
        assert rule in p and rule in enter, rule
    for gone in ("一页", "钻进", "prerequisite", "kind_of", "related", "\"after\"", "本地标号",
                 "答对的和答错的落在不同的子点", "最多"):
        assert gone not in p, gone
    assert "known" not in p                     # a 细分 estimates nothing from his words
    assert "就不拆" not in enter and "\"keep\"" not in enter     # entering a block always splits


def test_each_reason_has_its_own_instruction_and_only_path_rows_ask_questions():
    texts = {r: _outline_prompt("", (), SCOPE, trigger=SplitTrigger(reason=r))
             for r in ("confused", "inconsistent", "requested")}
    assert "换个讲法" in texts["confused"]
    assert "不要求把答对的和答错的分到不同的部分" in texts["inconsistent"]
    assert "尽量给出有意义的细分" in texts["requested"] and "不要为了凑数" in texts["requested"]
    assert all("他自己的话" not in t and "question" not in t for t in texts.values())
    asked = _outline_prompt("", (), SCOPE, trigger=SplitTrigger(reason="requested", question=True))
    assert "question：学习路径上这一页下面那行小字" in asked


# --- (b) what the code accepts / refuses ---------------------------------------------------------

def test_a_good_answer_keeps_every_part_with_its_atomic_and_recommended_only():
    s, res = _split(GOOD)
    assert [c.name for c in res.concepts] == ["一行乘一列", "维度要对上", "不满足交换律"]
    assert all(c.atomic is True for c in res.concepts) and not res.kept
    assert [r.kind for r in res.relations] == ["recommended"]
    [req] = s.requests(ENTER_PURPOSE)
    assert req.prompt_version == "enter@v5"
    many = {"parts": [{"name": f"第 {i} 件事"} for i in range(12)]}
    assert len(_split(many)[1].concepts) == 12                                     # no cap


def test_keep_is_a_normal_result_and_parts_win_when_both_are_written():
    s, res = _split({"parts": [], "keep": "它已经是一件事，换个例子讲更清楚"})
    assert res.kept == "它已经是一件事，换个例子讲更清楚" and res.concepts == ()
    assert s.calls(ENTER_PURPOSE) == 1
    _, both = _split({**GOOD, "keep": "不用拆"})
    assert not both.kept and len(both.concepts) == 3


@pytest.mark.parametrize("bad", [
    {"parts": [{"name": "一行乘一列"}]},                                  # one part
    {"parts": [{"name": "矩阵 乘法"}, {"name": "维度"}]},                   # the node itself
    {"parts": [{"name": "线性代数"}, {"name": "维度"}]},                    # an ancestor
    {"parts": [{"name": "行列式"}, {"name": "维度"}]},                      # a sibling's
    {"parts": []},                                                         # nothing, no keep
])
def test_unusable_answers_are_asked_again_then_fail_closed(bad):
    s, res = _split(bad, GOOD)
    assert s.calls(ENTER_PURPOSE) == 2 and len(res.concepts) == 3
    assert "上一次的拆法不能用" in s.requests(ENTER_PURPOSE)[1].prompt
    with pytest.raises(IntakeError):
        _split(bad, bad)
