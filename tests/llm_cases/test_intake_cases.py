"""Offline cases for the goal chain after S1 (spec 2026-10-28-llm-call-consolidation): goals.intake
— S1, one call per goal (goals/blocks.py, intake@v31: the blocks and the goal record), and the
a block being entered — goals.enter (S2, goals/intake.py, enter@v1): one call that splits it one
layer and, with his own words, estimates what he knows. The refine, split and gap calls are gone.

Information flow (the product specification): S1 and the entered
outline follow CREATE — they see the whole graph with ids (every node with its parents, parts,
prerequisites with phrases and the evidence K has) and reuse an existing node by `existing_id`.
The estimate follows OWN_WORDS — it reads his own words (about the block, for the goal), fenced,
and only a verbatim quote becomes an observation. Harness per call: valid, unusable and edge outputs."""

import re
from pathlib import Path

import pytest

from evowiki.goals.blocks import blocks_prompt, plan_blocks
from evowiki.goals.contracts import Aspect
from evowiki.goals.intake import (
    ConceptRef,
    IntakeError,
    IntakeScope,
    SourceToc,
    TocSection,
    TopicRef,
    _outline_prompt,
    decompose_one_level,
)

A = Aspect
OUTLINE, ENTER = "goals.intake", "goals.enter"
GOAL = "我要学机器学习"
BACKGROUND = "会写 Python，大学学过线性代数和一点概率，都忘得差不多了。"
EXISTING = (ConceptRef(id="c1", name="矩阵", aspects=(A.TERM,)),
            ConceptRef(id="c2", name="矩阵乘法", aspects=(A.PROCEDURE,), prereqs=("c1",)))
TOPICS = (TopicRef(id="hub-la", name="线性代数", members=("c1", "c2")),)
SVD_BACKGROUND = "大学学过线性代数，矩阵乘法还会算，特征值和特征向量忘得差不多了。"
CAPS = re.compile(r"最多\s*\d|不超过\s*\d|至少\s*\d|\d+\s*个|exceed|max\s*\d+|\d+\s*words")
ROOT = Path(__file__).resolve().parents[2]


class FakeLLM:
    """One reply per purpose (an Exception is raised; a callable answers from the prompt).
    Records (purpose, prompt) in call order."""

    def __init__(self, replies):
        self.replies = replies
        self.calls: list[tuple[str, str]] = []

    def __call__(self, role, purpose, prompt, schema):
        assert role == "generator" and purpose in (OUTLINE, ENTER)
        self.calls.append((purpose, prompt))
        out = self.replies[purpose]
        if callable(out):
            out = out(prompt)
        if isinstance(out, Exception):
            raise out
        return schema.model_validate(out)

    def prompt(self, purpose):
        return next(p for q, p in self.calls if q == purpose)

    def purposes(self):
        return [q for q, _ in self.calls]


# User ruling (intake@v15+): no prompt of the chain touches a test goal (machine learning, deep
# learning, music theory, SVD, correlation, z-scores, linear algebra, math). The one worked example
# is Metacademy's 哥德尔不完备定理, which shares no concept with the ML / NN / SVD gold sets.
TEST_GOAL_WORDS = ("机器学习", "深度学习", "乐理", "奇异值", "相关系数", "z-score", "z 分数", "线性代数",
                   "矩阵", "梯度", "概率", "导数", "回归", "神经网络", "Python", "数学", "特征值",
                   "统计", "均值", "方差", "梯度下降", "人像", "照片", "西班牙", "逆矩阵", "求逆",
                   "数据库")
NEUTRAL_GOAL = "我要学某样东西"
NEUTRAL_TOC = SourceToc(source_id="s1", title="某份资料", sections=(
    TocSection(id="x1", label="§1", title="1 第一节"),))
BLOCK_SCOPE = IntakeScope(goal="某一块", region_id="c9", region_name="某一块", scope="甲、乙",
                          parent_goal=NEUTRAL_GOAL, siblings=(("某一块", "甲、乙"), ("另一块", "丙")))


def _all_prompts():
    """Every prompt of the chain for a neutral goal (S1, the entered block's split, with and
    without his words)."""
    return {"blocks": blocks_prompt(NEUTRAL_GOAL, (), ()),
            "blocks_known": blocks_prompt(NEUTRAL_GOAL, (), (ConceptRef(id="c1", name="某个点"),),
                                          topics=(TopicRef(id="hub-x", name="某个大点",
                                                           members=("c1",)),)),
            "blocks_source": blocks_prompt(NEUTRAL_GOAL, (), (), sources=(NEUTRAL_TOC,),
                                           reply=False),
            "blocks_understand": blocks_prompt(NEUTRAL_GOAL, (), (), sources=(NEUTRAL_TOC,),
                                               use="understand", reply=False),
            "outline_block": _outline_prompt(NEUTRAL_GOAL, (), BLOCK_SCOPE),
            "outline_block_known": _outline_prompt(NEUTRAL_GOAL,
                                                   (ConceptRef(id="c1", name="某个点"),),
                                                   BLOCK_SCOPE),
            "outline_block_words": _outline_prompt(NEUTRAL_GOAL, (), BLOCK_SCOPE, words=("x",))}


