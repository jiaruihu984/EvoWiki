"""EvoUI v2 Task 6 in headless Chromium (skipped, saying why, without it): chart hover (a number state,
a linked title, tooltip, keyboard), legend toggles, the scroll window, annotations, donut, stacked /
smooth / step shapes, full-width plots and charts in the data palette, and the render check."""

import re

import pytest

from evowiki.deep.evoui import JS_PATH, validate
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.render import PlaywrightChecker

SETUP = """(b) => {
  window.got = {warn: [], progress: []};
  const w = console.warn; console.warn = (...a) => { window.got.warn.push(a.join(' ')); w(...a); };
  window.h = EvoUI.render(b, document.getElementById('m'), {onProgress: x => got.progress.push(x)});
}"""


def blk(body, state=None, derived=None):
    return {"type": "evoui", "title": "图表", "question": "看什么？", "state": state or {}, "derived": derived or {},
            "body": body, "fallback": "f"}


def box(*children):
    return {"kind": "box", "children": list(children)}


PRICE = "map(range(24), i => {'d': (i + 1) + '日', 'p': 150 + i})"
GALLERY = blk(box(
    {"kind": "slider", "bind": "k", "min": 1, "max": 3, "step": 1, "label": "倍数 {k}"},
    {"kind": "chart", "chart": "area", "gradient": True, "curve": "smooth", "data": "visits", "x": "m",
     "series": [{"key": "v", "label": "访问量"}], "caption": "最高 {k * 155} 次"},
    {"kind": "chart", "chart": "bar", "stack": True, "data": "quarters", "x": "q",
     "series": [{"key": "a", "label": "华东"}, {"key": "b", "label": "华南"}, {"key": "c", "label": "西部"}]},
    {"kind": "chart", "chart": "area", "stack": True, "curve": "smooth", "data": "quarters", "x": "q",
     "series": [{"key": "a", "label": "华东"}, {"key": "b", "label": "华南"}]},
    {"kind": "title", "text": "{h < 0 ? '最新 ' + price[23].p : (h + 1) + '日 ' + (150 + h)}"},
    {"kind": "chart", "chart": "line", "curve": "smooth", "hover": "h", "data": "price", "x": "d",
     "series": [{"key": "p", "label": "收盘价"}]},
    {"kind": "chart", "chart": "line", "curve": "step", "data": "price", "x": "d", "series": [{"key": "p", "label": "价"}]},
    {"kind": "chart", "chart": "line", "visible_points": 10, "data": "long", "x": "day",
     "series": [{"key": "a", "label": "甲"}, {"key": "b", "label": "乙"}]},
    {"kind": "chart", "chart": "line", "data": "long", "x": "day", "series": [{"key": "a", "label": "甲"}],
     "annotations": [{"x": 12, "label": "第 12 天", "tone": "danger"}, {"y": 5, "label": "目标 5", "tone": "success"},
                     {"x": "'没有这天'", "label": "无"}]},
    {"kind": "chart", "chart": "pie", "donut": True, "data": "share", "x": "s", "series": [{"key": "v", "label": "占比"}]},
    {"kind": "plot", "xmin": 0, "xmax": 4, "ymin": 0, "ymax": 5, "height": 200, "items": [
        {"shape": "rect", "x": 0.1, "y": 0, "w": 0.8, "h": 3, "tone": "accent", "fill": True, "label": "3"},
        {"shape": "rect", "x": 1.1, "y": 0, "w": 0.8, "h": "k + 1", "tone": "accent", "fill": True, "label": "{k + 1}"},
        {"shape": "rect", "x": 0, "y": 0, "w": 2, "h": 5, "tone": "success", "fill": True, "dashed": True}]},
), state={"h": -1, "k": 1}, derived={
    "visits": "map(range(12), i => {'m': (i + 1) + '月', 'v': k * (100 + i * 5)})",
    "quarters": "[{'q': 'Q1', 'a': 30, 'b': 22, 'c': 12}, {'q': 'Q2', 'a': 36, 'b': 25, 'c': 18}]",
    "price": PRICE,
    "long": "map(range(30), i => {'day': i + 1, 'a': i % 7, 'b': 3})",
    "share": "[{'s': '视频', 'v': 50}, {'s': '文章', 'v': 30}, {'s': '练习', 'v': 20}]",
})
AREA, STACKBAR, STACKAREA, PRICE_CHART, STEP, WINDOW, ANNOT, DONUT, PLOT = range(9)


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")
    with sync_playwright() as pw:
        try:
            br = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium not available ({type(exc).__name__}); uv run playwright install chromium")
        yield br
        br.close()


