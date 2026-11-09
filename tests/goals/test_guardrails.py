import copy

import pytest
from pydantic import ValidationError

from evowiki.goals.contracts import Band, SectionRole
from evowiki.goals.guardrails import (
    DraftOutline,
    PageSpec,
    check_outline,
    diagnose_outline,
    draft_of,
    page_bands,
    to_outline,
)
from tests.goals.fakes_k import ZSCORE_OUTLINE, zscore_k, zscore_spec


def _draft(edit=None) -> DraftOutline:
    raw = copy.deepcopy(ZSCORE_OUTLINE)
    if edit:
        edit(raw)
    return DraftOutline.model_validate(raw)


def _check(draft, k=None):
    spec, k = zscore_spec(), k or zscore_k()
    return check_outline(draft, spec, page_bands(spec, k))


def _diag(draft, k=None):
    spec, k = zscore_spec(), k or zscore_k()
    return diagnose_outline(draft, spec, page_bands(spec, k))


def _rules(vs):
    return {v.rule for v in vs}


def test_page_bands_follow_concept_then_aspect_order():
    spec = zscore_spec()
    bands = page_bands(spec, zscore_k())
    assert list(bands)[:3] == ["mean.term", "mean.notation", "mean.procedure"]
    assert bands["z_score.interpret"] is Band.GAP


def test_zscore_outline_passes():
    assert _check(_draft()) == [] and _diag(_draft()) == []


# coverage, analogy, known explained: diagnostics only - the outline is still usable
def test_uncovered_weak_cell_is_diagnosed_not_refused():
    d = _draft(lambda r: r["sections"].pop(2))
    vs = _diag(d)
    assert _check(d) == [] and _rules(vs) == {"coverage"}
    assert "z-score·解读结果" in vs[0].message


def test_uncovered_target_cell_is_diagnosed_even_if_known():
    d = _draft(lambda r: r["sections"][0]["cells"].remove("z_score.purpose"))
    assert _check(d) == [] and _rules(_diag(d)) == {"coverage"}


def test_weak_cell_only_in_line_is_diagnosed():
    def edit(r):
        r["sections"][0]["cells"].append("z_score.interpret")
        r["sections"].pop(2)
    d = _draft(edit)
    vs = _diag(d)
    assert _check(d) == [] and _rules(vs) == {"coverage"} and "没有被提醒或讲解" in vs[0].message


def test_assumed_cell_not_known_is_diagnosed():
    d = _draft(lambda r: r["assumed"].append("mean.procedure"))
    assert _check(d) == []
    assert "coverage" in _rules(_diag(d)) and any("默认你会" in v.message for v in _diag(d))


def test_analogy_from_unknown_cell_is_diagnosed():
    d = _draft(lambda r: r["sections"][2].update(analogy_sources=["variance.procedure"]))
    vs = _diag(d)
    assert _check(d) == [] and _rules(vs) == {"analogy"} and vs[0].section == "s3"


def test_analogy_from_user_background_is_fine():
    assert _diag(_draft(lambda r: r["sections"][2].update(
        analogy_sources=["背景：做过一点数据分析"]))) == []


def test_known_cell_in_explain_is_diagnosed():
    d = _draft(lambda r: r["sections"][2]["cells"].append("mean.term"))
    assert _check(d) == [] and _rules(_diag(d)) == {"known_explained"}


# lookup
def test_lookup_checks_format_only_not_notes_content():
    """needs_lookup format only; the LLM lists version-dependent claims itself."""
    notes = "用 v1、v2 表示两个基向量，sklearn.decomposition.PCA() 版本 1.4.2"
    assert _check(_draft(lambda r: r["sections"][2].update(notes=notes, needs_lookup=[]))) == []


def test_lookup_items_must_be_well_formed():
    assert _rules(_check(_draft(lambda r: r["sections"][2].update(needs_lookup=["  "])))) == {"lookup"}
    assert _rules(_check(_draft(lambda r: r["sections"][2].update(needs_lookup=["a", "a"])))) == {"lookup"}
    assert _rules(_check(_draft(lambda r: r["sections"][2].update(needs_lookup=["x" * 121])))) == {"lookup"}


