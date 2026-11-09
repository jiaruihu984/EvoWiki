"""The render check in a real headless Chromium (optional extra `deep`). Skipped, saying why, when
Playwright or its Chromium is not installed: `uv sync --extra deep` then `uv run playwright install
chromium`."""

import pytest

from evowiki.deep.contracts import InteractiveDraft, Probe
from evowiki.deep.render import PlaywrightChecker, check_with_browser


def _chromium():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")
    pw = sync_playwright().start()
    try:
        return pw, pw.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - e.g. the browser binary is missing
        pw.stop()
        pytest.skip(f"chromium not available ({type(exc).__name__}); "
                    "run `uv run playwright install chromium`")


@pytest.fixture(scope="module")
def browser():
    pw, b = _chromium()
    yield b
    b.close()
    pw.stop()


GOOD = """<!doctype html><html><head><meta charset="utf-8"><style>
:root{color-scheme:light dark}body{margin:0;font:16px system-ui}main{max-width:100%;padding:8px}
</style></head><body><main>
<button id="add">加一</button>
<select id="pick"><option value="a">A</option><option value="t">T</option></select>
<input id="seq" value="AT">
<p id="out">0</p></main><script>
let n = 0;
add.onclick = () => { out.textContent = "点了 " + (++n) + " 次"; };
pick.onchange = () => { out.textContent = "选了 " + pick.value; };
seq.oninput = () => { out.textContent = "输入 " + seq.value; };
</script></body></html>"""

BAD = """<!doctype html><meta charset="utf-8"><div style="width:900px">太宽</div>
<img src="https://example.com/x.png"><button id="b">坏按钮</button><p id="out">0</p>
<script>console.error("boom"); b.onclick = () => { missing(); };</script>"""


def draft(html: str, *probes: Probe) -> InteractiveDraft:
    return InteractiveDraft(interactive_id="demo", section_id="s1", title="t",
                            learning_question="q", html=html, height=480, probes=probes)


def test_working_interactive_passes_at_both_widths(browser):
    c = check_with_browser(browser, draft(
        GOOD, Probe(target="#add", action="click", expect_change="#out"),
        Probe(target="#pick", action="select", value="t", expect_change="#out"),
        Probe(target="#seq", action="input", value="GC", expect_change="#out")))
    assert c.ok, c
    assert (c.probes_passed, c.probes_total, c.network_requests, c.overflow_px) == (3, 3, 0, 0)


def test_broken_interactive_fails_with_every_reason(browser):
    c = check_with_browser(browser, draft(
        BAD, Probe(target="#b", action="click", expect_change="#out"),
        Probe(target="#nope", action="click", expect_change="#out")))
    assert not c.ok
    assert c.network_requests >= 1 and "example.com" in c.detail      # counted and aborted
    assert "boom" in c.console_errors
    assert any("missing" in e for e in c.page_errors)                  # uncaught exception
    assert c.overflow_px > 300                                         # 900px div at 375px
    assert (c.probes_passed, c.probes_total) == (0, 2)
    assert "找不到控件 #nope" in c.detail


def test_checker_launches_its_own_browser(browser):
    # `browser` only skips when Chromium is missing; the checker starts its own on its own thread
    c = PlaywrightChecker().check(draft(GOOD, Probe(target="#add", action="click",
                                                    expect_change="#out")))
    assert c.ok and c.probes_passed == 1
