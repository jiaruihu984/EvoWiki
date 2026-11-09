"""EvoUI v2 Task 5a in headless Chromium (skipped, saying why, without it): reveal (click / steps /
after a practice node), the motion layer (default `if` transition, transition, animate, plot / chart
glide, table FLIP), celebrate, timer and player — clicks, keys, progress restore, events, reduced motion.
Timers and players run on Playwright's fake clock (`page.clock`), so their timing is exact."""

import copy
import itertools

import pytest

from evowiki.deep.evoui import JS_PATH, validate
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.render import block_probes
from evowiki.wikiapp.wiki_schemas import BlockEventIn

SETUP = """([b, p]) => {
  window.got = {events: [], warn: [], progress: []};
  const w = console.warn; console.warn = (...a) => { window.got.warn.push(a.join(' ')); w(...a); };
  const opts = {blockId: 'blk', onEvent: e => got.events.push(e), onProgress: x => got.progress.push(x)};
  if (p) opts.progress = p;
  window.h = EvoUI.render(b, document.getElementById('m'), opts);
}"""

# the block's text as a reader sees it: leaving ghosts excluded (deep/render.py does the same)
VISIBLE = """() => { const root = document.querySelector('#m .evoui'), gone = [...root.querySelectorAll('[data-leaving]')];
  gone.forEach(e => { e.style.display = 'none'; }); const t = root.innerText; gone.forEach(e => { e.style.display = ''; }); return t; }"""


def blk(body, state=None, derived=None):
    return {"type": "evoui", "title": "动效", "question": "怎么动？", "state": state or {}, "derived": derived or {},
            "body": body, "fallback": "f"}


def box(*children):
    return {"kind": "box", "children": list(children)}


def text(t):
    return {"kind": "text", "text": t}


QUIZ = {"kind": "quiz", "id": "chk", "questions": [{"q": "1+1？", "options": ["1", "2"], "correct": 1, "why": "就是 2"}]}


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


def mount(browser, block, progress=None, reduced=False, clock=False, width=420):
    assert validate(block) == [], validate(block)
    page = browser.new_page(reduced_motion="reduce" if reduced else "no-preference",
                            viewport={"width": width, "height": 900})
    if clock:
        page.clock.install()
    page.set_content("<!doctype html><meta charset=utf-8><body><div id=m></div></body>")
    page.add_style_tag(content=(JS_PATH.parent / "evoui.css").read_text(encoding="utf-8"))
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate(SETUP, [block, progress])
    return page


def f(page, name):
    return page.locator(f"[data-f='{name}']")


def vis(page):
    return page.evaluate(VISIBLE)


def ev(page, js):
    return page.evaluate(js)


# ---------------------------------------------------------------- reveal

REVEAL_CLICK = blk(box({"kind": "reveal", "label": "看答案", "children": [text("答案甲"), text("答案乙")]}))


def test_reveal_click_shows_everything_at_once_and_hides_again(browser):
    page = mount(browser, REVEAL_CLICK)
    assert "答案甲" not in vis(page) and "看答案" in vis(page)
    assert ev(page, "h.progress().nodes['reveal#0']") == {"shown": 0}
    rev0 = ev(page, "h.progress().revision")
    f(page, "reveal").click()
    now = ev(page, """() => ({steps: [...document.querySelectorAll('.evoui-rv-step')].map(s => [s.hidden, s.classList.contains('evoui-enter'), s.style.animationDelay]),
      ghost: !!document.querySelector('.evoui-rv-mask[data-leaving]'), mask: document.querySelector('.evoui-rv-mask:not([data-leaving])').hidden,
      focus: document.activeElement.dataset.step || null, live: document.querySelector('.evoui-rv .evoui-sr').textContent})""")
    assert now["steps"] == [[False, True, ""], [False, True, "50ms"]]
    assert now["ghost"] and now["mask"] and now["focus"] == "1" and now["live"] == "已全部显示"
    assert "答案甲" in vis(page) and "答案乙" in vis(page) and "看答案" not in vis(page)
    page.wait_for_timeout(300)
    assert ev(page, "document.querySelectorAll('[data-leaving]').length") == 0
    assert ev(page, "document.querySelector('.evoui-rv-step').classList.contains('evoui-enter')") is False
    p = ev(page, "h.progress()")
    assert p["nodes"]["reveal#0"] == {"shown": 2} and p["revision"] == rev0 + 1
    f(page, "hide").click()
    assert "答案甲" not in vis(page) and ev(page, "h.progress().nodes['reveal#0']") == {"shown": 0}
    assert ev(page, "document.activeElement.dataset.f") == "reveal"
    assert ev(page, "got.events") == []                          # a reveal sends no result events
    page.close()


def test_reveal_under_reduced_motion_shows_without_motion(browser):
    page = mount(browser, REVEAL_CLICK, reduced=True)
    f(page, "reveal").click()
    assert "答案甲" in vis(page)
    assert ev(page, "document.querySelectorAll('[data-leaving],.evoui-enter').length") == 0
    page.close()


