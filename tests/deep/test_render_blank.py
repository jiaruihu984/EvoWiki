"""The render check's big-empty-area rule (`deep.render._BLANK_GAP_JS`): a block or component whose content has a
vertical gap taller than BLANK_MAX_PX with nothing drawn in it (text, images, controls, a canvas with pixels, SVG
shapes other than an axis / grid) fails, and the reason says what to change. In headless Chromium (skipped saying
why without it)."""

import pytest

from evowiki.deep.contracts import InteractiveDraft, Probe
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.render import BLANK_MAX_PX, PlaywrightChecker


@pytest.fixture(scope="module")
def checker():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium not available ({type(exc).__name__}); "
                    "uv sync --extra deep && uv run playwright install chromium")
    return PlaywrightChecker()


HEAD = {"type": "evoui", "title": "改节点或边，看 N 和 L", "question": "加一条边，N 和 L 怎么变？",
        "fallback": "加一条边 L 加一，N 不变。"}


def plot_block(items: list) -> EvoUIBlock:
    return EvoUIBlock.model_validate({**HEAD, "state": {"k": 0}, "derived": {}, "body": {"kind": "box", "children": [
        {"kind": "slider", "bind": "k", "min": 0, "max": 4, "step": 1, "label": "第 {k} 步"},
        {"kind": "plot", "xmin": 0, "xmax": 10, "ymin": 0, "ymax": 10, "height": 420, "items": items},
        {"kind": "text", "text": "现在是第 {k} 步"}]}})


def test_a_block_with_a_big_empty_plot_fails_and_says_why(checker):
    # the user's case: a slider, then a big plot area with nothing drawn in it (an axis is not content)
    r = checker.check_block("blank", plot_block([]))
    assert not r.ok and "空白" in r.detail, r


def test_a_plot_with_a_line_through_it_passes(checker):
    r = checker.check_block("line", plot_block([{"shape": "polyline", "points": "[[0, 0], [5, 5 + k], [10, 10]]"}]))
    assert r.ok, r


def test_a_filled_template_passes(checker):
    from evowiki.deep.evoui.typed import compile_block
    from tests.deep.test_evoui_typed_templates import spec
    r = checker.check_block("lab", EvoUIBlock.model_validate(compile_block(spec("lab"))))
    assert r.ok, r


def test_a_component_with_an_empty_stage_fails(checker):
    html = ("<!doctype html><html><body><main><h1>空的舞台</h1><div style='height:420px;background:#f4f4f5'></div>"
            "<button id='b' onclick=\"document.getElementById('r').textContent='点了'\">点</button>"
            "<p id='r'>没点</p></main></body></html>")
    d = InteractiveDraft(interactive_id="empty-stage", section_id="s1", title="空的舞台", learning_question="看什么？",
                         html=html, height=600, probes=(Probe(target="#b", action="click", expect_change="#r"),))
    r = checker.check(d)
    assert not r.ok and "空白" in r.detail and BLANK_MAX_PX == 200, r