def mount(browser, block=GALLERY, width=900, scheme="light"):
    assert validate(block) == [], validate(block)
    page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
    page.set_content("<!doctype html><meta charset=utf-8><body style='margin:0;padding:12px'><div id=m></div></body>")
    page.add_style_tag(content=(JS_PATH.parent / "evoui.css").read_text(encoding="utf-8"))
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate(SETUP, block)
    page.wait_for_timeout(50)
    return page


def ev(page, js):
    return page.evaluate(js)


W = "document.querySelectorAll('.evoui-plotwrap')"
# dispatch a pointer event at fraction fx of the chart box's width (synthetic, like a mouse)
POINT = """([k, type, fx]) => { const b = document.querySelectorAll('.evoui-plotwrap')[k].querySelector('.evoui-chartbox'),
  r = b.getBoundingClientRect(); b.dispatchEvent(new PointerEvent(type, {bubbles: true, clientX: r.left + r.width * fx,
  clientY: r.top + r.height / 2, pointerId: 1, buttons: 0})); }"""


def point(page, k, fx, type_="pointermove"):
    page.evaluate(POINT, [k, type_, fx])


def title(page):
    return ev(page, "document.querySelector('.evoui-num').textContent")


def test_hover_sets_the_state_and_a_linked_title_then_minus_one_on_leave(browser):
    page = mount(browser)
    assert title(page) == "最新 173" and ev(page, "h.state.h") == -1
    assert ev(page, f"{W}[{PRICE_CHART}].querySelectorAll('.ov .last').length") == 1     # last-point indicator
    rev0 = ev(page, "h.progress().revision")
    # The hover throttle (at most one state write per 100 ms) is tested on Playwright's fake clock, frozen
    # during the sweep, so a slow machine cannot stretch the sweep across several 100 ms windows.
    page.clock.install()
    page.clock.pause_at(ev(page, "Date.now()") + 1000)
    point(page, PRICE_CHART, 0.02)                                     # the left edge: the first point
    assert ev(page, "h.state.h") == 0 and title(page) == "1日 150"
    tip = ev(page, f"{W}[{PRICE_CHART}].querySelector('.evoui-tip').innerText")
    assert "1日" in tip and "收盘价" in tip and "150" in tip
    assert ev(page, f"{W}[{PRICE_CHART}].querySelectorAll('.ov .last').length") == 0
    assert ev(page, f"{W}[{PRICE_CHART}].querySelectorAll('.ov .hov').length") == 1
    assert ev(page, "h.progress().revision") - rev0 == 1               # leading write at once
    for fx in [i / 40 for i in range(1, 40)]:                          # a sweep within one 100 ms window ...
        point(page, PRICE_CHART, fx)
    assert ev(page, "h.progress().revision") - rev0 == 1               # ... writes nothing more yet
    assert ev(page, "h.state.h") == 0
    page.clock.run_for(99)
    assert ev(page, "h.progress().revision") - rev0 == 1               # still inside the window
    page.clock.run_for(2)                                              # the window ends: one trailing write ...
    assert ev(page, "h.progress().revision") - rev0 == 2
    assert ev(page, "h.state.h") == 23 and title(page) == "24日 173"   # ... of the last point under the pointer
    page.clock.run_for(300)
    assert ev(page, "h.progress().revision") - rev0 == 2               # and nothing after it settles
    page.clock.resume()
    point(page, PRICE_CHART, 0.5, "pointerleave")
    page.wait_for_timeout(150)
    assert ev(page, "h.state.h") == -1 and title(page) == "最新 173"
    assert ev(page, f"{W}[{PRICE_CHART}].querySelector('.evoui-tip').hidden") is True
    page.close()