def test_reveal_steps_one_child_per_press_with_the_placeholder_sliding(browser):
    block = blk(box({"kind": "reveal", "id": "why", "mode": "steps", "children": [text("第一步"), text("第二步"), text("第三步")]}))
    page = mount(browser, block)
    assert f(page, "reveal").inner_text() == "下一步 (1/3)"
    f(page, "reveal").click()
    out = ev(page, """() => ({moving: document.querySelector('.evoui-rv-mask').getAnimations().length,
      label: document.querySelector('[data-f=reveal]').innerText})""")
    assert out == {"moving": 1, "label": "下一步 (2/3)"}                  # the 200 ms layout move
    assert "第一步" in vis(page) and "第二步" not in vis(page)
    f(page, "reveal").click()
    f(page, "reveal").click()
    assert all(s in vis(page) for s in ("第一步", "第二步", "第三步"))
    assert ev(page, "h.progress().nodes.why") == {"shown": 3}
    assert ev(page, "[...document.querySelectorAll('.evoui-rv-step')].map(s => getComputedStyle(s, '::before').content)") == [
        '"1"', '"2"', '"3"']
    page.close()
    labelled = copy.deepcopy(block)
    labelled["body"]["children"][0]["label"] = "再看一步"
    page = mount(browser, labelled)
    assert f(page, "reveal").inner_text() == "再看一步 (1/3)"
    page.close()


def test_a_restored_reveal_is_shown_without_motion_and_a_misfit_is_discarded(browser):
    block = blk(box({"kind": "reveal", "id": "why", "mode": "steps", "children": [text("第一步"), text("第二步")]}))
    page = mount(browser, block, progress={"v": 2, "revision": 7, "state": {}, "nodes": {"why": {"shown": 1}}})
    out = ev(page, """() => ({enter: document.querySelectorAll('.evoui-enter').length, label: document.querySelector('[data-f=reveal]').innerText,
      rev: h.progress().revision, warn: got.warn, events: got.events, progress: got.progress})""")
    assert out == {"enter": 0, "label": "下一步 (2/2)", "rev": 7, "warn": [], "events": [], "progress": []}
    assert "第一步" in vis(page) and "第二步" not in vis(page)
    page.close()
    page = mount(browser, REVEAL_CLICK, progress={"v": 2, "revision": 1, "state": {}, "nodes": {"reveal#0": {"shown": 1}}})
    assert ev(page, "got.warn.length") == 1 and ev(page, "h.progress().nodes['reveal#0']") == {"shown": 0}  # click: 0 or all
    page.close()


AFTER = blk(box(QUIZ, {"kind": "reveal", "mode": "after", "after": "chk", "children": [text("解释：1+1=2")]}))


def test_reveal_after_opens_when_the_quiz_is_done_and_on_restore_at_once(browser):
    page = mount(browser, AFTER)
    assert "完成上面的练习后显示" in vis(page) and "解释" not in vis(page)
    assert f(page, "reveal").count() == 0                              # nothing to press: it waits
    page.locator("input[type=radio]").nth(1).check()
    page.locator(".evoui-check").click()
    out = ev(page, """() => ({enter: document.querySelector('.evoui-rv-step').classList.contains('evoui-enter'),
      ghost: !!document.querySelector('.evoui-rv-mask[data-leaving]'), kinds: got.events.map(e => e.type)})""")
    assert out == {"enter": True, "ghost": True, "kinds": ["item", "summary"]}
    assert "解释：1+1=2" in vis(page) and ev(page, "h.progress().nodes['reveal#0']") == {"shown": 1}
    saved = ev(page, "h.flush()")
    page.close()
    # restored: shown at once, no motion, nothing sent
    page = mount(browser, AFTER, progress=saved)
    assert "解释：1+1=2" in vis(page)
    assert ev(page, "({e: document.querySelectorAll('.evoui-enter,[data-leaving]').length, ev: got.events.length, p: got.progress.length})") == {
        "e": 0, "ev": 0, "p": 0}
    page.close()
    # the reveal's own progress lost, the quiz done (restored) — and the quiz comes after the reveal
    flipped = copy.deepcopy(AFTER)
    flipped["body"]["children"].reverse()
    lost = {**saved, "nodes": {"chk": saved["nodes"]["chk"]}}
    page = mount(browser, flipped, progress=lost)
    assert "解释：1+1=2" in vis(page) and ev(page, "document.querySelectorAll('.evoui-enter').length") == 0
    page.close()


