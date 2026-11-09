"""What the render check sees in a drawing: text half on a shape fails and goes back to the author; text in its box or beside a
shape does not; a state a control reaches is looked at too; a label beside a bar does not break inside a word."""

from typing import Any

import pytest

from evowiki.deep.evoui import harness_html
from evowiki.deep.evoui.contracts import EvoUIBlock
from tests.deep.test_evoui_render import checker  # noqa: F401 - the module's chromium fixture


def block(*marks: dict, **over: Any) -> EvoUIBlock:
    return EvoUIBlock.model_validate({
        "type": "evoui", "title": "t", "question": "q", "state": {"s": 0}, "derived": {}, "fallback": "f",
        "body": {"kind": "svg", "viewBox": "0 0 640 300", "children": list(marks)}, **over})


BAR = {"el": "rect", "x": 40, "y": 60, "width": 34, "height": 160, "fill": "blue"}


def test_text_half_on_a_bar_fails_with_the_reason(checker):  # noqa: F811
    r = checker.check_block("b", block(BAR, {"el": "text", "x": 40, "y": 100, "text": "已累计 196 元，极限是 250 元"}))
    assert not r.ok and "压在图形上" in r.detail, r


@pytest.mark.parametrize("mark", [
    {"el": "text", "x": 80, "y": 100, "text": "柱子右边的一句话"},          # beside it
    {"el": "text", "x": 57, "y": 56, "text-anchor": "middle", "text": "9"},  # a value just above it
    {"el": "text", "x": 57, "y": 100, "text-anchor": "middle", "text": "9"},  # inside it
])
def test_text_beside_above_or_inside_a_bar_passes(checker, mark):  # noqa: F811
    assert checker.check_block("b", block(BAR, mark)).ok


def test_text_on_a_soft_fill_stays_readable_and_passes(checker):  # noqa: F811
    soft = {**BAR, "fill": "blue-soft"}
    assert checker.check_block("b", block(soft, {"el": "text", "x": 40, "y": 100, "text": "已累计 196 元，极限是 250 元"})).ok


def test_a_node_with_its_own_text_passes(checker):  # noqa: F811
    assert checker.check_block("b", block({"el": "node", "x": 10, "y": 10, "width": 80, "height": 40, "text": "一个节点里的文字可以换行"})).ok


def test_a_state_a_control_reaches_is_looked_at_too():
    from evowiki.deep.render import PlaywrightChecker
    b = EvoUIBlock.model_validate({
        "type": "evoui", "title": "t", "question": "q", "state": {"s": 0}, "derived": {}, "fallback": "f",
        "body": {"kind": "box", "children": [
            {"kind": "slider", "bind": "s", "min": 0, "max": 1, "step": 1, "label": "第 {s} 步"},
            {"kind": "svg", "viewBox": "0 0 640 300", "children": [
                BAR, {"el": "text", "x": "{s == 1 ? 40 : 200}", "y": 100, "text": "这句话走到柱子上了，走到了柱子上"}]}]}})
    r = PlaywrightChecker().check_block("b", b)
    assert not r.ok and "压在图形上" in r.detail, r


def test_what_a_nested_branch_drew_goes_when_the_outer_branch_switches():
    from playwright.sync_api import sync_playwright
    inner = {"kind": "if", "cond": "s == 0", "then": {"el": "text", "x": 1, "y": 9, "text": "内层"}, "else": None}
    loop = {"kind": "each", "in": "s == 2 ? [] : [1, 2]", "as": "a", "index": "b", "children": [inner]}
    b = EvoUIBlock.model_validate({
        "type": "evoui", "title": "t", "question": "q", "state": {"s": 0}, "derived": {}, "fallback": "f",
        "body": {"kind": "svg", "viewBox": "0 0 640 300", "children": [
            {"kind": "if", "cond": "s == 0", "then": {"el": "g", "children": []}, "else": {"el": "text", "x": 1, "y": 9, "text": "另一支"}},
            {"kind": "if", "cond": "s == 0", "then": inner, "else": None}, loop]}})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(harness_html(b))
        texts = "()=>[...document.querySelectorAll('svg.evoui-svg text')].map(t=>t.textContent)"
        assert page.evaluate(texts) == ["内层"] * 3
        page.evaluate("()=>window.__evoui.set('s', 1)")
        assert page.evaluate(texts) == ["另一支"]
        page.evaluate("()=>window.__evoui.set('s', 0)")
        assert page.evaluate(texts) == ["内层"] * 3
        page.evaluate("()=>window.__evoui.set('s', 2)")
        assert page.evaluate(texts) == ["另一支"]
        browser.close()


def test_a_label_beside_a_bar_does_not_break_inside_a_word():
    from playwright.sync_api import sync_playwright
    b = EvoUIBlock.model_validate({
        "type": "evoui", "title": "t", "question": "q", "state": {}, "derived": {}, "fallback": "f",
        "body": {"kind": "row", "gap": 2, "children": [
            {"kind": "text", "text": "R1", "size": "xs"},
            {"kind": "box", "background": "blue", "width": "100%", "height": 14, "children": []},
            {"kind": "text", "text": "12.0", "size": "xs"}]}})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 200, "height": 300})
        page.set_content(harness_html(b))
        lines = page.evaluate("()=>[...document.querySelectorAll('.evoui-row>.evoui-text')].map(e=>{const g=document.createRange();g.selectNodeContents(e);return g.getClientRects().length})")
        browser.close()
    assert lines == [1, 1]


def test_a_program_wider_than_the_narrowest_pane_fails(checker):  # noqa: F811
    """凯恩斯 chain-of-rounds in real Obsidian: the frame of a narrow pane is 238 px, its row of inputs needed 267."""
    wide = EvoUIBlock.model_validate({"type": "evoui", "title": "t", "question": "q", "state": {}, "derived": {}, "fallback": "f", "body": {
        "kind": "app", "html": "<div style='display:flex;gap:8px'><input style='min-width:260px'><button id='a' onclick='this.textContent=1'>0</button></div>"
                               "<script>evowiki.ready()</script>"}})
    r = checker.check_block("b", wide)
    assert not r.ok and "溢出" in r.detail, r


def test_a_block_is_photographed_opening_after_a_control_narrow_and_dark(checker):  # noqa: F811
    b = EvoUIBlock.model_validate({"type": "evoui", "title": "t", "question": "q", "state": {"s": 0}, "derived": {}, "fallback": "f",
                                   "body": {"kind": "box", "children": [{"kind": "slider", "bind": "s", "min": 0, "max": 9, "step": 1, "label": "s"},
                                                                        {"kind": "svg", "viewBox": "0 0 640 100", "children": [
                                                                            {"el": "rect", "x": "{s * 50}", "y": 10, "width": 40, "height": 40, "fill": "blue"}]}]}})
    seen = checker.look_block("b", b, ())
    assert seen.shots[0][0] == "刚打开" and len(seen.shots) == 4 and "最窄的窗格" in seen.shots[2][0]
