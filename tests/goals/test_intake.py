"""goals/intake.py (S2, goals.enter, enter@v1): ONE call splits a block being entered one level
(the scoped outline: every block of the goal with its covers, his limits for the goal), in
learning order, ids in learning order, no prerequisite at all — only optional unverified
`recommended` edges (relation layers) — and, with his own words, estimates per cell what he knows
(`known` → K self-reports, verbatim quotes only); the code drops only what cannot work. The eager intake
(refine, split levels, the unscoped and understand outlines) is gone: S1 (goals/blocks.py) makes
a goal. Through the real Gateway (scripted providers)."""

import json

import pytest

from evowiki.goals.contracts import Aspect
from evowiki.goals.intake import (
    ConceptRef,
    IntakeError,
    IntakeRelation,
    IntakeScope,
    TopicRef,
    decompose_one_level,
)
from evowiki.goals.selfreport import SELF_CLAIM

from .fakes_intake import ESTIMATE_MARK, scripted_llm, to_parts

A = Aspect
OUTLINE = "goals.enter"
SCOPE = IntakeScope(goal="某目标 · 某块", region_id="c9", region_name="某块", scope="甲、乙",
                    parent_goal="某目标", siblings=(("某块", "甲、乙"), ("另一块", "丙")))


class _Calls:
    def __init__(self, provider):
        self.provider = provider

    def of(self, purpose):
        return [r for r in self.provider.calls if r.purpose == purpose]


def _llm(tmp_path, outline, known=None):
    def reply(purpose, prompt):
        assert purpose == OUTLINE, purpose
        out = json.loads(to_parts(json.dumps(outline, ensure_ascii=False)))   # parts + atomic
        if known is not None:
            out["known"] = known
        return json.dumps(out, ensure_ascii=False)

    llm, gen, _ = scripted_llm(tmp_path, reply)
    return llm, _Calls(gen)


def blk(name, page=True, **extra):
    return {"name": name, "summary": f"{name}讲什么", "page": page, **extra}


def run(llm, said="", existing=(), **kw):
    return decompose_one_level(llm, SCOPE.goal, (said,) if said else (), list(existing), evidence_ref="s",
                  scope=SCOPE, **kw)


def test_one_layer_in_learning_order_and_no_split_call(tmp_path):
    llm, calls = _llm(tmp_path, {"title": "某块", "reference": "某本教材",
                                 "blocks": [blk("甲"), blk("乙", False, target=True), blk("丙")]})
    res = run(llm)
    # intake@v32: no reference any more; each part's atomic (page → atomic here)
    assert [(c.id, c.name, c.atomic, c.target) for c in res.concepts] == [
        ("c1", "甲", True, False), ("c2", "乙", False, True), ("c3", "丙", True, False)]
    assert res.targets == ("c2",) and res.regions == ()
    assert [r.purpose for r in calls.provider.calls] == [OUTLINE]       # nothing said: no gap
    p = calls.of(OUTLINE)[0].prompt
    assert "2. 「另一块」（丙）" in p and "这一次只把下面这一块往下拆一层" in p
    assert "对自己的描述" not in p and "细化后的目标" not in p


def test_no_usable_part_is_an_error(tmp_path):
    for i, out in enumerate(({"blocks": []}, {"blocks": [{"name": " "}, "x"]},
                             {"title": "t", "nodes": [{"id": "n1", "name": "旧形状"}]})):
        llm, _ = _llm(tmp_path / str(i), out)
        with pytest.raises(IntakeError):
            run(llm)


def test_the_last_part_is_the_default_target(tmp_path):
    llm, _ = _llm(tmp_path, {"blocks": [blk("甲"), blk("乙")]})
    res = run(llm)
    assert res.targets == ("c2",) and [c.target for c in res.concepts] == [False, True]


def test_recommended_ends_are_names_or_existing_ids_and_bad_ones_are_dropped(tmp_path):
    """intake@v20: optional `recommended` become unverified recommended relations; an end naming
    no node is unknown_id, both ends one node a self_loop, a repeated pair keeps the first. An
    older reply's `requires` is ignored: intake writes no prerequisite."""
    existing = [ConceptRef(id="c1", name="旧点", aspects=(A.TERM,), prereqs=())]
    out = {"blocks": [blk("甲"), blk("乙", target=True)], "recommended": [
        {"from": "c1", "to": "乙", "why": "看过旧点更好懂"}, {"from": "甲", "to": "无此块", "why": "x"},
        {"from": "乙", "to": "乙", "why": "自己"}, {"from": "乙", "to": "c1", "why": "反过来"},
        {"from": "c1", "to": "乙", "why": "重复"}],
        "requires": [{"from": "甲", "to": "乙", "why": "旧形状"}]}
    llm, _ = _llm(tmp_path, out)
    res = run(llm, existing=existing)
    assert [(c.id, c.name) for c in res.concepts] == [("c2", "甲"), ("c3", "乙")]
    assert [(r.kind, r.src, r.dst, r.phrase) for r in res.relations] == [
        ("recommended", "c1", "c3", "看过旧点更好懂"), ("recommended", "c3", "c1", "反过来")]
    assert all(c.prereqs == () for c in res.concepts)
    assert [d.reason for d in res.dropped] == ["unknown_id", "self_loop"]