def test_reveal_after_flashcards_and_inside_an_if_branch(browser):
    cards = {"kind": "flashcards", "id": "fc", "shuffle": False, "cards": [{"front": "σ", "back": "标准差"}, {"front": "μ", "back": "均值"}]}
    block = blk(box({"kind": "toggle", "bind": "on", "label": "显示"},
                    {"kind": "if", "cond": "on", "then": {"kind": "reveal", "mode": "after", "after": "fc", "label": "记完再看",
                                                          "children": [text("都记住了吗")]}, "else": None},
                    cards), state={"on": False})
    page = mount(browser, block)
    page.locator(".evoui-toggle input").check()
    assert "记完再看" in vis(page)
    page.locator(".evoui-fc-stage").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    assert "都记住了吗" in vis(page)
    page.locator(".evoui-toggle input").uncheck()
    page.locator(".evoui-toggle input").check()                    # rebuilt branch: target already done
    assert "都记住了吗" in vis(page)
    page.close()


# ---------------------------------------------------------------- motion: if / transition / animate

SWITCH = blk(box({"kind": "toggle", "bind": "on", "label": "开关"},
                 {"kind": "if", "cond": "on", "then": box(text("现在开着"), QUIZ), "else": text("现在关着")}),
             state={"on": True})


def test_an_if_switch_fades_the_old_branch_out_and_the_new_one_in(browser):
    page = mount(browser, SWITCH)
    assert ev(page, "document.querySelectorAll('[data-leaving],.evoui-enter').length") == 0    # first render: still
    page.locator("input[type=radio]").nth(1).check()
    page.locator(".evoui-toggle input").uncheck()
    out = ev(page, """() => { const g = document.querySelector('[data-leaving]'), cs = g && getComputedStyle(g);
      const fresh = [...document.querySelectorAll('.evoui-slot > *')].find(x => !x.hasAttribute('data-leaving'));
      return {ghost: !!g, hidden: g && g.getAttribute('aria-hidden'), inert: g && g.inert, pe: cs && cs.pointerEvents, pos: cs && cs.position,
        names: g && [...g.querySelectorAll('input')].filter(i => i.name).length, text: fresh.innerText,
        enter: fresh.classList.contains('evoui-enter'), delay: fresh.style.animationDelay}; }""")
    assert out == {"ghost": True, "hidden": "true", "inert": True, "pe": "none", "pos": "absolute", "names": 0,
                   "text": "现在关着", "enter": True, "delay": "60ms"}
    assert "现在关着" in vis(page) and "现在开着" not in vis(page)
    page.wait_for_timeout(250)
    assert ev(page, "document.querySelectorAll('[data-leaving]').length") == 0
    page.locator(".evoui-toggle input").check()                      # back: the quiz keeps its pick
    assert ev(page, "[...document.querySelectorAll('input[type=radio]')].filter(r => !r.closest('[data-leaving]')).map(r => r.checked)") == [
        False, True]
    page.close()


def test_reduced_motion_swaps_instantly(browser):
    page = mount(browser, SWITCH, reduced=True)
    page.locator(".evoui-toggle input").uncheck()
    assert ev(page, "document.querySelectorAll('[data-leaving],.evoui-enter,.evoui-fadein').length") == 0
    assert page.locator(".evoui").inner_text().count("现在开着") == 0
    page.close()


TRANS = blk(box({"kind": "choice", "bind": "mode", "options": [{"label": "甲", "value": "a"}, {"label": "乙", "value": "b"}]},
                {"kind": "slider", "bind": "x", "min": 0, "max": 9, "step": 1},
                {"kind": "transition", "key": "mode", "children": [text("模式 {mode}，x = {x}")]}),
            state={"mode": "a", "x": 1})


def test_transition_crossfades_when_its_key_changes_only(browser):
    page = mount(browser, TRANS)
    page.evaluate("h.set('x', 2)")                                     # not the key: no transition
    assert ev(page, "document.querySelectorAll('[data-leaving],.evoui-enter').length") == 0
    page.locator(".evoui-choice button").nth(1).click()
    out = ev(page, """() => ({ghost: document.querySelector('.evoui-tr > [data-leaving]').innerText,
      inner: document.querySelector('.evoui-tr-inner').innerText, enter: document.querySelector('.evoui-tr-inner').classList.contains('evoui-enter')})""")
    assert out == {"ghost": "模式 a，x = 2", "inner": "模式 b，x = 2", "enter": True}
    assert "模式 b" in vis(page) and "模式 a" not in vis(page)
    page.wait_for_timeout(300)
    assert ev(page, "document.querySelectorAll('[data-leaving]').length") == 0
    page.close()
    page = mount(browser, TRANS, reduced=True)
    page.locator(".evoui-choice button").nth(1).click()
    assert ev(page, "document.querySelectorAll('[data-leaving],.evoui-enter').length") == 0
    page.close()


