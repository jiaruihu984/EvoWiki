"""EvoUI v2 Task 5b in headless Chromium (skipped, saying why, without it): pager (gate, page motion,
summary, confetti), fill-blank, sentence builder and route — clicks and keys, the events (validated
by `BlockEventIn`, item events graded by the backend grader), progress restore and the render check."""

import copy
import re

import pytest

from evowiki.deep.evoui import JS_PATH, controls, node_keys, quiz_cells, validate
from evowiki.wikiapp.block_results import _grade_item
from evowiki.wikiapp.wiki_schemas import BlockEventIn

FILL = {"kind": "fill-blank", "id": "fb", "cell": "c1.interpret",
        "sentence": "z = 1.5 表示这个值比均值高 ___ 个标准差。",
        "options": [{"label": "1.5", "why": "z 就是离均值几个标准差。"},
                    {"label": "15", "why": "把 z 当成了原始分的差。"},
                    {"label": "0.15", "why": "z 不是百分比。"},
                    {"label": "3"}],
        "correct": 0, "why": "z = (x − μ) / σ：差了几个标准差。"}
SENT = {"kind": "sentence", "id": "sb", "sentence": "z 分数 = (___ − ___) ÷ ___",
        "words": ["x", "σ", "μ", "n", "σ²"], "answer": [0, 2, 1], "why": "先减去均值，再除以标准差。"}
QUIZ = {"kind": "quiz", "id": "qz", "layout": "paged", "questions": [
    {"q": "z = −2 的值在均值的哪一边？", "options": ["上方", "下方"], "correct": 1, "why": "负的 z 在均值下方。"},
    {"q": "两个 z 都是 1 的值，离各自的均值一样远吗？", "options": ["按各自的标准差算，一样远", "原始分一定一样远"],
     "correct": 0, "why": "z 用各自的标准差做单位。"}]}
ROUTE = {"kind": "route", "id": "how", "prompt": "这一节想怎么学？", "preference": "teaching_mode", "bind": "mode",
         "options": [{"label": "一步步引导", "value": "guided", "next": "请一步一步引导我理解 z 分数，每步先问我一个问题。",
                      "desc": "每一步先问你，再给提示"},
                     {"label": "直接看讲解", "value": "explain", "next": "请直接完整讲解 z 分数的含义和算法。",
                      "desc": "一口气讲清楚，再做练习"},
                     {"label": "考考我", "value": "quiz", "next": "请出几道 z 分数的练习题考我。",
                      "desc": "先做题，错了再讲"}]}
PAGER = {"kind": "pager", "id": "pg", "gate": True, "children": [
    {"kind": "box", "children": [{"kind": "text", "text": "先填一个空：", "tone": "secondary"}, FILL]},
    SENT,
    QUIZ]}
DEMO = {"type": "evoui", "title": "z 分数：练一练", "question": "z 分数说的是离均值几个标准差吗？",
        "state": {"mode": ""},
        "derived": {"name": "mode == 'guided' ? '一步步引导' : mode == 'explain' ? '直接看讲解' : mode == 'quiz' ? '考考我' : ''"},
        "body": {"kind": "box", "gap": 4, "children": [
            ROUTE,
            {"kind": "caption", "text": "{mode == '' ? '选一种方式，下一轮就按它来。' : '已记下：' + name + '。下一轮按这种方式继续。'}"},
            PAGER]},
        "fallback": "z 分数 = (x − μ) ÷ σ，表示一个值离均值几个标准差；正的在均值上方，负的在下方。"}

SETUP = """([b, p]) => {
  window.got = {events: [], warn: [], progress: []};
  const w = console.warn; console.warn = (...a) => { window.got.warn.push(a.join(' ')); w(...a); };
  const opts = {blockId: 'blk', onEvent: e => got.events.push(e), onProgress: x => got.progress.push(x)};
  if (p) opts.progress = p;
  window.h = EvoUI.render(b, document.getElementById('m'), opts);
}"""


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