# no caps: length follows the topic (user ruling 2026-10-10)
def test_many_sections_recaps_checks_are_neither_refused_nor_diagnosed():
    chk = {"cell": "z_score.interpret", "stem": "z=1 时？", "options": ["a", "b", "c"], "answer": 0}

    def many(r):
        r["sections"] += [{"id": f"o{i}", "heading": "开篇", "role": "organizer"} for i in range(6)]
        r["sections"] += [{"id": f"r{i}", "heading": "回顾", "role": "recap",
                           "cells": ["mean.procedure"]} for i in range(5)]
        r["sections"] += [{"id": f"c{i}", "heading": "确认", "role": "check", "check": chk}
                          for i in range(4)]
    d = _draft(many)
    assert len(d.sections) > 8 and _check(d) == [] and _diag(d) == []


# shape: what cannot be rendered or graded
def test_shape_violations():
    with pytest.raises(ValidationError):              # a role is one of the six, by schema
        _draft(lambda r: r["sections"][0].update(role="essay"))
    assert _rules(_check(_draft(lambda r: r["sections"][0]["cells"].append("pca.term")))) == {"shape"}
    for opts in (["a"], [str(i) for i in range(9)]):                    # 1, 9 options
        bad_check = {"cell": "z_score.interpret", "stem": "?", "options": opts, "answer": 0}
        assert _rules(_check(_draft(lambda r, b=bad_check: r["sections"].append(
            {"id": "c", "heading": "确认", "role": "check", "check": b})))) == {"shape"}
    for opts in (["对", "错"], [str(i) for i in range(8)]):              # 2–8 distinct: fine
        ok = {"cell": "z_score.interpret", "stem": "?", "options": opts, "answer": 0}
        assert _rules(_check(_draft(lambda r, b=ok: r["sections"].append(
            {"id": "c", "heading": "确认", "role": "check", "check": b})))) == set()
    assert _rules(_check(_draft(lambda r: r["sections"].append(
        {"id": "c", "heading": "确认", "role": "check"})))) == {"shape"}
    ok_check = {"cell": "z_score.interpret", "stem": "z=1 时？", "options": ["a", "b", "c"], "answer": 0}
    assert _rules(_check(_draft(lambda r: r["sections"][0].update(check=ok_check)))) == {"shape"}
    assert _check(DraftOutline())[0].rule == "shape"


def test_to_outline_normalises_ids_and_round_trips():
    def edit(r):
        r["sections"][1]["id"] = "s1"
        r["sections"][2]["id"] = ""
    spec, k = zscore_spec(), zscore_k()
    draft = _draft(edit)
    out = to_outline(draft, spec, page_bands(spec, k))
    assert [s.id for s in out.sections] == ["s1", "s1x", "s3"]
    assert out.sections[2].role is SectionRole.EXPLAIN and out.source == "organizer"
    assert out.concepts == ("mean", "variance", "z_score") and out.targets == ("z_score",)
    assert check_outline(draft_of(out), spec, page_bands(spec, k)) == []


def test_page_spec_targets_must_be_concepts():
    with pytest.raises(ValueError):
        zscore_spec(targets=("pca",))
    assert isinstance(zscore_spec(), PageSpec)


def test_a_check_with_latex_is_refused_like_a_malformed_one():
    tex = {"cell": "z_score.interpret", "stem": r"$z=1$ 时？", "options": ["a", "b", "c"],
           "answer": 0}
    plain = {**tex, "stem": "z=1 时？", "options": ["x^2", "b", "c"]}
    add = lambda chk: lambda r: r["sections"].append(
        {"id": "c", "heading": "确认", "role": "check", "check": chk})
    v = _check(_draft(add(tex)))
    assert _rules(v) == {"shape"} and "LaTeX" in v[0].message
    assert "「$z=1$」" in v[0].message     # the retry is told which markup to take out
    assert _check(_draft(add(plain))) == []


def test_a_check_without_cell_takes_its_sections_one_cell_and_with_two_says_it_is_missing():
    chk = {"stem": "z=1 时？", "options": ["a", "b"], "answer": 0}
    add = lambda cells: lambda r: r["sections"].append(
        {"id": "c", "heading": "确认", "role": "check", "cells": cells, "check": chk})
    assert _check(_draft(add(["z_score.interpret"]))) == []
    assert "check.cell 没写" in _check(_draft(add(["z_score.interpret", "mean.term"])))[0].message
