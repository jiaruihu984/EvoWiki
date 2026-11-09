"""The render check runs the plugin's own guard (guard.js) and hands the writer what the screen said."""

from evowiki.deep.evoui.contracts import EvoUIBlock, EvoUIProbe
from evowiki.deep.tools import check_text
from tests.deep.test_evoui_render import checker  # noqa: F401 - the module's chromium fixture


def lab(derived: dict, *children: dict, state: dict | None = None) -> EvoUIBlock:
    return EvoUIBlock.model_validate({
        "type": "evoui", "title": "实验室", "question": "问题", "fallback": "结论",
        "state": state or {"p0": 0.5, "p10": 0.3}, "derived": derived,
        "body": {"kind": "box", "children": list(children)}})


SLIDERS = ({"kind": "slider", "bind": "p0", "min": 0, "max": 1, "step": 0.1, "label": "p0 = {fmt(p0, 2)}"},
           {"kind": "slider", "bind": "p10", "min": 0, "max": 1, "step": 0.1, "label": "p10 = {fmt(p10, 2)}"})


def test_a_block_the_plugin_would_refuse_fails_with_the_reason_and_how_to_write_it(checker):  # noqa: F811
    b = lab({"xs": "map(range(3), i => ['a', 'b', 'c'][i])", "total": "sum(map(range(3), i => [1, 2, 3][xs[i]] * p0))"},
            *SLIDERS, {"kind": "text", "text": "和 {fmt(total, 2)}"})
    r = checker.check_block("lab", b)
    assert not r.ok and "xs:" in r.detail and "对象键不加引号" in r.detail, r
    assert r.readings == ()


def test_a_sum_that_comes_to_nan_does_not_pass(checker):  # noqa: F811
    b = lab({"xs": "map(range(3), i => ['a', 'b', 'c'][i])", "total": "sum(map(xs, v => v * p0))"},
            *SLIDERS, {"kind": "text", "text": "和 {fmt(total, 2)}"})
    r = checker.check_block("lab", b)
    assert not r.ok and "NaN" in r.detail, r


def test_an_array_of_numbers_read_by_index_passes_and_computes(checker):  # noqa: F811
    b = lab({"dx": "map(range(3), i => [1, 2, 3][i] - 2)", "pr": "map(range(3), i => dx[i] * dx[i] * p0)", "total": "sum(pr)"},
            SLIDERS[0], {"kind": "text", "text": "和 {fmt(total, 2)}"}, state={"p0": 1})
    r = checker.check_block("lab", b)
    assert r.ok, r
    assert "和 2.00" in r.readings[0], r.readings


def test_the_readings_show_each_trial_and_the_controls_at_their_extremes_together(checker):  # noqa: F811
    b = lab({"p50": "max(0, 1 - p0 - p10)"}, *SLIDERS,
            {"kind": "text", "text": "三个加起来恒等于 1"}, {"kind": "text", "text": "p50 = {fmt(p50, 2)}"})
    r = checker.check_block("lab", b)
    assert r.ok, r
    assert "三个加起来恒等于 1" in r.readings[0] and "p50 = 0.20" in r.readings[0]
    assert any(x.startswith("p0=1：") and "p0 = 1.00" in x and "p50 = 0.00" in x for x in r.readings), r.readings
    together = r.readings[-1]
    assert together.startswith("p0=1，p10=1：") and "p0 = 1.00" in together and "p10 = 1.00" in together, r.readings
    text = check_text(r)
    assert "屏幕读数" in text and together in text and "对照你写的结论和数字" in text


def test_a_reading_that_stayed_or_swapped_still_shows_next_to_the_ones_that_moved(checker):  # noqa: F811
    b = lab({"other": "1 - p0"}, SLIDERS[0], {"kind": "text", "text": "{fmt(p0, 1)}"}, {"kind": "text", "text": "{fmt(other, 1)}"},
            {"kind": "text", "text": "接近垂直方向"}, {"kind": "text", "text": "{p0 > 0.5 ? '大' : '小'}"}, state={"p0": 0.0})
    r = checker.check_block("lab", b.model_copy(update={"probes": (EvoUIProbe(set={"p0": 1}),)}))
    assert r.ok, r
    swapped = next(x for x in r.readings if x.startswith("p0=1："))
    assert "1.0 | 0.0" in swapped and "大" in swapped, r.readings
    assert "接近垂直方向" in r.readings[0] and "接近垂直方向" not in swapped, r.readings


def test_a_static_block_has_no_readings(checker):  # noqa: F811
    r = checker.check_block("lab", lab({}, {"kind": "text", "text": "只有文字"}, state={}))
    assert r.ok and r.readings == (), r