def mount(browser, block=DEMO, progress=None, reduced=False, width=420):
    page = browser.new_page(reduced_motion="reduce" if reduced else "no-preference",
                            viewport={"width": width, "height": 900})
    page.set_content("<!doctype html><meta charset=utf-8><body><div id=m></div></body>")
    page.add_style_tag(content=(JS_PATH.parent / "evoui.css").read_text(encoding="utf-8"))
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate(SETUP, [block, progress])
    return page


def solo(*nodes, state=None):
    """A block whose body is a box of these nodes."""
    return {"type": "evoui", "title": "练习", "question": "练一练", "state": state or {}, "derived": {},
            "body": {"kind": "box", "children": copy.deepcopy(list(nodes))}, "fallback": "f"}


def f(page, name, within=""):
    return page.locator(f"{within} [data-f='{name}']".strip()).first


def events(page):
    return page.evaluate("window.got.events")


def prog(page, key):
    return page.evaluate("k => h.progress().nodes[k]", key)


def slots(page, within=""):
    return page.evaluate("s => [...document.querySelectorAll((s ? s + ' ' : '') + '.evoui-sx-slot')]"
                         ".map(x => [x.dataset.s, x.innerText])", within)


def pills(page):
    # by the written order (on screen they are shuffled)
    return page.evaluate("[...document.querySelectorAll('.evoui-pill')]"
                         ".sort((a, b) => a.dataset.f.localeCompare(b.dataset.f, 'en', {numeric: true}))"
                         ".map(b => [b.dataset.used, b.innerText.trim(), b.disabled])")


def check_events(evs, block):
    """Every event validates as the backend reads it; every item event's server grade equals the client's."""
    keys = node_keys(block)
    for ev in evs:
        BlockEventIn.model_validate(ev)
        if ev["type"] == "item":
            got = _grade_item(BlockEventIn.model_validate(ev), keys[ev["node"]], {}).correct
            assert got == ev["correct"], ev


def test_the_demo_is_valid_and_its_cells_are_k_cells():
    assert validate(DEMO) == []
    assert quiz_cells(DEMO) == {"c1.interpret"}
    assert list(node_keys(DEMO)) == ["how", "pg", "fb", "sb", "qz"]
    unbound = {k: v for k, v in ROUTE.items() if k != "bind"}
    assert [c["kind"] for c in controls(DEMO["body"])] == ["route"]      # a bound route is a control …
    assert list(controls(solo(unbound)["body"])) == []                  # … an unbound one is not