def test_the_chart_takes_focus_and_arrow_keys_move_the_hovered_point(browser):
    page = mount(browser)
    box_ = page.locator(".evoui-plotwrap").nth(PRICE_CHART).locator(".evoui-chartbox")
    box_.focus()
    page.keyboard.press("ArrowLeft")                                   # from no hover: the last point
    page.wait_for_timeout(120)
    assert ev(page, "h.state.h") == 23
    page.keyboard.press("ArrowLeft")
    page.wait_for_timeout(120)
    assert ev(page, "h.state.h") == 22 and title(page) == "23日 172"
    page.keyboard.press("Home")
    page.wait_for_timeout(120)
    assert ev(page, "h.state.h") == 0
    page.keyboard.press("Escape")
    page.wait_for_timeout(120)
    assert ev(page, "h.state.h") == -1
    page.close()


def test_hover_without_a_state_shows_the_tooltip_and_bar_band(browser):
    page = mount(browser)
    point(page, STACKBAR, 0.9)
    tip = ev(page, f"{W}[{STACKBAR}].querySelector('.evoui-tip').innerText")
    assert "Q2" in tip and "华东" in tip and "36" in tip and "西部" in tip and "18" in tip
    assert ev(page, f"{W}[{STACKBAR}].querySelectorAll('.under .band').length") == 1
    assert ev(page, "h.state.h") == -1
    page.close()


def test_legend_toggles_a_series_and_keeps_one(browser):
    page = mount(browser)
    bars = f"{W}[{STACKBAR}].querySelectorAll('path.bar').length"
    assert ev(page, bars) == 6
    legend = page.locator(".evoui-plotwrap").nth(STACKBAR).locator("button.evoui-lg")
    assert legend.count() == 3
    legend.nth(0).click()
    assert ev(page, bars) == 4 and legend.nth(0).get_attribute("aria-pressed") == "false"
    legend.nth(1).click()
    legend.nth(2).click()                                              # the last shown one stays
    assert ev(page, bars) == 2 and legend.nth(2).get_attribute("aria-pressed") == "true"
    legend.nth(0).click()
    assert ev(page, bars) == 4
    assert ev(page, "JSON.stringify(h.progress()).includes('hidden')") is False      # render-local, never saved
    # one series: a plain legend, no button
    assert ev(page, f"{W}[{PRICE_CHART}].querySelectorAll('.evoui-legend button').length") == 0
    page.close()


XT = "[...document.querySelectorAll('.evoui-plotwrap')[{k}].querySelectorAll('.xticks text')].map(t => t.textContent)"


