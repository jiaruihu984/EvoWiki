"""tests/llm_cases/intake_rubric.py (offline): the structure text the judge sees has names, parts
and prerequisites but no ids; one verdict per rubric item or the judge fails loudly; every case
has the critical false-dependency item and the non-critical leaf item (review 2026-10-14); the
code metrics (duplicate rate, evidence overreach)."""

import pytest

from evowiki.goals.contracts import Observation
from evowiki.goals.intake import IntakeConcept, IntakeRelation, IntakeResult
from tests.llm_cases.intake_rubric import (
    FALSE_DEPENDENCY,
    LEAF,
    DecompositionCase,
    duplicate_rate,
    evidence_overreach,
    item_verdict,
    judge,
    structure_text,
)
from tests.llm_cases.rubric import JudgeOut, RubricJudgeError

CASE = DecompositionCase("x", "我要学乐理", "", "音 → 音程", ("五线谱",))
RES = IntakeResult(
    goal="我要学乐理", title="乐理",
    concepts=(IntakeConcept(id="c1", name="乐理", aspects=("term",), target=True),
              IntakeConcept(id="c2", name="音程", aspects=("term",)),
              IntakeConcept(id="c3", name="五线谱", aspects=("term",))),
    targets=("c1",), observations=(), dropped=(), resolutions=(),
    relations=(IntakeRelation(kind="part_of", src="c2", dst="c1", phrase=""),
               IntakeRelation(kind="prerequisite", src="c3", dst="c2", phrase=""),
               IntakeRelation(kind="recommended", src="c3", dst="c1", phrase="")))


class FakeGateway:
    def __init__(self, items):
        self.items, self.prompts = items, []

    def call(self, role, req, schema):
        self.prompts.append(req.prompt)
        return JudgeOut.model_validate({"items": self.items}), None


def test_structure_text_names_parts_and_prerequisites_without_ids():
    t = structure_text(RES)
    assert "- 乐理（目标）（包含，按学习顺序：音程）（建议先看：五线谱）" in t
    assert "- 音程（之前要会：五线谱）" in t
    assert "c1" not in t and "c2" not in t


def test_one_verdict_per_item():
    gw = FakeGateway([{"index": i, "passed": i != 3, "reason": "r"} for i in range(1, 7)])
    out = judge(gw, CASE, "学会乐理的基础", structure_text(RES))
    assert [r.passed for r in out] == [True, True, False, True, True, True]
    assert "细化后的目标：「学会乐理的基础」" in gw.prompts[0] and "五线谱" in gw.prompts[0]
    assert item_verdict(out, FALSE_DEPENDENCY) == {"passed": True, "reason": "r"}


def test_every_case_has_the_false_dependency_and_leaf_items():
    items = CASE.rubric("g")
    sev = {x.text: x.severity for x in items}
    assert sev[FALSE_DEPENDENCY] == "critical" and sev[LEAF] == "non-critical"
    assert FALSE_DEPENDENCY == ("recommended 关系两端都是具体知识点，"
                                "没有把一整个领域写成另一个点的推荐前置。")
    assert "单独讲解、单独练习、单独出题" in LEAF and "一整个领域（例如一门学科）" in LEAF


def test_case_specific_critical_items_and_no_prerequisite_item_when_none():
    case = DecompositionCase("y", "我要学某样东西", "", "a → b", (), critical=("聚焦",),
                             extra=("工具",))
    items = case.rubric("g")
    assert not [x for x in items if "前置没有缺" in x.text]
    assert [(x.text, x.severity) for x in items if x.text in ("聚焦", "工具")] == [
        ("聚焦", "critical"), ("工具", "non-critical")]


def test_duplicate_rate_counts_repeats_of_a_normalized_name():
    res = RES.model_copy(update={"concepts": (*RES.concepts,
                                             IntakeConcept(id="c4", name=" 音 程", aspects=("term",)))})
    assert duplicate_rate(res) == 0.25 and duplicate_rate(RES) == 0.0


def _obs(cell, polarity="+", quote="会音程"):
    return Observation(cell=cell, polarity=polarity, quote=quote, evidence_ref="g", strength=0.5,
                       kind="self")


def test_evidence_overreach_splits_whole_node_and_per_part():
    concepts = (IntakeConcept(id="c1", name="乐理", aspects=("term", "purpose"), target=True),
                IntakeConcept(id="c2", name="音程", aspects=("term", "procedure")),
                IntakeConcept(id="c3", name="五线谱", aspects=("term",)))
    obs = (_obs("c1.term", "-", "完全零基础"), _obs("c1.purpose", "-", "完全零基础"),
           _obs("c2.procedure", "+", "会算音程"), _obs("c3.term", "-", "完全零基础"))
    res = RES.model_copy(update={"concepts": concepts, "observations": obs,
                                 "relations": (*RES.relations,
                                               IntakeRelation(kind="part_of", src="c3", dst="c1"))})
    got = evidence_overreach(res, "完全零基础，会算音程")
    assert got == {"observed": 3, "unnamed": 2, "unnamed_whole": 2, "unnamed_per_part": 1,
                   "partial": 1, "unnamed_names": ["乐理", "五线谱"], "rate": 0.6667}
    assert evidence_overreach(RES, "x")["rate"] == 0.0


def test_a_missing_verdict_fails_loudly():
    with pytest.raises(RubricJudgeError):
        judge(FakeGateway([{"index": 1, "passed": True, "reason": "r"}]), CASE, "g", "s")