def test_fill_blank_pick_judge_lock_feedback_retry(browser):
    page = mount(browser, solo(FILL))
    assert slots(page) == [["active", ""]]
    assert page.locator(".evoui-pc-title").inner_text() == "填空"
    f(page, "opt-1").click()                                   # 15: wrong
    assert slots(page) == [["filled", "15"]]                    # grey first …
    assert page.locator(".evoui-fbk").is_hidden()
    page.wait_for_timeout(400)
    assert slots(page) == [["wrong", "15"]]                     # … then judged
    assert page.evaluate("[...document.querySelectorAll('.evoui-pill')]"
                         ".every(b => b.disabled && b.dataset.dim === 'true')")
    fb = page.locator(".evoui-fbk").inner_text()
    assert "不对" in fb and "正确答案" in fb and "z = 1.5 表示这个值比均值高 1.5 个标准差。" in fb
    assert "把 z 当成了原始分的差。" in fb and "差了几个标准差" in fb
    f(page, "opt-0").click(force=True)                         # locked: nothing
    evs = events(page)
    assert [(e["type"], e.get("kind")) for e in evs] == [("item", "fill-blank"), ("summary", "fill-blank")]
    it, sm = evs
    assert (it["node"], it["item"], it["attempt"], it["response"], it["correct"]) == ("fb", 0, 1, 1, False)
    assert it["hintUsed"] is False and it["latencyMs"] >= 0 and it["block"] == "blk"
    assert (sm["total"], sm["right"], sm["missed"], sm["hintsUsed"]) == (1, 0, [0], 0)
    assert sm["timeMs"] == it["latencyMs"] and sm["revision"] == it["revision"]
    assert prog(page, "fb") == {"attempt": 1, "answer": 1, "done": True, "ms": it["latencyMs"]}
    f(page, "retry").click()                                   # 再试一次: attempt 2
    assert slots(page) == [["active", ""]] and page.locator(".evoui-fbk").is_hidden()
    assert prog(page, "fb")["attempt"] == 2 and prog(page, "fb")["answer"] is None
    assert page.evaluate("document.activeElement.dataset.f") == "opt-0"
    # keys 1–8 pick by the order on screen (a fixed shuffle: the writer tends to put the answer first)
    shown = page.evaluate("[...document.querySelector('.evoui-pills').children].map((b) => b.dataset.f)")
    page.keyboard.press(str(shown.index("opt-0") + 1))
    page.wait_for_timeout(400)
    assert slots(page) == [["right", "1.5"]]
    fb = page.locator(".evoui-fbk").inner_text()
    assert "答对了" in fb and "z 就是离均值几个标准差。" in fb and "正确答案" not in fb
    assert page.locator(".evoui-fbk [data-f='retry']").count() == 0   # no retry after a right answer
    evs = events(page)
    assert (evs[2]["attempt"], evs[2]["response"], evs[2]["correct"]) == (2, 0, True)
    assert (evs[3]["type"], evs[3]["attempt"], evs[3]["right"], evs[3]["missed"]) == ("summary", 2, 1, [])
    check_events(evs, solo(FILL))
    page.close()


def test_fill_blank_under_reduced_motion_judges_at_once(browser):
    page = mount(browser, solo(FILL), reduced=True)
    f(page, "opt-0").click()
    assert slots(page) == [["right", "1.5"]] and page.locator(".evoui-fbk").is_visible()
    page.close()


def test_sentence_builder_order_numbers_undo_judge_retry(browser):
    page = mount(browser, solo(SENT))
    assert slots(page) == [["active", ""], ["empty", ""], ["empty", ""]]
    f(page, "word-0").click()
    f(page, "word-1").click()                                  # σ in the second blank (wrong so far)
    p = pills(page)
    assert p[0] == ["true", "1\nx", True] and p[1] == ["true", "2\nσ", True] and p[2] == ["false", "μ", False]
    assert slots(page) == [["filled", "x"], ["filled", "σ"], ["active", ""]]
    f(page, "undo").click()                                    # ↶ takes σ back
    assert slots(page)[1] == ["active", ""] and prog(page, "sb")["picks"] == [0]
    f(page, "word-1").click()
    page.locator(".evoui-sb").focus()
    page.keyboard.press("Backspace")                           # the key undoes too
    assert prog(page, "sb")["picks"] == [0] and events(page) == []
    f(page, "word-2").click()
    f(page, "word-3").click()                                  # n: wrong in the last blank → judged
    page.wait_for_timeout(400)
    assert slots(page) == [["filled", "x"], ["filled", "μ"], ["wrong", "n"]]   # only the wrong blank is red
    assert page.locator("[data-f='undo']").is_hidden()
    fb = page.locator(".evoui-fbk").inner_text()
    assert "不对" in fb and "z 分数 = (x − μ) ÷ σ" in fb and "先减去均值" in fb
    assert all(x[2] for x in pills(page))
    it, sm = events(page)
    assert (it["kind"], it["item"], it["response"], it["correct"], it["attempt"]) == ("sentence", 0, [0, 2, 3], False, 1)
    assert (sm["kind"], sm["total"], sm["right"], sm["missed"]) == ("sentence", 1, 0, [0])
    f(page, "retry").click()
    for w in (0, 2, 1):
        f(page, f"word-{w}").click()
    page.wait_for_timeout(400)
    assert slots(page) == [["right", "x"], ["right", "μ"], ["right", "σ"]]
    assert "答对了" in page.locator(".evoui-fbk").inner_text()
    evs = events(page)
    assert (evs[2]["attempt"], evs[2]["response"], evs[2]["correct"]) == (2, [0, 2, 1], True)
    assert prog(page, "sb") == {"attempt": 2, "picks": [0, 2, 1], "done": True, "ms": evs[2]["latencyMs"]}
    check_events(evs, solo(SENT))
    page.close()