@pytest.mark.parametrize("call", list(_all_prompts()))
def test_no_prompt_of_the_chain_touches_a_test_goal(call):
    p = _all_prompts()[call]
    for s in TEST_GOAL_WORDS:
        assert s not in p, (call, s)


@pytest.mark.parametrize("call", list(_all_prompts()))
def test_no_prompt_keeps_the_cooking_or_photography_fragments_or_caps(call):
    p = _all_prompts()[call]
    for s in ("做菜", "刀工", "切丝", "摄影", "相机", "拍照", "构图", "白平衡", "三分法", "光圈"):
        assert s not in p, (call, s)
    assert not CAPS.search(p), (call, p)


@pytest.mark.parametrize("call", list(_all_prompts()))
def test_no_prompt_has_a_learner_description_line(call):
    """S1: the learner's profile reaches the model only through the gateway; no prompt carries a
    description of him (the estimate reads his own words, fenced, as its quotes' source)."""
    p = _all_prompts()[call]
    assert "细化后的目标" not in p and "学习者对自己的描述" not in p, call


@pytest.mark.parametrize("call", ["outline_block", "blocks"])
def test_the_tools_rule_is_in_the_decompose_guidance(call):
    p = _all_prompts()[call]
    if call == "blocks":            # intake@v32: the user's own wording of the tools rule
        assert ("理论知识不依赖特定软件。涉及动手操作时，可以包含必要的工具能力，但不把某一种工具"
                "当成唯一选择，除非目标明确指定。") in p
    else:
        assert ("理论知识不依赖特定软件工具；目标明确包含动手操作时，可以列出完成这些操作所需的工具能力，"
                "但不把某一种特定工具当成唯一选择，除非目标明确指定。") in p, call
        assert "部署等目标没提到的进一步工程能力不列" in p, call
    for s in ("needs_parts", '"nodes"', '"relations"', "expansions", "Skill Mapper"):
        assert s not in p, (call, s)


def test_the_entered_outline_has_optional_aspects():
    p = _all_prompts()["outline_block"]
    assert "可以写 aspects" in p and "不写也可以" in p


@pytest.mark.parametrize("call", ["outline_block", "outline_block_words"])
def test_procedure_is_steps_or_calculation(call):
    p = _all_prompts()[call]
    assert "procedure=操作步骤/计算过程" in p and "procedure=计算过程" not in p


def test_the_entered_outline_has_no_observations_and_no_teaching_standard():
    p = _outline_prompt(GOAL, EXISTING, BLOCK_SCOPE, topics=TOPICS)
    for s in ('"observations"', '"skill_gaps"', "polarity", "答对", "答错", "【标准",
              "planning_questions", "当桥"):
        assert s not in p, s


@pytest.mark.parametrize("call", ["outline_block", "outline_block_known"])
def test_the_entered_outline_has_the_recommended_rule_with_the_logic_counter_example(call):
    """Relation layers (user ruling 2026-10-14): specific ends only, never a whole field; the
    counter-example (conceptual understanding needs no computational knowledge) is in the worked
    example's domain, not a test goal's (TEST_GOAL_WORDS is checked on every prompt)."""
    p = _all_prompts()[call]
    for s in ("可以写 recommended", "两端都必须是具体的知识点：不能是一整个领域，也不能是还包含部分的块",
              "这只是「建议先看」，不是「不会它就学不了」",
              "概念性的理解不一定依赖计算性的知识",
              "不要写「数理逻辑 recommended 可判定性的概念」",
              "理解「什么是可判定性」不需要先会图灵机的形式定义",
              "要写也写在具体的点上，例如「图灵机 recommended 停机问题的证明」"):
        assert s in p, (call, s)
    assert "requires" not in p and "prerequisite" not in p


def test_the_entered_outline_judges_atomic_not_page_size():
    """Spec 2026-10-28 §2.4 (intake@v32): a part is atomic when it is one thing that can be
    learned and checked on its own — page size has nothing to do with it; no reference line."""
    p = _all_prompts()["outline_block"]
    assert "atomic（true = 它已经是一件能独立学、独立检验的事" in p
    assert "一次能学完、能检验的就是 true" in p
    assert "一页" not in p and "reference" not in p and '"page"' not in p