def test_a_window_shows_the_latest_points_and_scrolls_inside_the_chart(browser):
    page = mount(browser, width=375)
    k = WINDOW
    rng = page.locator(".evoui-plotwrap").nth(k).locator("input[type=range]")
    assert rng.get_attribute("aria-label") == "拖动查看更多" and rng.is_visible()
    assert (rng.get_attribute("max"), rng.input_value()) == ("20", "20")
    assert "30" in ev(page, XT.format(k=k)) and "1" not in ev(page, XT.format(k=k))
    assert ev(page, f"{W}[{k}].querySelectorAll('.ov .last').length") == 2         # the latest point is in view
    rng.fill("0")
    labels = ev(page, XT.format(k=k))
    assert "1" in labels and "30" not in labels
    assert ev(page, f"{W}[{k}].querySelectorAll('.ov .last').length") == 0
    # shift + wheel scrolls right, a horizontal drag scrolls back
    ev(page, f"""(() => {{ const b = {W}[{k}].querySelector('.evoui-chartbox');
      b.dispatchEvent(new WheelEvent('wheel', {{deltaY: 400, shiftKey: true, bubbles: true, cancelable: true}})); }})()""")
    assert int(rng.input_value()) > 0
    before = int(rng.input_value())
    bb = page.locator(".evoui-plotwrap").nth(k).locator(".evoui-chartbox").bounding_box()
    page.mouse.move(bb["x"] + 60, bb["y"] + 60)
    page.mouse.down()
    page.mouse.move(bb["x"] + 260, bb["y"] + 60, steps=8)
    page.mouse.up()
    assert int(rng.input_value()) < before
    # the hovered index is the data index, not the window's
    point(page, k, 0.98)
    assert ev(page, f"{W}[{k}].querySelector('.evoui-tip-x').textContent") == str(int(rng.input_value()) + 10)
    assert ev(page, "document.documentElement.scrollWidth - document.documentElement.clientWidth") == 0
    # a chart without `window`: no scroll row and no range input at all (a host that grabs the block's first
    # range input must get a real control, not a hidden chart scroller)
    assert ev(page, f"{W}[{PRICE_CHART}].querySelector('.evoui-ch-scroll, input[type=range]')") is None
    assert ev(page, "document.querySelectorAll('.evoui-chart input[type=range]').length") == 1
    page.close()


def test_annotations_draw_x_and_y_markers_and_skip_an_unmatched_x(browser):
    page = mount(browser)
    k = ANNOT
    lines = ev(page, f"[...{W}[{k}].querySelectorAll('.annots line')].map(l => l.getAttribute('class'))")
    labels = ev(page, f"[...{W}[{k}].querySelectorAll('.annots text')].map(l => l.textContent)")
    assert sorted(labels) == ["目标 5", "第 12 天"] and len(lines) == 2
    assert any("t-danger" in c for c in lines) and any("t-success" in c for c in lines)
    vline = ev(page, f"(() => {{ const l = [...{W}[{k}].querySelectorAll('.annots line')].find(x => x.getAttribute('x1') === x.getAttribute('x2')); return Number(l.getAttribute('x1')); }})()")
    plot_w = ev(page, f"{W}[{k}].querySelector('svg').viewBox.baseVal.width")
    assert 0.3 * plot_w < vline < 0.45 * plot_w           # the 12th of 30 points
    warns = ev(page, "got.warn")
    assert len([w for w in warns if "matches no data point" in w]) == 1
    page.evaluate("h.set('k', 2)")                                     # re-drawn: warned once only
    assert len([w for w in ev(page, "got.warn") if "matches no data point" in w]) == 1
    page.close()


def test_a_donut_highlights_the_slice_of_the_hovered_legend_item(browser):
    page = mount(browser)
    k = DONUT
    assert ev(page, f"{W}[{k}].querySelectorAll('path.slice').length") == 3
    mid = f"[...{W}[{k}].querySelectorAll('.pie-big, .pie-sub')].map(t => t.textContent)"
    assert ev(page, mid) == ["100", "合计"]
    assert ev(page, f"[...{W}[{k}].querySelectorAll('.evoui-lg')].map(x => [x.getAttribute('role'), x.getAttribute('aria-label'), x.tabIndex])") ==         [["button", "视频 50%", 0], ["button", "文章 30%", 0], ["button", "练习 20%", 0]]
    page.locator(".evoui-plotwrap").nth(k).locator(".evoui-lg").nth(1).hover()
    assert ev(page, f"[...{W}[{k}].querySelectorAll('path.slice')].map(p => p.classList.contains('dim'))") == [True, False, True]
    assert ev(page, mid) == ["30%", "文章"]
    assert "hi" in page.locator(".evoui-plotwrap").nth(k).locator(".evoui-lg").nth(1).get_attribute("class")
    page.mouse.move(0, 0)
    assert ev(page, f"{W}[{k}].querySelectorAll('path.slice.dim').length") == 0
    assert ev(page, mid) == ["100", "合计"]
    page.close()


