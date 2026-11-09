"""A timer in a narrow cell (two-column grid at 375 px) never clips its readout, in any state; and the
render check fails a block whose text is clipped. Headless Chromium (skipped, saying why, without it)."""

import pytest

from evowiki.deep.evoui import harness_html
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.render import _CLIPPED_JS, check_block_with_browser

BLOCK = {"type": "evoui", "title": "两个计时器并排", "question": "窄格子里的计时器读数放得下吗？",
         "state": {"sw": 0}, "derived": {},
         "body": {"kind": "grid", "columns": 2, "gap": 3, "children": [
             {"kind": "grid-item", "tone": "surface", "children": [
                 {"kind": "timer", "id": "down", "mode": "countdown", "seconds": 600, "label": "倒计时 10 分钟"}]},
             {"kind": "grid-item", "tone": "surface", "children": [
                 {"kind": "timer", "id": "sw", "mode": "stopwatch", "bind": "sw", "label": "秒表"},
                 {"kind": "text", "text": "已过 {sw} 秒"}]}]},
         "fallback": "倒计时 10 分钟和一个秒表。"}

# a deliberately clipped text (inline style is not reachable from a block, so the page adds it)
CLIP_CSS = ".evoui-title{overflow:hidden;white-space:nowrap;width:60px}"


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium not available ({type(exc).__name__}); uv run playwright install chromium")
        yield b
        b.close()


def _page(browser, width=375):
    page = browser.new_page(viewport={"width": width, "height": 800})
    page.clock.install()
    page.set_content(harness_html(BLOCK))
    return page


def _clipped(page):
    return page.evaluate(_CLIPPED_JS)


def test_narrow_timer_readout_is_not_clipped_in_any_state(browser):
    page = _page(browser)
    assert page.evaluate("document.querySelectorAll('.evoui-tm').length") == 2
    width = page.evaluate("document.querySelector('.evoui-tm').getBoundingClientRect().width")
    assert width < 170                                          # really a narrow cell
    assert _clipped(page) == []                                 # idle
    for sel in (".evoui-tm[data-mode=countdown] [data-f=start]", ".evoui-tm[data-mode=stopwatch] [data-f=start]"):
        page.locator(sel).click()
    page.clock.run_for(65_000)
    assert page.locator(".evoui-tm[data-mode=countdown] .evoui-tm-of").inner_text() == "/ 10:00"
    assert _clipped(page) == []                                 # running: 8:55 / 10:00, 1:05.0
    page.locator(".evoui-tm[data-mode=countdown] [data-f=start]").click()
    page.clock.run_for(1_000)
    assert _clipped(page) == []                                 # paused
    page.locator(".evoui-tm[data-mode=countdown] [data-f=start]").click()
    page.clock.run_for(600_000)
    assert page.locator(".evoui-tm[data-mode=countdown]").get_attribute("data-state") == "done"
    assert _clipped(page) == []                                 # 时间到
    for tm in page.locator(".evoui-tm").all():                  # nothing inside sticks out of the card
        assert tm.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    page.close()


def test_render_check_passes_the_narrow_timer_block(browser):
    r = check_block_with_browser(browser, "t", EvoUIBlock.model_validate(BLOCK))
    assert r.ok, r.detail


def test_clip_detector_reports_clipped_text_and_skips_screen_reader_text(browser):
    page = _page(browser)
    assert _clipped(page) == []                                 # .evoui-sr live regions are 1 px and clipped by design
    page.add_style_tag(content=CLIP_CSS)
    out = _clipped(page)
    assert len(out) == 1 and out[0].startswith(".evoui-title：两个计时器")
    page.close()


def test_clip_detector_skips_deliberate_ellipsis_truncation(browser):
    page = _page(browser)
    page.add_style_tag(content=CLIP_CSS.replace("}", ";text-overflow:ellipsis}"))
    assert _clipped(page) == []                                 # 「两个计…」 is meant to be cut
    page.close()