EXISTING = [ConceptRef(id="c1", name="矩阵", aspects=(A.TERM,)),
            ConceptRef(id="c2", name="矩阵乘法", aspects=(A.PROCEDURE,), prereqs=("c1",))]
TOPICS = [TopicRef(id="hub-la", name="某领域", members=("c1", "c2"))]
RELS = [IntakeRelation(kind="part_of", src="c2", dst="hub-la", phrase="最常用的运算")]


def test_a_part_reuses_an_existing_node_by_id_or_exact_name(tmp_path):
    out = {"blocks": [{"existing_id": "c1", "summary": "基本对象", "page": True},
                      blk(" 矩阵 乘法"), {"existing_id": "zz", "name": "新点", "page": True},
                      {"existing_id": "zz"}]}
    llm, calls = _llm(tmp_path, out)
    res = run(llm, existing=EXISTING, topics=TOPICS, graph_relations=RELS,
              annotations={"c2": "已有证据：计算过程 可能会"})
    assert [(c.id, c.name, c.reused) for c in res.concepts] == [
        ("c1", "矩阵", True), ("c2", "矩阵乘法", True), ("c3", "新点", False)]
    assert [(x.action, x.existing, x.name) for x in res.resolutions] == [
        ("merged", "c2", "矩阵 乘法")]
    assert [(d.reason, d.item["existing_id"]) for d in res.dropped] == [
        ("unknown_id", "zz"), ("unknown_id", "zz")]
    assert next(c for c in res.concepts if c.id == "c1").definition == "基本对象"
    p = calls.of(OUTLINE)[0].prompt
    assert "hub-la 某领域（包含：c1 矩阵、c2 矩阵乘法）" in p
    assert "c2 矩阵乘法（层面：procedure；属于：hub-la 某领域；前置：c1 矩阵；已有证据：计算过程 可能会）" in p


def test_his_limits_shape_the_outline(tmp_path):
    llm, calls = _llm(tmp_path, {"blocks": [blk("甲"), blk("乙")]})
    run(llm, limits=("尽快读到第一页", " "))
    assert "他为这个目标提的条件：尽快读到第一页" in calls.of(OUTLINE)[0].prompt


def test_his_words_are_estimated_in_the_same_call_as_verbatim_self_reports(tmp_path):
    """S2 (enter@v1): one call; `known` by part name; a verbatim quote → kind self on that cell
    (strength by level), an aspect-less entry → every cell of that part; the parts keep the
    split's meaning."""
    known = [{"part": "乙", "aspect": "procedure", "level": "intermediate", "quote": "乙我会算"},
             {"part": "甲", "level": "beginner", "quote": "甲学过但忘了"}]
    llm, calls = _llm(tmp_path, {"blocks": [blk("甲", aspects=["term", "purpose"]),
                                            blk("乙", aspects=["procedure"])]}, known)
    res = decompose_one_level(llm, SCOPE.goal, ("乙我会算，甲学过但忘了",), [], evidence_ref="goal:m:c9",
                 scope=SCOPE)
    assert len(calls.provider.calls) == 1
    p = calls.of(OUTLINE)[0].prompt
    assert ESTIMATE_MARK in p and "乙我会算，甲学过但忘了" in p and '"known"' in p
    obs = {(o.cell, o.polarity, o.strength, o.kind, o.quote, o.evidence_ref)
           for o in res.observations}
    # the self-report table (goals.selfreport): intermediate = can_do, beginner = studied
    can_do, studied = SELF_CLAIM["can_do"][1], SELF_CLAIM["studied"][1]
    assert obs == {("c2.procedure", "+", can_do, "self", "乙我会算", "goal:m:c9"),
                   ("c1.term", "+", studied, "self", "甲学过但忘了", "goal:m:c9"),
                   ("c1.purpose", "+", studied, "self", "甲学过但忘了", "goal:m:c9")}
    assert [c.name for c in res.concepts] == ["甲", "乙"]