def test_fill_blank_and_sentence_restore_without_events_and_discard_misfits(browser):
    p = {"v": 2, "revision": 9, "state": {}, "nodes": {
        "fb": {"attempt": 2, "answer": 1, "done": True, "ms": 1500},
        "sb": {"attempt": 1, "picks": [4], "done": False, "ms": 0}}}
    page = mount(browser, solo(FILL, SENT), progress=p)
    page.wait_for_timeout(300)
    assert slots(page, ".evoui-fb") == [["wrong", "15"]] and page.locator(".evoui-fb .evoui-fbk").is_visible()
    assert slots(page, ".evoui-sb") == [["filled", "σ²"], ["active", ""], ["empty", ""]]
    assert page.evaluate("window.got") == {"events": [], "warn": [], "progress": []}
    assert page.evaluate("h.progress().revision") == 9
    page.close()
    bad = {"v": 2, "revision": 3, "state": {}, "nodes": {
        "fb": {"attempt": 1, "answer": 9, "done": True, "ms": 0},                 # no option 9
        "sb": {"attempt": 1, "picks": [0, 1, 2], "done": False, "ms": 0}}}         # full but not done
    page = mount(browser, solo(FILL, SENT), progress=bad)
    assert len(page.evaluate("window.got.warn")) == 2
    assert prog(page, "fb")["answer"] is None and prog(page, "sb")["picks"] == []
    page.close()


def test_pager_gate_pages_motion_summary_and_restore(browser):
    page = mount(browser)
    pg = ".evoui-pg"
    assert page.locator(f"{pg} .evoui-pq-count").first.inner_text() == "1/3"
    assert f(page, "fwd", pg).is_disabled() and f(page, "next", pg).is_disabled()
    assert page.locator(".evoui-pg-tip").inner_text() == "做完这一页的练习再继续"
    f(page, "opt-1").click()                                   # wrong is still done: the gate opens
    assert not f(page, "next", pg).is_disabled() and page.locator(".evoui-pg-tip").is_hidden()
    f(page, "next", pg).click()
    swap = page.evaluate("""() => { const g = document.querySelector('.evoui-pg-view > [data-leaving]');
      const live = document.querySelector('.evoui-pg-view > .evoui-pg-page:not([data-leaving])');
      return [!!g && g.inert, live.classList.contains('evoui-enter'), !!live.querySelector('.evoui-sb')]; }""")
    assert swap == [True, True, True]                           # the old page leaves, the new one enters
    page.wait_for_timeout(300)
    assert page.locator(".evoui-pg-view > *").count() == 1
    assert f(page, "fwd", pg).is_disabled()                    # the sentence is not done yet
    f(page, "prev", pg).click()                                # back is always allowed
    page.wait_for_timeout(250)
    assert slots(page, pg) == [["wrong", "15"]]                # the page comes back as it was
    f(page, "fwd", pg).focus()
    page.keyboard.press("ArrowRight")                          # ← → on the pager's buttons
    page.wait_for_timeout(250)
    assert prog(page, "pg")["page"] == 1
    for w in (0, 2, 1):
        f(page, f"word-{w}").click()
    f(page, "next", pg).click()
    page.wait_for_timeout(250)
    assert f(page, "finish", pg).is_disabled() and f(page, "finish", pg).inner_text() == "完成"
    f(page, "opt-1", ".evoui-pq").click()                      # quiz q0 right
    f(page, "next", ".evoui-pq").click()
    page.wait_for_timeout(250)
    f(page, "opt-0", ".evoui-pq").click()                      # q1 right: the quiz is done
    fin = f(page, "finish", pg)
    assert not fin.is_disabled() and "primary" in fin.get_attribute("class")
    n0 = len(events(page))
    fin.click()
    evs = events(page)
    sm = evs[n0]
    assert len(evs) == n0 + 1 and (sm["type"], sm["kind"], sm["node"]) == ("summary", "pager", "pg")
    assert (sm["attempt"], sm["total"], sm["right"], sm["missed"], sm["hintsUsed"]) == (1, 4, 3, ["fb"], 0)
    assert sm["timeMs"] > 0
    assert page.evaluate("!!document.querySelector('.evoui-confetti')") is False   # fb was wrong: no confetti
    assert f(page, "finish", pg).is_disabled() and "已完成" in f(page, "finish", pg).inner_text()
    assert page.locator(".evoui-pg-tip").inner_text() == "答对 3 / 4"
    check_events(evs, DEMO)
    saved = page.evaluate("h.progress()")
    assert saved["nodes"]["pg"]["done"] is True and saved["nodes"]["pg"]["page"] == 2
    page.close()
    page = mount(browser, progress=saved)                      # restore: same page, done, nothing fired
    page.wait_for_timeout(300)
    assert page.locator(".evoui-pg .evoui-pq-count").first.inner_text() == "3/3"
    assert "已完成" in f(page, "finish", ".evoui-pg").inner_text()
    got = page.evaluate("[got.events.length, got.warn.length, got.progress.length, !!document.querySelector('.evoui-confetti')]")
    assert got == [0, 0, 0, False]
    page.close()