def test_animate_plays_its_enter_when_it_appears(browser):
    block = blk(box({"kind": "toggle", "bind": "on"},
                    {"kind": "animate", "children": [text("一"), text("二")]},
                    {"kind": "if", "cond": "on", "then": {"kind": "animate", "effect": "fade", "children": [text("三")]}, "else": None}),
                state={"on": False})
    page = mount(browser, block)
    first = ev(page, "[...document.querySelectorAll('.evoui-animate > *')].map(x => [x.className.includes('evoui-enter'), x.style.animationDelay])")
    assert first == [[True, ""], [True, "50ms"]]
    page.locator(".evoui-toggle input").check()
    assert ev(page, "[...document.querySelectorAll('.evoui-animate')][1].firstChild.classList.contains('evoui-fadein')") is True
    page.wait_for_timeout(400)
    assert ev(page, "document.querySelectorAll('.evoui-enter,.evoui-fadein').length") == 0     # cleaned up after playing
    page.close()
    page = mount(browser, block, reduced=True)
    assert ev(page, "document.querySelectorAll('.evoui-enter,.evoui-fadein').length") == 0
    page.close()


# ---------------------------------------------------------------- plot / chart glide, table FLIP

GLIDE = blk(box({"kind": "slider", "bind": "x", "min": 0, "max": 10, "step": 1},
                {"kind": "plot", "xmin": 0, "xmax": 10, "ymin": 0, "ymax": 10,
                 "items": [{"shape": "point", "x": "x", "y": 5, "label": "P {x}"},
                           {"shape": "arrow", "x1": 0, "y1": 0, "x2": "x", "y2": 5}]},
                {"kind": "table", "columns": ["名", "值"], "rows": "sort_rows"}),
            state={"x": 0}, derived={"sort_rows": "x < 5 ? [['甲', 1], ['乙', 2], ['丙', 3]] : [['丙', 3], ['甲', 1], ['乙', 2]]"})

PLOT_STATE = """() => { const c = document.querySelector('.evoui-plot circle'), l = document.querySelector('.evoui-plot line.stroke');
  return {cx: Number(c.getAttribute('cx')), x2: Number(l.getAttribute('x2')), label: document.querySelector('.evoui-plot text').textContent}; }"""


def test_a_redrawn_plot_glides_and_resorted_rows_slide(browser):
    page = mount(browser, GLIDE)
    start = ev(page, PLOT_STATE)
    mid = ev(page, "() => { h.set('x', 10); return (" + PLOT_STATE + ")(); }")
    assert mid["label"] == "P 10"                                     # text at once
    assert mid["cx"] == pytest.approx(start["cx"], abs=0.5)           # geometry starts where it was
    soon = ev(page, "new Promise(r => setTimeout(() => r((" + PLOT_STATE + ")()), 80))")
    assert start["cx"] + 5 < soon["cx"]                                # ... and is on its way
    moved = ev(page, "[...document.querySelectorAll('.evoui-table tbody tr')].map(r => r.getAnimations().length)")
    assert moved == [1, 1, 1]                                          # every row moved: each slides
    page.wait_for_timeout(350)
    end = ev(page, PLOT_STATE)
    assert end["cx"] > start["cx"] + 100 and end["x2"] == pytest.approx(end["cx"], abs=0.01)
    page.close()
    page = mount(browser, GLIDE, reduced=True)
    page.evaluate("h.set('x', 10)")
    assert ev(page, PLOT_STATE)["cx"] > start["cx"] + 100              # no glide: final at once
    assert ev(page, "[...document.querySelectorAll('.evoui-table tbody tr')].map(r => r.getAnimations().length)") == [0, 0, 0]
    page.close()


# ---------------------------------------------------------------- celebrate

CHEER = blk(box({"kind": "slider", "bind": "x", "min": 0, "max": 9, "step": 1}, {"kind": "celebrate", "when": "x >= 5"}),
            state={"x": 5})
CONFETTI = "document.querySelectorAll('canvas.evoui-confetti').length"


def test_celebrate_fires_on_false_to_true_only(browser):
    page = mount(browser, CHEER)
    assert ev(page, CONFETTI) == 0                                    # true on first render: no confetti
    page.evaluate("h.set('x', 0)")
    assert ev(page, CONFETTI) == 0
    page.evaluate("h.set('x', 6)")
    assert ev(page, CONFETTI) == 1
    page.evaluate("h.set('x', 7)")                                    # still true: nothing new
    assert ev(page, CONFETTI) == 1
    page.evaluate("h.set('x', 1); h.set('x', 9)")                     # re-armed
    assert ev(page, CONFETTI) == 2
    assert ev(page, "got.events") == []
    page.close()
    page = mount(browser, CHEER, progress={"v": 2, "revision": 3, "state": {"x": 8}, "nodes": {}})
    assert ev(page, CONFETTI) == 0                                    # restore: never
    page.close()
    page = mount(browser, CHEER, reduced=True)
    page.evaluate("h.set('x', 0); h.set('x', 6)")
    assert ev(page, CONFETTI) == 0
    page.close()


# ---------------------------------------------------------------- timer (fake clock)

COUNTDOWN = blk(box({"kind": "timer", "id": "clock", "mode": "countdown", "seconds": 3, "bind": "secs", "label": "限时 {secs}"},
                    text("已过 {secs} 秒")), state={"secs": 0})