def _nums(d):
    return [float(x) for x in re.findall(r"-?\d*\.?\d+", d)]


def test_stacked_smooth_and_step_shapes(browser):
    page = mount(browser)
    # stacked bars: the top of Q2 is at 36 + 25 + 18 = 79 and the y axis reaches it
    ticks = ev(page, f"[...{W}[{STACKBAR}].querySelectorAll('.axes text')].map(t => Number(t.textContent.replace(/,/g, '')))")
    assert max(ticks) >= 79
    # stacked areas: the second area's lower edge is the first one's upper edge (2 + 2 paths: 2 areas + 2 lines)
    assert ev(page, f"{W}[{STACKAREA}].querySelectorAll('path.area').length") == 2
    # smooth: cubic segments whose control points never leave their segment's y range (no overshoot)
    d = ev(page, f"{W}[{AREA}].querySelector('path.line').getAttribute('d')")
    assert "C" in d
    pts = re.findall(r"C([-\d.]+),([-\d.]+) ([-\d.]+),([-\d.]+) ([-\d.]+),([-\d.]+)", d)
    start = _nums(d.split("C")[0])
    y0 = start[1]
    for seg in pts:
        c1, c2, y1 = float(seg[1]), float(seg[3]), float(seg[5])
        lo, hi = min(y0, y1) - 0.01, max(y0, y1) + 0.01
        assert lo <= c1 <= hi and lo <= c2 <= hi
        y0 = y1
    # step-after: each segment is horizontal first, then vertical
    d = ev(page, f"{W}[{STEP}].querySelector('path.line').getAttribute('d')")
    nums = _nums(d)
    xy = list(zip(nums[::2], nums[1::2], strict=True))
    for (x0, y0_), (x1, y1_), (x2, y2_) in zip(xy[0::2], xy[1::2], xy[2::2], strict=False):
        assert y1_ == y0_ and x1 == x2
    page.close()


def test_plots_and_charts_fill_the_block_width_in_the_data_palette(browser):
    for width in (375, 1280):
        for scheme in ("light", "dark"):
            page = mount(browser, width=width, scheme=scheme)
            vb = ev(page, f"[...{W}].filter(w => w.dataset.chart && w.dataset.chart !== 'pie').map(w => [w.querySelector('svg.evoui-plot').viewBox.baseVal.width, Math.round(w.getBoundingClientRect().width), Math.round(w.querySelector('svg.evoui-plot').getBoundingClientRect().width)])")
            for vbw, ww, sw in vb:
                assert vbw == ww == sw, (width, vb)                     # a chart fills the block, one viewBox unit = one px
            pv = ev(page, f"(() => {{ const w = {W}[{PLOT}], s = w.querySelector('svg'), r = s.getBoundingClientRect(), wr = w.getBoundingClientRect();"
                          " return [s.viewBox.baseVal.width, s.viewBox.baseVal.height, Math.round(r.width), Math.round(r.height),"
                          " Math.round(wr.width), Math.round(r.left - wr.left), Math.round(wr.right - r.right)]; })()")
            vw, vh, sw, sh, ww, left, right = pv
            assert vw == sw == min(ww, 720) and vh == sh == round(min(560, max(200, 200 * vw / 360))), pv   # a plot keeps 360 : height
            assert abs(left - right) <= 1, pv                           # ... centred
            tick = ev(page, f"getComputedStyle({W}[{AREA}].querySelector('.tick')).fontSize")
            assert tick == "11px"
            line = ev(page, f"getComputedStyle({W}[{PRICE_CHART}].querySelector('path.line')).stroke")
            assert line == ("rgb(47, 111, 223)" if scheme == "light" else "rgb(110, 162, 255)")      # --viz-1, not the purple accent
            bar = ev(page, f"getComputedStyle({W}[{STACKBAR}].querySelectorAll('path.bar')[2]).fill")
            assert bar.startswith("url(")                               # a gradient of the series colour
            rect = ev(page, f"[...{W}[{PLOT}].querySelectorAll('rect.shape')].map(r => [r.classList.contains('solid'), r.style.fill, r.getAttribute('rx')])")
            assert [[a, f.startswith("url(") and f.endswith("currentcolor"), rx] for a, f, rx in rect] == [[True, True, "5"], [True, True, "5"], [False, False, "3"]]
            assert ev(page, f"{W}[{STACKBAR}].querySelector('path.bar').style.fill").endswith("currentcolor")   # a ghost without the gradient keeps a colour
            assert ev(page, "document.documentElement.scrollWidth - document.documentElement.clientWidth") == 0
            page.close()
    page = mount(browser, width=1280)
    page.set_viewport_size({"width": 600, "height": 900})               # a resize re-draws at the new width
    page.wait_for_timeout(150)
    vb = ev(page, f"[{W}[{AREA}].querySelector('svg').viewBox.baseVal.width, Math.round({W}[{AREA}].getBoundingClientRect().width)]")
    assert vb[0] == vb[1] < 600
    page.close()


