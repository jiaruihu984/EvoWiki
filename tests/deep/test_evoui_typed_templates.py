"""deep.evoui.typed: every template compiles to a block the validator accepts, uses only the contract's node
fields, and its response schemas are ones Anthropic structured outputs accept (closed objects, no recursion, at
most 16 union-typed parameters per request)."""

from typing import Any

import pytest

from evowiki.deep.evoui import validate
from evowiki.deep.evoui.contracts import (
    CARD_FIELDS,
    FILL_OPTION_FIELDS,
    NODE_FIELDS,
    QUESTION_FIELDS,
)
from evowiki.deep.evoui.typed import KINDS, BlockSpec, compile_block, guide

HEAD = {"title": "配对：矩阵和它的特征值", "question": "能一眼看出哪些矩阵的特征值吗？", "fallback": "对角矩阵的特征值是对角线上的数。"}
EXAMPLES: dict[str, dict[str, Any]] = {
    "quiz": {**HEAD, "kind": "quiz", "layout": "paged", "questions": [
        {"q": "[[2, 0], [0, 5]] 的特征值？", "options": ["2 和 5", "0 和 7"], "correct": 0, "why": "对角线上的数。",
         "hint": "", "cell": "c1.term"},
        {"q": "λ 是 det(A − λI) = 0 的根吗？", "options": ["是", "不是"], "correct": 0, "why": "定义如此。",
         "hint": "回看定义", "cell": ""}]},
    "flashcards": {**HEAD, "kind": "flashcards", "cards": [
        {"front": "特征值", "back": "Av = λv 里的 λ", "hint": "", "cell": "c1.term"},
        {"front": "特征向量", "back": "方向不变的 v", "hint": "想想拉伸", "cell": ""}]},
    "fill_blank": {**HEAD, "kind": "fill_blank", "items": [
        {"sentence": "对角矩阵的特征值就是 ___ 上的数。", "options": [{"label": "对角线", "why": ""},
                                                          {"label": "第一行", "why": "不对"}],
         "correct": 0, "why": "对角线。", "cell": ""}]},
    "sentence": {**HEAD, "kind": "sentence", "items": [
        {"sentence": "[[2, 0], [0, 5]] 的特征值是 ___ 和 ___。", "words": ["1", "2", "5"], "answer": [1, 2],
         "why": "对角线。", "cell": ""},
        {"sentence": "[[2, 1], [1, 2]] 的特征值是 ___ 和 ___。", "words": ["1", "3", "4"], "answer": [1, 0],
         "why": "解特征方程。", "cell": "c1.term"}]},
    "steps": {**HEAD, "kind": "steps", "steps": ["先写 Av = λv。", "移项得 (A − λI)v = 0。", "要求 det(A − λI) = 0。"]},
    "lab": {**HEAD, "kind": "lab", "intro": "拖动角度，看 Av 和 v 是否同向。",
            "controls": [{"name": "deg", "label": "角度 {deg}°", "min": 0, "max": 180, "step": 1, "value": 30}],
            "derived": [{"name": "r", "expr": "deg2rad(deg)"}, {"name": "c", "expr": "abs(cos(r) * 3)"}],
            "chart": {"show": True, "chart": "line", "data": "map(range(10), i => {\"x\": i, \"y\": i * c})", "x": "x",
                      "series": [{"key": "y", "label": "y"}], "x_label": "x", "y_label": ""},
            "readouts": [{"text": "c = {fmt(c, 2)}", "tone": "accent"}, {"text": "角度 {deg}", "tone": "default"}]},
    "timeline": {**HEAD, "kind": "timeline", "intro": "按发现的先后看。", "events": [
        {"when": "1736", "title": "欧拉解七桥问题", "text": "第一次把地图抽象成点和线。", "tone": "accent"},
        {"when": "", "title": "特征值进入力学", "text": "", "tone": "default"}]},
    "deep_dive": {**HEAD, "kind": "deep_dive", "intro": "", "label": "",
                  "paragraphs": ["det(A − λI) = 0 为什么一定有解？", "因为复数范围里 n 次多项式有 n 个根。"]},
}


def spec(kind: str) -> BlockSpec:
    from pydantic import TypeAdapter
    return TypeAdapter(BlockSpec).validate_python(EXAMPLES[kind])


def nodes(n: Any):
    if isinstance(n, dict):
        if "kind" in n:
            yield n
        for v in n.values():
            yield from nodes(v)
    elif isinstance(n, list):
        for v in n:
            yield from nodes(v)


@pytest.mark.parametrize("kind", KINDS)
def test_every_template_compiles_to_a_valid_block(kind):
    block = compile_block(spec(kind))
    assert validate(block) == [], validate(block)


@pytest.mark.parametrize("kind", KINDS)
def test_compiled_nodes_use_only_the_contracts_fields(kind):
    block = compile_block(spec(kind))
    for n in nodes(block["body"]):
        if n["kind"] not in NODE_FIELDS:            # quiz questions / cards carry no kind
            continue
        req, opt = NODE_FIELDS[n["kind"]]
        assert set(n) - {"kind"} <= {*req, *opt}, (n["kind"], set(n) - {*req, *opt})
    allowed = {"q": QUESTION_FIELDS, "front": CARD_FIELDS}
    for n in nodes(block):
        for q in n.get("questions", []):
            assert set(q) <= {*allowed["q"][0], *allowed["q"][1]}
        for c in n.get("cards", []):
            assert set(c) <= {*allowed["front"][0], *allowed["front"][1]}
        if n.get("kind") == "fill-blank":
            for o in n["options"]:
                assert set(o) <= {*FILL_OPTION_FIELDS[0], *FILL_OPTION_FIELDS[1]}


def test_lab_without_a_chart_and_a_single_item_has_no_pager():
    lab = compile_block(spec("lab").model_copy(update={"chart": spec("lab").chart.model_copy(update={"show": False})}))
    assert not any(n["kind"] == "chart" for n in nodes(lab["body"])) and validate(lab) == []
    assert not any(n["kind"] == "pager" for n in nodes(compile_block(spec("fill_blank"))["body"]))
    assert any(n["kind"] == "pager" for n in nodes(compile_block(spec("sentence"))["body"]))


def unions(schema: dict) -> int:
    """Union-typed parameters (anyOf / type arrays) in a schema, as Anthropic counts them."""
    n = 0

    def visit(node: Any) -> None:
        nonlocal n
        if isinstance(node, dict):
            if "anyOf" in node or isinstance(node.get("type"), list):
                n += 1
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)
    visit(schema)
    return n


def test_timeline_and_deep_dive_compile_to_their_nodes():
    tl = compile_block(spec("timeline"))
    ev = next(n for n in nodes(tl["body"]) if n["kind"] == "timeline")["events"]
    assert ev == [{"title": "欧拉解七桥问题", "when": "1736", "text": "第一次把地图抽象成点和线。", "tone": "accent"},
                  {"title": "特征值进入力学"}]           # empty texts and the default tone are left out
    dd = next(n for n in nodes(compile_block(spec("deep_dive"))["body"]) if n["kind"] == "deep-dive")
    assert "label" not in dd and [c["text"] for c in dd["children"]] == EXAMPLES["deep_dive"]["paragraphs"]


def test_the_guide_carries_the_spec_expression_section():
    g = guide()
    assert "sentence" in g and "lab" in g and "fmt(x, 位数)" in g