TIMER = """() => { const t = document.querySelector('.evoui-tm');
  return {state: t.dataset.state, digits: t.querySelector('.evoui-tm-digits').textContent, frac: t.querySelector('.evoui-tm-frac').textContent,
    of: t.querySelector('.evoui-tm-of').textContent, go: t.querySelector('[data-f=start]').hidden ? null : t.querySelector('[data-f=start]').getAttribute('aria-label'),
    secs: h.state.secs, node: h.progress().nodes.clock}; }"""


def test_a_countdown_runs_pauses_finishes_and_resets(browser):
    page = mount(browser, COUNTDOWN, clock=True)
    assert ev(page, TIMER)["digits"] == "0:03" and ev(page, TIMER)["state"] == "idle"
    rev0 = ev(page, "h.progress().revision")
    f(page, "start").click()
    page.clock.run_for(1050)
    t = ev(page, TIMER)
    assert (t["state"], t["digits"], t["of"], t["go"], t["secs"]) == ("running", "0:02", "/ 0:03", "暂停", 1)
    assert "已过 1 秒" in vis(page) and "限时 1" in vis(page)
    assert ev(page, "h.progress().revision") == rev0 + 2                 # start + one whole second
    f(page, "start").click()                                             # pause
    paused = ev(page, TIMER)
    page.clock.run_for(5000)
    assert ev(page, TIMER) == paused and paused["state"] == "paused" and paused["go"] == "继续"
    assert paused["node"]["running"] is False and 1000 <= paused["node"]["elapsedMs"] <= 1200
    f(page, "start").click()
    page.clock.run_for(2500)
    t = ev(page, TIMER)
    assert (t["state"], t["digits"], t["go"], t["secs"]) == ("done", "时间到", None, 3)
    assert t["node"] == {"elapsedMs": 3000, "running": False, "done": True}
    assert page.locator(".evoui-tm .evoui-sr").inner_text() == "时间到"
    f(page, "reset").click()
    t = ev(page, TIMER)
    assert (t["state"], t["digits"], t["secs"], t["node"]) == ("idle", "0:03", 0, {"elapsedMs": 0, "running": False, "done": False})
    assert ev(page, "got.events") == []
    page.close()


def test_a_bound_timer_changes_state_at_most_once_a_second(browser):
    page = mount(browser, COUNTDOWN, clock=True)
    f(page, "start").click()
    page.clock.run_for(300)
    n0 = ev(page, "[h.progress().revision, got.progress.length]")
    page.clock.run_for(1900)                                             # 0.3 s -> 2.2 s: seconds 1 and 2
    n1 = ev(page, "[h.progress().revision, got.progress.length]")
    assert n1[0] - n0[0] == 2 and n1[1] - n0[1] <= 2
    page.close()


def test_a_stopwatch_shows_tenths_and_autostarts(browser):
    block = blk(box({"kind": "timer", "mode": "stopwatch", "autostart": True}))
    page = mount(browser, block, clock=True)
    page.clock.run_for(1250)
    t = ev(page, "() => { const t = document.querySelector('.evoui-tm'); return [t.dataset.state, t.querySelector('.evoui-tm-digits').textContent, t.querySelector('.evoui-tm-frac').textContent]; }")
    assert t == ["running", "0:01", ".2"]
    assert ev(page, "h.progress().nodes['timer#0'].running") is True
    page.close()


def test_a_restored_timer_is_paused_and_a_misfit_is_discarded(browser):
    block = copy.deepcopy(COUNTDOWN)
    block["body"]["children"][0]["autostart"] = True
    page = mount(browser, block, clock=True, progress={"v": 2, "revision": 4, "state": {"secs": 1},
                                                       "nodes": {"clock": {"elapsedMs": 1500, "running": True, "done": False}}})
    page.clock.run_for(3000)
    t = ev(page, TIMER)
    assert (t["state"], t["digits"], t["secs"]) == ("paused", "0:02", 1)   # never runs on by itself
    assert ev(page, "[got.events.length, got.progress.length, got.warn.length]") == [0, 0, 0]
    page.close()
    page = mount(browser, COUNTDOWN, clock=True, progress={"v": 2, "revision": 4, "state": {"secs": 0},
                                                           "nodes": {"clock": {"elapsedMs": 9000, "running": False, "done": False}}})
    assert ev(page, "got.warn.length") == 1 and ev(page, TIMER)["node"]["elapsedMs"] == 0
    page.close()


def test_summaries_finished_while_a_timer_runs_carry_timer_ms(browser):
    block = blk(box({"kind": "timer", "id": "clock", "mode": "stopwatch"}, QUIZ))
    page = mount(browser, block, clock=True)
    f(page, "start").click()
    page.clock.run_for(2000)
    page.locator("input[type=radio]").nth(0).check()
    page.locator(".evoui-check").click()
    f(page, "start").click()                                             # pause
    page.locator("input[type=radio]").nth(1).check()
    page.locator(".evoui-check").click()
    evs = ev(page, "got.events")
    items = [e for e in evs if e["type"] == "item"]
    sums = [e for e in evs if e["type"] == "summary"]
    assert all("timerMs" not in e for e in items)
    assert 1900 <= sums[0]["timerMs"] <= 2300 and "timerMs" not in sums[1]
    for e in evs:
        BlockEventIn.model_validate(e)
    assert BlockEventIn.model_validate(sums[0]).timer_ms == sums[0]["timerMs"]
    page.close()