VECTOR = blk(box({"kind": "plot", "xmin": 0, "xmax": 4, "ymin": 0, "ymax": 3, "items": [
    {"shape": "arrow", "x1": 0, "y1": 0, "x2": 3, "y2": 3, "tone": "accent", "label": "v"},
    {"shape": "circle", "cx": 2, "cy": 1.5, "r": 1, "tone": "warning"}]}))


def test_a_plot_keeps_its_v1_angles_and_round_circles_at_every_width(browser):
    import math
    sx, sy = (360 - 44) / 4, (260 - 44) / 3                            # v1: a fixed 360 x 260 viewBox
    v1 = math.degrees(math.atan2(3 * sy, 3 * sx))
    for width in (375, 1280):
        page = mount(browser, VECTOR, width=width)
        line = ev(page, "(() => { const l = document.querySelector('.evoui-plot line.stroke'); return ['x1','y1','x2','y2'].map(a => Number(l.getAttribute(a))); })()")
        deg = math.degrees(math.atan2(line[1] - line[3], line[2] - line[0]))
        assert abs(deg - v1) < 5, (width, deg, v1)
        c = ev(page, "(() => { const c = document.querySelector('.evoui-plot circle.shape'), s = c.ownerSVGElement.viewBox.baseVal; return [Number(c.getAttribute('r')), s.width, s.height]; })()")
        assert c[0] == pytest.approx((c[1] - 44) / 4)                   # r in x units, as in v1
        assert c[0] < c[2] / 2                                          # ... and it fits the height
        page.close()


def test_a_drag_ending_outside_clears_the_hover_and_a_removed_chart_writes_no_state(browser):
    page = mount(browser, width=900)
    bb = page.locator(".evoui-plotwrap").nth(WINDOW).locator(".evoui-chartbox").bounding_box()
    page.mouse.move(bb["x"] + 300, bb["y"] + 60)
    page.mouse.down()
    page.mouse.move(bb["x"] + 400, bb["y"] + bb["height"] + 200, steps=6)   # dragged out of the box
    page.mouse.up()
    assert ev(page, f"{W}[{WINDOW}].querySelector('.evoui-tip').hidden") is True
    assert ev(page, f"{W}[{WINDOW}].querySelectorAll('.ov .hov').length") == 0
    # a pending hover write after the chart is gone is dropped
    point(page, PRICE_CHART, 0.02)
    assert ev(page, "h.state.h") == 0
    point(page, PRICE_CHART, 0.5)                                      # throttled: pending
    ev(page, f"{W}[{PRICE_CHART}].remove()")
    page.wait_for_timeout(250)
    assert ev(page, "h.state.h") == 0
    page.close()


def test_the_render_check_passes_the_chart_gallery(browser):   # `browser`: skipped without Chromium
    r = PlaywrightChecker().check_block("charts", EvoUIBlock.model_validate(GALLERY))
    assert r.ok, r
    assert (r.probes_total, r.probes_passed, r.overflow_px, r.network_requests) == (1, 1, 0, 0)