def test_pager_all_right_throws_confetti_once(browser):
    pager = {"kind": "pager", "gate": True, "children": [FILL, {"kind": "text", "text": "读一读：z 是标准化的分数。"}]}
    page = mount(browser, solo(pager))
    f(page, "opt-0").click()
    f(page, "next").click()
    page.wait_for_timeout(250)
    assert not f(page, "finish").is_disabled()                 # a page without practice is always complete
    f(page, "finish").click()
    assert page.evaluate("!!document.querySelector('.evoui-confetti')")
    sm = events(page)[-1]
    assert (sm["kind"], sm["node"], sm["total"], sm["right"], sm["missed"]) == ("pager", "pager#0", 1, 1, [])
    assert prog(page, "pager#0")["celebrated"] is True
    page.close()
    page = mount(browser, solo(pager), reduced=True)           # reduced motion: no confetti
    f(page, "opt-0").click()
    f(page, "next").click()
    f(page, "finish").click()
    assert page.evaluate("!!document.querySelector('.evoui-confetti')") is False
    page.close()


def test_a_pager_without_gate_pages_freely_and_counts_untouched_items_as_not_right(browser):
    page = mount(browser, solo({"kind": "pager", "children": [FILL, SENT]}))
    assert not f(page, "next").is_disabled() and page.locator(".evoui-pg-tip").is_hidden()
    f(page, "next").click()
    page.wait_for_timeout(250)
    f(page, "finish").click()
    sm = events(page)[-1]
    assert (sm["kind"], sm["total"], sm["right"], sm["missed"]) == ("pager", 2, 0, ["fb", "sb"])
    page.close()


def test_reveal_after_a_fill_blank(browser):
    b = solo(FILL, {"kind": "reveal", "mode": "after", "after": "fb",
                    "children": [{"kind": "text", "text": "延伸：z 可以比较不同的考试。"}]})
    assert validate(b) == []
    page = mount(browser, b)
    assert page.locator(".evoui-rv").get_attribute("data-shown") == "0"
    f(page, "opt-2").click()
    assert page.locator(".evoui-rv").get_attribute("data-shown") == "1"
    page.close()