def test_the_entered_outline_sees_the_whole_graph_with_ids():
    p = _outline_prompt(GOAL, EXISTING, BLOCK_SCOPE, topics=TOPICS,
                        annotations={"c2": "已有证据：模糊"})
    for s in ("hub-la 线性代数（包含：c1 矩阵、c2 矩阵乘法）",
              "c2 矩阵乘法（层面：procedure；属于：hub-la 线性代数；前置：c1 矩阵；已有证据：模糊）",
              '"existing_id":"它的 id"'):
        assert s in p, s


# === S2: the estimate in goals.enter (enter@v1) =====================================================

LA_SCOPE = IntakeScope(goal=f"{GOAL} · 线性代数", region_id="hub-la", region_name="线性代数",
                       scope="矩阵、矩阵乘法、特征值", parent_goal=GOAL,
                       siblings=(("线性代数", "矩阵、矩阵乘法、特征值"), ("概率论", "条件概率")))
LA_PARTS = {"parts": [
    {"name": "矩阵", "existing_id": "c1", "summary": "数排成的表", "atomic": True},
    {"name": "矩阵乘法", "existing_id": "c2", "summary": "行乘列", "atomic": True},
    {"name": "特征值和特征向量", "summary": "Av = λv", "atomic": True,
     "aspects": ["term", "procedure"]}]}


def enter(known=None, words=(SVD_BACKGROUND,), reply=None):
    """One goals.enter call over 线性代数 with LA_PARTS and `known` as its reply."""
    out = reply if reply is not None else {**LA_PARTS, "known": known or []}
    llm = FakeLLM({ENTER: out})
    res = decompose_one_level(llm, LA_SCOPE.goal, words, EXISTING, evidence_ref="goal:m:hub-la",
                 scope=LA_SCOPE, topics=TOPICS)
    return res, llm


def test_the_estimate_part_reads_his_words_and_names_the_levels():
    p = _outline_prompt(GOAL, EXISTING, LA_SCOPE, topics=TOPICS, words=(SVD_BACKGROUND,))
    for s in ("他自己说过的话（只有这些算他的话）",
              SVD_BACKGROUND, "再写 known：", "没说到的部分不写",
              "「还会」「现在会」「还在用」是 intermediate（他说很熟练才是 advanced）",
              "「学过但忘了」「记不清」是 beginner", "「没学过」「不会」是 unlearned",
              "只说「学过」、没说现在会不会：不写",
              "只说到这一整块的话已经记在这一块上了，不要摊到各部分上",
              "quote 从他的话里原样摘出", '"known"'):
        assert s in p, s
    for s in ("Skill Gap Identifier", "skill_gaps", "current_level", "confidence", "polarity",
              "strength", "unknown"):
        assert s not in p, s
    assert not CAPS.search(p), p


def test_no_words_no_estimate_part():
    p = _outline_prompt(GOAL, EXISTING, LA_SCOPE, topics=TOPICS)
    assert "再写 known：" not in p and '"known"' not in p and "他自己说过的话" not in p


def test_still_can_compute_is_a_claim_and_forgotten_is_only_studied():
    res, llm = enter([
        {"part": "矩阵乘法", "level": "intermediate", "quote": "矩阵乘法还会算"},
        {"part": "特征值和特征向量", "level": "beginner", "quote": "特征值和特征向量忘得差不多了"}])
    assert llm.purposes() == [ENTER]                                  # one call
    # the self-report table (goals.selfreport): intermediate = can_do 0.9, beginner = studied 0.2
    assert [(o.cell, o.polarity, o.strength, o.quote) for o in res.observations] == [
        ("c2.procedure", "+", 0.9, "矩阵乘法还会算"),
        ("c3.term", "+", 0.2, "特征值和特征向量忘得差不多了"),
        ("c3.procedure", "+", 0.2, "特征值和特征向量忘得差不多了")]
    assert {o.kind for o in res.observations} == {"self"}


def test_only_verbatim_quotes_become_observations():
    res, _ = enter([{"part": "矩阵乘法", "level": "beginner", "quote": "都忘得差不多了"},
                    {"part": "矩阵", "level": "advanced", "quote": "矩阵我很熟"}],
                   words=(BACKGROUND,))
    assert [(o.cell, o.polarity, o.quote) for o in res.observations] == [
        ("c2.procedure", "+", "都忘得差不多了")]
    assert any(d.reason == "quote_not_verbatim" and d.item.get("quote") == "矩阵我很熟"
               for d in res.dropped)