def test_a_removed_timer_or_player_stops_ticking(browser):
    block = blk(box({"kind": "timer", "mode": "stopwatch", "bind": "secs", "autostart": True},
                    {"kind": "player", "bind": "k", "min": 0, "max": 100, "step": 1, "interval_ms": 100}),
                state={"secs": 0, "k": 0})
    page = mount(browser, block, clock=True)
    f(page, "play").click()
    page.clock.run_for(1100)
    before = ev(page, "[h.state.secs, h.state.k]")
    assert before[0] == 1 and before[1] >= 10
    page.evaluate("document.getElementById('m').replaceChildren()")
    page.clock.run_for(3000)
    assert ev(page, "[h.state.secs, h.state.k]") == before
    page.close()


# ---------------------------------------------------------------- player (fake clock)

PLAYER = blk(box({"kind": "player", "id": "run", "bind": "k", "min": 0, "max": 4, "step": 1, "interval_ms": 500, "label": "第 {k} 步"},
                 text("k = {k}")), state={"k": 0})
PL = """() => { const p = document.querySelector('.evoui-pl'); return {k: h.state.k, playing: p.dataset.playing,
  play: p.querySelector('[data-f=play]').getAttribute('aria-label'), count: p.querySelector('.evoui-pl-count').textContent,
  back: p.querySelector('[data-f=back]').disabled, fwd: p.querySelector('[data-f=fwd]').disabled, node: h.progress().nodes.run,
  speed: [...p.querySelectorAll('.evoui-pl-speed button')].map(b => b.getAttribute('aria-pressed') === 'true')}; }"""


def test_the_player_steps_on_its_interval_and_stops_at_max(browser):
    page = mount(browser, PLAYER, clock=True)
    p = ev(page, PL)
    assert (p["k"], p["playing"], p["play"], p["count"], p["back"], p["fwd"], p["speed"]) == (
        0, "false", "播放", "0 / 4", True, False, [False, True, False])
    f(page, "play").click()
    page.clock.run_for(520)
    assert ev(page, PL)["k"] == 1 and ev(page, PL)["play"] == "暂停" and "k = 1" in vis(page)
    f(page, "speed-2").click()                                           # 250 ms a step now
    page.clock.run_for(760)
    assert ev(page, PL)["k"] == 4
    p = ev(page, PL)
    assert (p["playing"], p["play"], p["fwd"], p["node"]) == ("false", "从头播放", True, {"speed": 2, "playing": False})
    page.clock.run_for(2000)
    assert ev(page, PL)["k"] == 4                                        # stopped at max
    f(page, "play").click()                                              # at max: starts again from min
    assert ev(page, PL)["k"] == 0
    page.clock.run_for(260)
    assert ev(page, PL)["k"] == 1 and ev(page, PL)["playing"] == "true"
    assert ev(page, "got.events") == []
    page.close()


def test_a_looping_player_wraps_and_ticks_save_progress_lazily(browser):
    block = copy.deepcopy(PLAYER)
    block["body"]["children"][0].update(loop=True, interval_ms=100)
    page = mount(browser, block, clock=True)
    f(page, "play").click()
    page.clock.run_for(50)
    n0 = ev(page, "got.progress.length")
    page.clock.run_for(2000)                                             # 20 steps, 4 laps
    assert ev(page, PL)["playing"] == "true"
    assert ev(page, "got.progress.length") - n0 <= 3                     # at most one onProgress a second
    ks = [ev(page, "h.state.k")]
    for _ in range(7):
        page.clock.run_for(100)
        ks.append(ev(page, "h.state.k"))
    assert all(b == (a + 1) % 5 for a, b in itertools.pairwise(ks)) and 4 in ks, ks   # 3, 4, 0, 1 …
    page.close()