def test_route_choice_event_bind_rechoose_keys_and_restore(browser):
    page = mount(browser)
    cap = page.locator(".evoui-text.s-sm.t-secondary").first
    assert cap.inner_text() == "选一种方式，下一轮就按它来。"
    assert page.evaluate("document.querySelector('.evoui-rt').dataset.chosen") == "-1"
    f(page, "route-1").click()
    states = page.evaluate("[...document.querySelectorAll('.evoui-rt-opt')]"
                           ".map(b => [b.dataset.state, b.getAttribute('aria-pressed')])")
    assert states == [["other", "false"], ["chosen", "true"], ["other", "false"]]
    assert cap.inner_text() == "已记下：直接看讲解。下一轮按这种方式继续。"
    assert page.evaluate("h.state.mode") == "explain" and prog(page, "how") == {"chosen": 1}
    ev = events(page)[0]
    assert re.fullmatch(r"[0-9a-f]{16}", ev["session"])
    assert ev == {"type": "route", "block": "blk", "session": ev["session"], "node": "how", "option": "explain",
                  "next": "请直接完整讲解 z 分数的含义和算法。", "preference": "teaching_mode", "revision": ev["revision"]}
    f(page, "route-1").click()                                 # the same choice again: nothing new
    assert len(events(page)) == 1
    f(page, "route-1").focus()
    page.keyboard.press("ArrowDown")                           # arrows move between the options …
    assert page.evaluate("document.activeElement.dataset.f") == "route-2"
    page.keyboard.press("Enter")                               # … Enter chooses
    assert [e["option"] for e in events(page)] == ["explain", "quiz"]
    assert page.evaluate("h.state.mode") == "quiz"
    page.evaluate("h.set('mode', 'guided')")                   # the highlight is the reader's choice, not the state
    assert page.evaluate("document.querySelector('.evoui-rt').dataset.chosen") == "2"
    check_events(events(page), DEMO)
    saved = page.evaluate("h.progress()")
    page.close()
    page = mount(browser, progress=saved)
    assert page.evaluate("[document.querySelector('.evoui-rt').dataset.chosen, got.events.length]") == ["2", 0]
    page.close()


def test_a_bound_state_equal_to_an_option_is_not_a_choice(browser):
    """Fix round 1: state {mode: "guided"} = the first option's value: nothing is marked before the
    reader chooses, and choosing that option still sends the route event with the preference."""
    b = copy.deepcopy(DEMO)
    b["state"]["mode"] = "guided"
    page = mount(browser, b)
    marks = page.evaluate("[...document.querySelectorAll('.evoui-rt-opt')].map(x => x.dataset.state)")
    assert marks == ["idle", "idle", "idle"] and prog(page, "how") == {"chosen": None}
    f(page, "route-0").click()
    ev = events(page)
    assert [(e["type"], e["option"], e["preference"]) for e in ev] == [("route", "guided", "teaching_mode")]
    assert page.evaluate("h.state.mode") == "guided" and prog(page, "how") == {"chosen": 0}
    assert page.evaluate("document.querySelector('.evoui-rt').dataset.chosen") == "0"
    f(page, "route-0").click()                                 # the same choice again: nothing new
    assert len(events(page)) == 1
    check_events(events(page), b)
    page.close()


def test_an_unbound_route_keeps_its_choice_in_progress(browser):
    r = {k: v for k, v in ROUTE.items() if k not in ("bind", "preference", "id")}
    page = mount(browser, solo(r))
    f(page, "route-2").click()
    ev = events(page)[0]
    assert (ev["node"], ev["option"], ev["preference"]) == ("route#0", "quiz", None)
    saved = page.evaluate("h.progress()")
    assert saved["nodes"]["route#0"] == {"chosen": 2}
    page.close()
    page = mount(browser, solo(r), progress=saved)
    assert page.evaluate("document.querySelector('.evoui-rt').dataset.chosen") == "2"
    page.close()


def test_the_demo_has_no_overflow_at_375(browser):
    page = mount(browser, width=375)
    f(page, "opt-1").click()
    page.wait_for_timeout(400)
    assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0
    page.close()