def test_an_estimate_that_cannot_work_is_dropped_and_the_split_stands(tmp_path):
    known = [{"part": "乙", "level": "advanced", "quote": "乙我很熟"},          # not his words
             {"part": "某块", "level": "unlearned", "quote": "某块没学过"},       # the block itself
             {"part": "乙", "aspect": "mechanism", "level": "unlearned", "quote": "某块没学过"},
             {"part": "甲", "level": "会一点", "quote": "某块没学过"},
             {"part": 7, "level": None, "quote": ["x"]}]                       # not strings
    llm, _ = _llm(tmp_path, {"blocks": [blk("甲"), blk("乙", aspects=["procedure"])]}, known)
    res = run(llm, "某块没学过")
    assert res.observations == () and [c.name for c in res.concepts] == ["甲", "乙"]
    reasons = [d.reason for d in res.dropped]
    assert {"quote_not_verbatim", "unknown_id", "unknown_cell", "bad_level"} <= set(reasons)


def test_without_his_words_nothing_is_estimated_or_asked(tmp_path):
    """D07 (a stated beginner, a block not to diagnose) and nothing said: the prompt is the split
    alone and its schema has no `known` — a `known` the model writes anyway is not read."""
    from evowiki.goals.intake import _EnterOut, _OneLevelOut, _outline_prompt
    llm, calls = _llm(tmp_path, {"blocks": [blk("甲"), blk("乙")]},
                      [{"part": "甲", "level": "advanced", "quote": "某目标"}])
    res = run(llm)
    p = calls.of(OUTLINE)[0].prompt
    assert ESTIMATE_MARK not in p and '"known"' not in p and res.observations == ()
    from evowiki.llm.prompt_steps import strip_marks
    assert p == strip_marks(_outline_prompt(SCOPE.goal, [], SCOPE))   # the v32 split text
    assert "known" not in _OneLevelOut.model_json_schema()["properties"]
    assert "known" in _EnterOut.model_json_schema()["properties"]


def test_an_invalid_split_is_asked_again_with_the_estimate_part(tmp_path):
    replies = iter([{"parts": [{"name": "某块", "atomic": True}]},
                    {"parts": [{"name": "甲", "atomic": True}, {"name": "乙", "atomic": True}],
                     "known": [{"part": "甲", "level": "intermediate", "quote": "甲我会"}]}])

    def reply(purpose, prompt):
        assert purpose == OUTLINE and ESTIMATE_MARK in prompt
        return json.dumps(next(replies), ensure_ascii=False)
    llm, gen, _ = scripted_llm(tmp_path, reply)
    res = run(llm, "甲我会")
    assert len(gen.calls) == 2 and "上一次的拆法不能用" in gen.calls[1].prompt
    assert {o.cell for o in res.observations} == {"c1.term", "c1.purpose"}


def test_known_fields_are_strings_and_a_non_string_value_reads_as_empty_never_a_failure():
    from evowiki.goals.intake import _EnterOut
    out = _EnterOut.model_validate({"parts": [], "known": [
        {"part": "c1", "level": "beginner", "aspect": ["x"], "quote": None}, 7]})
    k = out.known[0]
    assert (k.part, k.level, k.aspect, k.quote) == ("c1", "beginner", "", "")
    assert len(out.known) == 1


def test_materials_and_his_words_in_the_one_call(tmp_path):
    """Materials (spec 2026-10-28 §2.5) and the estimate share the one goals.enter call: the
    block's sections and his words are both in the prompt; the answer gives links and known."""
    from evowiki.goals.intake import SourceToc, TocSection
    toc = SourceToc(source_id="s1", title="讲义", sections=(
        TocSection(id="x1", label="§1", title="1 甲"), TocSection(id="x2", label="§2", title="2 乙")))
    llm, calls = _llm(tmp_path, {"blocks": [blk("甲", sections=["§1"]), blk("乙", sections=["§2"])]},
                      [{"part": "乙", "level": "intermediate", "quote": "乙我会"}])
    res = decompose_one_level(llm, SCOPE.goal, ("乙我会",), [], evidence_ref="s", scope=SCOPE, sources=(toc,))
    p = calls.of(OUTLINE)[0].prompt
    assert len(calls.provider.calls) == 1 and ESTIMATE_MARK in p and "§2 2 乙" in p
    assert {(x.node_id, x.section_id, x.by) for x in res.links} == {("c1", "x1", "enter"),
                                                                   ("c2", "x2", "enter")}
    assert {o.cell for o in res.observations} == {"c2.term", "c2.purpose"}