def test_quotes_may_come_from_the_blocks_said_or_the_goals_words():
    said, words = "矩阵乘法还会算", "我想学机器学习，特征值忘了"
    res, _ = enter([{"part": "c2", "level": "intermediate", "quote": said},
                    {"part": "特征值和特征向量", "aspect": "term", "level": "beginner",
                     "quote": "特征值忘了"}], words=(said, words))
    assert [o.cell for o in res.observations] == ["c2.procedure", "c3.term"]


def test_an_estimate_on_an_aspect_the_part_lacks_is_dropped():
    res, _ = enter([{"part": "矩阵", "aspect": "procedure", "level": "unlearned",
                     "quote": "矩阵乘法还会算"}])
    assert res.observations == () and [d.reason for d in res.dropped] == ["unknown_cell"]


def test_words_about_the_whole_block_are_not_spread_over_its_parts():
    """The block itself is no part (S1 recorded his words on it): an entry naming it is dropped."""
    res, _ = enter([{"part": "线性代数", "level": "beginner", "quote": "大学学过线性代数"}])
    assert res.observations == () and [d.reason for d in res.dropped] == ["unknown_id"]


def test_an_aspectless_estimate_covers_only_that_parts_own_cells():
    res, _ = enter([{"part": "特征值和特征向量", "level": "unlearned",
                     "quote": "特征值和特征向量忘得差不多了"}])
    assert [o.cell for o in res.observations] == ["c3.term", "c3.procedure"]


@pytest.mark.parametrize("known", [None, "x", {"part": "c2"}, 3, ["not an object"]])
def test_known_that_is_not_a_list_of_objects_is_ignored(known):
    res, _ = enter(reply={**LA_PARTS, "known": known})
    assert res.observations == () and [c.name for c in res.concepts][-1] == "特征值和特征向量"


def test_a_failed_call_propagates_and_nothing_is_recorded():
    with pytest.raises(ValueError):
        enter(reply=ValueError("bad json"))


# --- S1: goals.intake (intake@v31, one call per goal) ----------------------------------------------


def _blocks_llm(blocks_reply):
    """goals.intake answers `blocks_reply`; prompts kept."""
    seen: list[tuple[str, str]] = []

    def call(role, purpose, prompt, schema):
        seen.append((purpose, prompt))
        assert purpose == OUTLINE, purpose
        return schema.model_validate(blocks_reply)
    return call, seen


def test_blocks_call_sees_hub_members_and_the_reuse_rule():
    """CREATE for the blocks call: every existing node with its parts (a hub's members, so an
    unrelated hub is not reused — 黑壳虾 root cause 5) and the reuse-after-looking rule."""
    llm, seen = _blocks_llm({"title": "机器学习", "blocks": [
        {"name": "线性代数", "existing_id": "hub-la", "covers": ["矩阵"]}]})
    res = plan_blocks(llm, GOAL, (BACKGROUND,), EXISTING, topics=TOPICS)
    prompt = seen[0][1]
    assert "hub-la 线性代数（包含：c1 矩阵、c2 矩阵乘法）" in prompt
    assert "如果已有 Wiki 节点，只有内容和范围匹配时才复用，否则创建新块" in prompt
    assert "existing_id：确认复用已有 Wiki 节点时填写其实际 ID" in prompt
    assert [p for p, _ in seen] == [OUTLINE]
    assert res.blocks[0].existing_id == "hub-la"


def test_blocks_call_with_no_usable_block_fails_closed():
    llm, seen = _blocks_llm({"blocks": [{"summary": "没有名字"}]})
    with pytest.raises(IntakeError):
        plan_blocks(llm, GOAL, (), ())
    assert [p for p, _ in seen] == [OUTLINE]


def test_a_reading_repair_gets_the_units_and_problems_not_the_material():
    """goals.reading.repair (reading-repair@v1, goals/blocks.py): edge cases of the planning are in
    tests/goals/test_blocks.py; this is what the prompt may carry."""
    from evowiki.goals.blocks import _RawSpan, _RawUnit, repair_prompt

    toc = SourceToc(source_id="s1", title="某份资料", sections=(
        TocSection(id="x1", label="§1", title="引言", chars=500),
        TocSection(id="x2", label="§2", title="方法", chars=0)))
    units = [_RawUnit(title="方法", spans=[_RawSpan(section="§2")])]
    p = repair_prompt(units, toc, ["第 1 个单元「方法」只有标题、没有正文：把它并进它下面的小节"])
    assert "只有标题、没有正文" in p and '"section": "§2"' in p
    assert "§1：只能整节读" in p and "§2：只有标题" in p
    assert "引言" not in p and "读懂" not in p