def test_player_steps_while_paused_keys_scrub_and_restore(browser):
    page = mount(browser, PLAYER, clock=True)
    f(page, "fwd").click()
    f(page, "fwd").click()
    assert ev(page, PL)["k"] == 2
    f(page, "back").click()
    assert ev(page, PL)["k"] == 1
    f(page, "play").click()
    page.clock.run_for(520)
    f(page, "fwd").click()                                               # a step pauses a playing player
    p = ev(page, PL)
    assert (p["k"], p["playing"]) == (3, "false")
    f(page, "play").focus()
    page.keyboard.press("ArrowLeft")
    assert ev(page, PL)["k"] == 2
    page.locator(".evoui-pl-scrub").fill("4")
    assert ev(page, PL)["k"] == 4 and ev(page, PL)["count"] == "4 / 4"
    f(page, "restart").click()
    assert ev(page, PL)["k"] == 0 and ev(page, PL)["back"] is True
    f(page, "speed-0.5").click()
    f(page, "play").click()
    page.clock.run_for(1100)
    saved = ev(page, "h.flush()")
    assert saved["nodes"]["run"] == {"speed": 0.5, "playing": True} and saved["state"]["k"] == 1
    page.close()
    page = mount(browser, PLAYER, clock=True, progress=saved)
    page.clock.run_for(3000)
    p = ev(page, PL)
    assert (p["k"], p["playing"], p["speed"]) == (1, "false", [True, False, False])   # never plays after a reload
    assert ev(page, "[got.events.length, got.progress.length, got.warn.length]") == [0, 0, 0]
    page.close()
    page = mount(browser, PLAYER, clock=True, progress={**saved, "nodes": {"run": {"speed": 3, "playing": False}}})
    assert ev(page, "got.warn.length") == 1 and ev(page, PL)["node"] == {"speed": 1, "playing": False}
    page.close()


def test_the_player_is_a_control_for_the_render_check():
    b = EvoUIBlock.model_validate(PLAYER)
    assert block_probes(b) == [({"k": 0}, {"k": 4})]


# ---------------------------------------------------------------- the demo block (every Task 5a node)
# A bubble-sort stepper: the player drives `step`, the bars glide (plot), the pass number crossfades
# (transition), the `if` badge switches, a reveal explains in steps, a timer, confetti when sorted,
# a quiz and a reveal that opens after it. Also used by the render check test and the screenshots.
BARS = [{"shape": "rect", "x": i + 0.12, "y": 0, "w": 0.76, "h": f"cur.a[{i}]", "tone": "accent",
         "fill": True, "label": f"{{cur.a[{i}]}}"} for i in range(8)]

TRACE = ("iterate(step + 1, {a: [7, 3, 8, 2, 6, 1, 5, 4], j: 0, p: 0, cj: -1, sw: false, m: 0, done: 0}, "
         "s => {a: s.a[s.j] > s.a[s.j + 1] ? map(range(8), k => k == s.j ? s.a[s.j + 1] : "
         "k == s.j + 1 ? s.a[s.j] : s.a[k]) : s.a, j: s.j >= 6 - s.p ? 0 : s.j + 1, "
         "p: s.j >= 6 - s.p ? s.p + 1 : s.p, cj: s.j, sw: s.a[s.j] > s.a[s.j + 1], "
         "m: s.m + (s.a[s.j] > s.a[s.j + 1] ? 1 : 0), done: s.j >= 6 - s.p ? s.done + 1 : s.done})[step]")

DEMO = {
    "type": "evoui", "title": "冒泡排序：一步一步看", "question": "冒泡排序每一步在做什么，为什么最大的数会先到右边？",
    "state": {"step": 0, "secs": 0},
    "derived": {
        "cur": TRACE,
        "pass": "min(cur.p + 1, 7)",
        "swaps": "cur.m",
        "sorted": "step == 28",
    },
    "body": {"kind": "box", "gap": 3, "children": [
        {"kind": "player", "id": "run", "bind": "step", "min": 0, "max": 28, "step": 1, "interval_ms": 700,
         "label": "比较第 {step} 次"},
        {"kind": "plot", "xmin": 0, "xmax": 8, "ymin": 0, "ymax": 9, "height": 220, "items": [
            {"shape": "rect", "x": "sorted ? 0 : 8 - cur.done", "y": 0, "w": "sorted ? 8 : cur.done", "h": 9, "tone": "success", "fill": True,
             "dashed": True},
            {"shape": "rect", "x": "sorted ? -3 : max(cur.cj, 0) + 0.04", "y": 0.06, "w": 1.92, "h": 8.88, "tone": "warning",
             "dashed": True},
            *BARS]},
        {"kind": "row", "justify": "between", "wrap": True, "children": [
            {"kind": "transition", "key": "cur.p", "children": [
                {"kind": "text", "weight": "bold", "text": "{sorted ? '排好了' : '第 ' + pass + ' 趟'}"}]},
            {"kind": "if", "cond": "sorted",
             "then": {"kind": "badge", "tone": "success", "text": "排序完成 · 交换 {swaps} 次"},
             "else": {"kind": "badge", "tone": "secondary", "text": "已交换 {swaps} 次"}}]},
        {"kind": "text", "size": "sm", "tone": "secondary",
         "text": "{step == 0 ? '按 ▶ 开始：每一步比较一对相邻的数。' : '比较位置 ' + (cur.cj + 1) + ' 和 ' + (cur.cj + 2) + "
                 "'：' + (cur.sw ? '左边更大，交换。' : '已经有序，不动。')}"},
        {"kind": "celebrate", "when": "sorted"},
        {"kind": "if", "cond": "sorted", "then": {"kind": "animate", "children": [
            {"kind": "text", "text": "每一趟都把剩下的数里最大的那个「冒」到最右边，所以 7 趟之后一定有序。"}]}, "else": None},
        {"kind": "divider"},
        {"kind": "reveal", "id": "why", "mode": "steps", "children": [
            {"kind": "text", "text": "一趟里从左到右比较相邻的两个数，左边大就交换。"},
            {"kind": "text", "text": "最大的数一路被交换到最右边，这一趟之后它的位置就定了（绿色区域）。"},
            {"kind": "text", "text": "下一趟只需要排剩下的数，所以 n 个数最多比较 n(n−1)/2 = 28 次。"}]},
        {"kind": "timer", "id": "clock", "mode": "countdown", "seconds": 90, "bind": "secs",
         "label": "限时 90 秒：先自己在脑子里排一遍"},
        {"kind": "quiz", "id": "check", "layout": "paged", "title": "检查一下", "questions": [
            {"q": "8 个数的冒泡排序（不提前结束）一共比较几次？", "options": ["8", "28", "64"], "correct": 1,
             "why": "7 + 6 + … + 1 = 28。"},
            {"q": "第一趟结束后，哪个数一定在最右边？", "options": ["最小的", "最大的", "不一定"], "correct": 1,
             "why": "最大的数每次比较都会被换到右边。"}]},
        {"kind": "reveal", "mode": "after", "after": "check", "children": [
            {"kind": "text", "text": "延伸：如果某一趟一次交换都没有，说明已经有序，可以提前结束——这就是「提前结束」优化。"}]},
    ]},
    "fallback": "冒泡排序每一趟从左到右比较相邻的数，左边大就交换，最大的数会被换到最右边；8 个数最多比较 28 次。",
}


def test_the_demo_block_runs_end_to_end(browser):
    page = mount(browser, DEMO, width=375)
    assert "第 1 趟" in vis(page) and ev(page, "h.progress().nodes.run") == {"speed": 1, "playing": False}
    page.evaluate("h.set('step', 27)")
    f(page, "fwd").first.click()
    assert "排好了" in vis(page) and "排序完成" in vis(page)
    assert ev(page, CONFETTI) == 1
    assert ev(page, "document.documentElement.scrollWidth - document.documentElement.clientWidth") == 0
    page.close()


# ---------------------------------------------------------------- fix round 1

def test_a_quiz_inside_a_transition_keeps_its_checked_radio_after_a_key_change(browser):
    """The leaving copy is stripped of names before it is inserted: a cloned checked radio never joins
    (and unchecks) the live radio group."""
    block = blk(box({"kind": "choice", "bind": "mode", "options": [{"label": "甲", "value": "a"}, {"label": "乙", "value": "b"}]},
                    {"kind": "transition", "key": "mode", "children": [text("模式 {mode}"), QUIZ]}), state={"mode": "a"})
    page = mount(browser, block)
    page.locator("input[type=radio]").nth(1).check()
    page.locator(".evoui-choice button").nth(1).click()
    live = "[...document.querySelectorAll('input[type=radio]')].filter(r => !r.closest('[data-leaving]')).map(r => r.checked)"
    assert ev(page, live) == [False, True]
    assert ev(page, "[...document.querySelectorAll('[data-leaving] input')].map(r => r.name)") == ["", ""]
    page.wait_for_timeout(250)
    assert ev(page, live) == [False, True] and ev(page, "h.progress().nodes.chk.answers") == [1]
    page.locator(".evoui-check").click()
    assert "答对 1 / 1" in vis(page)
    page.close()


def test_a_leaving_timer_never_touches_the_rebuilt_timers_progress(browser):
    block = blk(box({"kind": "toggle", "bind": "on"},
                    {"kind": "if", "cond": "on", "then": {"kind": "timer", "mode": "stopwatch", "autostart": True}, "else": None}),
                state={"on": True})
    page = mount(browser, block, clock=True)
    page.clock.run_for(300)
    page.locator(".evoui-toggle input").uncheck()
    page.locator(".evoui-toggle input").check()          # rebuilt while the old one is still a ghost
    page.clock.run_for(1500)                               # the old instance's last tick happens in here
    t = ev(page, "[document.querySelector('.evoui-tm:not([data-leaving] *)').dataset.state, h.progress().nodes['timer#0'].running]")
    assert t == ["running", True]
    page.close()


def test_a_player_step_larger_than_the_range_stops_at_max(browser):
    block = blk(box({"kind": "player", "bind": "k", "min": 0, "max": 2, "step": 3}, text("k = {k}")), state={"k": 0})
    page = mount(browser, block, clock=True)
    f(page, "fwd").click()
    assert ev(page, "h.state.k") == 2 and "k = 2" in vis(page)
    f(page, "play").click()                                # at the end: from min again
    page.clock.run_for(800)
    assert ev(page, "h.state.k") == 2 and ev(page, "document.querySelector('.evoui-pl').dataset.playing") == "false"
    page.close()
