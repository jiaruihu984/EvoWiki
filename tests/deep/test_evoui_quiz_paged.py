"""EvoUI v2 Task 3 in headless Chromium (skipped, saying why, without it): the paged quiz (ChatGPT
LearningQuiz parity: judged on click / on 「提交答案」, locked feedback, hollow ring for a missed multi
option, ‹ n/N › gating, hint toggle, results page with restart / wrong-only / request, confetti once per
perfect attempt and never on restore), its events and progress, and the list quiz's multi / feedback /
hint additions. Every emitted item event is also graded by the backend grader to check the shapes."""

import copy

import pytest

from evowiki.deep.evoui import JS_PATH, validate
from evowiki.wikiapp.block_results import _grade_item
from evowiki.wikiapp.wiki_schemas import BlockEventIn

PAGED = {"kind": "quiz", "id": "pq", "layout": "paged", "title": "小测", "questions": [
    {"q": "抛 3 次硬币至少一次正面？", "options": ["1/2", "3/4", "7/8"], "correct": 2, "why": "1 − 1/8",
     "feedback": ["只抛一次", "抛两次", "对：补集"], "hint": "先算全是反面", "cell": "c1.interpret"},
    {"q": "哪些是质数？", "type": "multi", "options": ["2", "9", "11"], "correct": [0, 2], "why": "只能被 1 和自己整除",
     "feedback": ["偶质数", "3 × 3", "质数"]},
    {"q": "0 是偶数。", "type": "tf", "options": ["对", "错"], "correct": 0, "why": "0 = 2 × 0", "hint": "定义"},
    {"q": "骰子掷出 6？", "options": ["1/3", "1/6"], "correct": 1, "why": "六面之一"}]}
BLOCK = {"type": "evoui", "title": "测一测", "question": "会用补集吗？", "state": {}, "derived": {},
         "body": PAGED, "fallback": "7/8；2 和 11；0 是偶数；1/6。"}
RIGHT = [2, [0, 2], 0, 1]

LIST = {"kind": "quiz", "questions": [
    {"q": "哪些是偶数？", "type": "multi", "options": ["2", "3", "4"], "correct": [0, 2], "why": "能被 2 整除",
     "feedback": ["偶", "奇", "偶"], "hint": "看个位"},
    {"q": "1+1？", "options": ["2", "3"], "correct": 0, "why": "2"}]}
LIST_BLOCK = {"type": "evoui", "title": "列表", "question": "q", "state": {}, "derived": {}, "body": LIST,
              "fallback": "f"}

SETUP = """([b, p]) => {
  window.got = {events: [], quiz: [], warn: []};
  const w = console.warn; console.warn = (...a) => { window.got.warn.push(a.join(' ')); w(...a); };
  const opts = {blockId: 'blk', onEvent: e => got.events.push(e), onQuizResult: r => got.quiz.push(r)};
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


def mount(browser, block=BLOCK, progress=None, reduced=False):
    page = browser.new_page(reduced_motion="reduce" if reduced else "no-preference")
    page.set_content("<!doctype html><meta charset=utf-8><div id=m></div>")
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate(SETUP, [block, progress])
    return page


def opt(page, i):
    return page.locator(f".evoui-pq-opt[data-f='opt-{i}']")


def f(page, name):
    return page.locator(f"[data-f='{name}']")


def marks(page):
    return page.evaluate("[...document.querySelectorAll('.evoui-pq-opt')].map(o => o.dataset.mark || '')")


def answer(page, response, *, last=False):
    """Answer the current page and move on (single: click; multi: picks + 提交答案)."""
    if isinstance(response, list):
        for k in response:
            opt(page, k).click()
        f(page, "submit").click()
    else:
        opt(page, response).click()
    f(page, "next").click()


def graded(ev, block=BLOCK):
    """The backend's verdict for an emitted item event (raises when the shape does not fit)."""
    node = block["body"]
    return _grade_item(BlockEventIn.model_validate(ev), node, {id(node): 0}).correct


def test_the_blocks_are_valid():
    assert validate(BLOCK) == [] and validate(LIST_BLOCK) == []


def test_a_single_question_is_judged_on_click_and_locks(browser):
    page = mount(browser)
    assert page.locator(".evoui-pq-count").inner_text() == "1/4"
    assert f(page, "prev").is_disabled() and f(page, "fwd").is_disabled() and f(page, "next").is_disabled()
    assert page.locator(".evoui-pq-title").inner_text() == "小测"
    assert page.evaluate("[...document.querySelectorAll('.evoui-pq-ind')].map(i => i.textContent)") == ["A", "B", "C"]
    page.wait_for_timeout(60)
    opt(page, 1).click()
    assert marks(page) == ["off", "wrong", "right"]
    assert page.locator(".evoui-pq-fb").all_inner_texts() == ["抛两次", "对：补集"]
    assert all(opt(page, i).is_disabled() for i in range(3))
    assert not f(page, "fwd").is_disabled() and f(page, "next").inner_text() == "下一题"
    assert page.locator(".evoui-sr").inner_text() == "答错了"
    got = page.evaluate("window.got")
    page.close()
    [ev] = got["events"]
    assert {k: ev[k] for k in ("type", "block", "node", "kind", "item", "attempt", "response", "correct",
                               "hintUsed")} == {"type": "item", "block": "blk", "node": "pq", "kind": "quiz", "item": 0,
                                                "attempt": 1, "response": 1, "correct": False, "hintUsed": False}
    assert isinstance(ev["latencyMs"], int) and ev["latencyMs"] >= 60
    assert graded(ev) is False
    assert got["quiz"] == [{"quiz": 0, "question": 0, "chosen": 1, "correct": False, "case": None,
                            "cell": "c1.interpret"}]


def test_multi_toggles_then_submits_and_rings_a_missed_option(browser):
    page = mount(browser)
    answer(page, 2)
    assert page.locator(".evoui-pq-count").inner_text() == "2/4"
    assert page.locator(".evoui-pq-tip").inner_text() == "选出所有适用项"
    assert f(page, "submit").is_disabled()
    opt(page, 0).click()
    opt(page, 1).click()
    opt(page, 1).click()                                     # toggled off again
    assert marks(page) == ["sel", "", ""] and opt(page, 0).get_attribute("aria-pressed") == "true"
    assert len(page.evaluate("window.got.events")) == 1      # picking is not judging
    f(page, "submit").click()
    assert marks(page) == ["right", "off", "missed"]
    assert page.locator(".evoui-pq-tip").count() == 0
    assert page.locator(".evoui-pq-opt[data-mark=missed] .evoui-pq-ind svg").count() == 1
    assert page.locator(".evoui-pq-fb").all_inner_texts() == ["偶质数", "质数"]
    got = page.evaluate("window.got")
    page.close()
    ev = got["events"][1]
    assert (ev["item"], ev["response"], ev["correct"]) == (1, [0], False)
    assert graded(ev) is False
    assert len(got["quiz"]) == 1                             # a multi question is not an onQuizResult


def test_paging_hint_and_keys(browser):
    page = mount(browser)
    f(page, "hint").click()
    assert page.locator(".evoui-pq-hint").inner_text() == "先算全是反面"
    assert f(page, "hint").get_attribute("aria-pressed") == "true"
    page.keyboard.press("ArrowRight")                        # not answered: no paging
    assert page.locator(".evoui-pq-count").inner_text() == "1/4"
    page.keyboard.press("3")                                 # focus is in the card (the hint button)
    assert marks(page) == ["off", "off", "right"]
    page.keyboard.press("ArrowRight")
    assert page.locator(".evoui-pq-count").inner_text() == "2/4"
    assert page.locator(".evoui-pq-hint").count() == 0 and f(page, "hint").count() == 0
    assert page.locator(".evoui-pq-page.evoui-enter").count() == 1
    page.keyboard.press("ArrowLeft")                         # review: still locked, hint closed
    assert page.locator(".evoui-pq-count").inner_text() == "1/4"
    assert marks(page) == ["off", "off", "right"] and page.locator(".evoui-pq-hint").count() == 0
    f(page, "fwd").click()
    page.keyboard.press("a")
    page.keyboard.press("c")
    f(page, "submit").click()
    page.keyboard.press("ArrowRight")
    assert page.locator(".evoui-pq-count").inner_text() == "3/4"
    got = page.evaluate("window.got.events")
    prog = page.evaluate("h.progress().nodes.pq")
    page.close()
    assert [(e["item"], e["response"], e["correct"], e["hintUsed"]) for e in got] == [
        (0, 2, True, True), (1, [0, 2], True, False)]
    assert [graded(e) for e in got] == [True, True]
    assert (prog["current"], prog["judged"], prog["hintOpen"], prog["hintUsed"]) == (
        2, [True, True, False, False], False, [True, False, False, False])


def test_results_summary_wrong_only_and_more(browser):
    page = mount(browser)
    for r in [1, [0], 0, 1]:                                 # q0 and q1 wrong
        answer(page, r)
    assert page.locator(".evoui-pq-num").inner_text() == "2/4"
    rows = page.locator(".evoui-pq-row")
    assert rows.count() == 4
    first = rows.nth(0).inner_text()
    assert "正确答案：C. 7/8" in first and "你选了 B。抛两次" in first
    assert "你选了 A，漏了 C。只能被 1 和自己整除" in rows.nth(1).inner_text()
    assert page.locator("canvas.evoui-confetti").count() == 0
    events = page.evaluate("window.got.events")
    summary = events[-1]
    assert {k: summary[k] for k in ("type", "node", "kind", "attempt", "total", "right", "missed", "hintsUsed")} == {
        "type": "summary", "node": "pq", "kind": "quiz", "attempt": 1, "total": 4, "right": 2, "missed": [0, 1],
        "hintsUsed": 0}
    assert summary["timeMs"] == sum(e["latencyMs"] for e in events[:-1])
    BlockEventIn.model_validate(summary)
    f(page, "wrong").click()                                 # only the wrong ones, attempt 2
    assert page.locator(".evoui-pq-count").inner_text() == "1/2"
    assert page.locator(".evoui-pq-q").inner_text() == PAGED["questions"][0]["q"]
    answer(page, 2)
    answer(page, [0, 2])
    assert page.locator(".evoui-pq-num").inner_text() == "2/2"
    assert page.locator(".evoui-pq-round").inner_text() == "第 2 轮"
    assert f(page, "wrong").count() == 0
    page.wait_for_timeout(50)
    assert page.locator("canvas.evoui-confetti").count() == 1  # a perfect attempt
    f(page, "more").click()
    assert f(page, "more").is_disabled() and f(page, "more").inner_text() == "已请求新的一套"
    events = page.evaluate("window.got.events")
    page.close()
    att2 = [e for e in events if e.get("attempt") == 2]
    assert [(e["type"], e.get("item")) for e in att2] == [("item", 0), ("item", 1), ("summary", None)]
    assert att2[-1]["missed"] == [] and att2[-1]["total"] == 2
    req = events[-1]
    assert {k: req[k] for k in ("type", "block", "node", "what")} == {"type": "request", "block": "blk", "node": "pq",
                                                                      "what": "more"}
    BlockEventIn.model_validate(req)
    assert sum(e["type"] == "request" for e in events) == 1


def test_confetti_once_per_attempt_never_on_restore_or_reduced_motion(browser):
    page = mount(browser)
    for r in RIGHT:
        answer(page, r)
    page.wait_for_timeout(50)
    assert page.locator("canvas.evoui-confetti").count() == 1
    assert page.evaluate("getComputedStyle(document.querySelector('canvas.evoui-confetti')).pointerEvents") == "none"
    saved = page.evaluate("h.flush()")
    page.wait_for_timeout(4200)
    assert page.locator("canvas.evoui-confetti").count() == 0     # removed when done
    f(page, "restart").click()
    assert page.locator(".evoui-pq-count").inner_text() == "1/4"
    page.close()
    assert saved["nodes"]["pq"]["phase"] == "results" and saved["nodes"]["pq"]["celebrated"] == 1

    again = mount(browser, progress=saved)                   # restore: results page, nothing fired
    page2 = again
    assert page2.locator(".evoui-pq-num").inner_text() == "4/4"
    page2.wait_for_timeout(50)
    assert page2.locator("canvas.evoui-confetti").count() == 0
    assert page2.evaluate("window.got") == {"events": [], "quiz": [], "warn": []}
    page2.close()

    calm = mount(browser, reduced=True)
    for r in RIGHT:
        answer(calm, r)
    calm.wait_for_timeout(50)
    assert calm.locator("canvas.evoui-confetti").count() == 0
    assert calm.locator(".evoui-pq-num").inner_text() == "4/4"
    calm.close()


def test_restore_mid_quiz_and_misfit_progress(browser):
    page = mount(browser)
    answer(page, 1)
    f(page, "hint").count()
    saved = page.evaluate("h.flush()")
    page.close()
    p = mount(browser, progress=saved)
    assert p.locator(".evoui-pq-count").inner_text() == "2/4"
    assert p.evaluate("h.progress().revision") == saved["revision"]
    f(p, "prev").click()
    assert marks(p) == ["off", "wrong", "right"]
    got = p.evaluate("window.got")
    p.close()
    assert got["events"] == [] and got["quiz"] == []

    bad = copy.deepcopy(saved)
    bad["nodes"]["pq"] = {"answers": [None] * 4, "checked": False, "attempt": 0, "done": False}   # a list shape
    q = mount(browser, progress=bad)
    assert q.locator(".evoui-pq-count").inner_text() == "1/4"
    warn = q.evaluate("window.got.warn")
    q.close()
    assert any("pq" in w and "discarded" in w for w in warn)


def test_list_quiz_multi_feedback_hint_and_per_attempt_latency(browser):
    page = mount(browser, block=LIST_BLOCK)
    q0 = page.locator("fieldset").nth(0)
    assert q0.locator("input[type=checkbox]").count() == 3
    assert page.locator("fieldset").nth(1).locator("input[type=radio]").count() == 2
    assert page.locator(".evoui-hint").is_hidden()
    q0.locator(".evoui-hintbtn").click()
    assert page.locator(".evoui-hint").inner_text() == "看个位"
    q0.locator("input[type=checkbox]").nth(0).check()
    page.locator("fieldset").nth(1).locator("input[type=radio]").nth(0).check()
    page.wait_for_timeout(250)
    page.locator(".evoui-check").click()
    assert page.evaluate("[...document.querySelectorAll('fieldset')[0].querySelectorAll('.opt')].map(l => l.className)") == [
        "opt right", "opt", "opt missed"]
    assert page.evaluate("[...document.querySelectorAll('.evoui-optfb')].filter(e => !e.hidden).map(e => e.textContent)") == ["偶"]
    q0.locator("input[type=checkbox]").nth(2).check()          # change → a new attempt, timed from the last check
    page.locator(".evoui-check").click()
    got = page.evaluate("window.got")
    page.close()
    items = [e for e in got["events"] if e["type"] == "item"]
    assert [(e["attempt"], e["item"], e["response"], e["correct"], e["hintUsed"]) for e in items] == [
        (1, 0, [0], False, True), (1, 1, 0, True, False), (2, 0, [0, 2], True, True), (2, 1, 0, True, False)]
    assert [graded(e, LIST_BLOCK) for e in items] == [False, True, True, True]
    first, second = items[0]["latencyMs"], items[2]["latencyMs"]
    assert first >= 250 and second < first
    summaries = [e for e in got["events"] if e["type"] == "summary"]
    assert [s["hintsUsed"] for s in summaries] == [1, 1]
    assert [r["question"] for r in got["quiz"]] == [1, 1]       # single-answer questions only


def test_results_only_when_every_question_is_judged(browser):
    """A (restored) progress on the last page with earlier pages unjudged: 查看结果 goes to the first
    unjudged question instead of the results page; a results phase with unjudged questions is refused."""
    page = mount(browser)
    answer(page, 2)
    saved = page.evaluate("h.flush()")
    page.close()
    odd = copy.deepcopy(saved)
    pq = odd["nodes"]["pq"]
    pq.update(current=3, answers=[None, None, None, 1], judged=[False, False, False, True], done=False)
    p = mount(browser, progress=odd)
    assert p.locator(".evoui-pq-count").inner_text() == "4/4" and p.evaluate("window.got.warn") == []
    f(p, "next").click()
    assert p.locator(".evoui-pq-results").count() == 0
    assert p.locator(".evoui-pq-count").inner_text() == "1/4"
    p.close()
    odd["nodes"]["pq"]["phase"] = "results"
    q = mount(browser, progress=odd)
    assert q.locator(".evoui-pq-results").count() == 0
    warn = q.evaluate("window.got.warn")
    q.close()
    assert any("pq" in w and "discarded" in w for w in warn)


def test_paged_results_show_the_case_diagnosis(browser):
    quiz = copy.deepcopy(PAGED)
    quiz["cases"] = [{"id": "prob", "label": "概率", "advice": "回看补集"}, {"id": "num", "label": "数论"}]
    for i, c in enumerate(["prob", "num", "num", "prob"]):
        quiz["questions"][i]["case"] = c
    block = {**BLOCK, "body": quiz}
    assert validate(block) == []
    page = mount(browser, block=block)
    for r in [1, [0, 2], 0, 1]:                              # q0 (概率) wrong
        answer(page, r)
    lines = page.locator(".evoui-pq-case")
    got = [(lines.nth(i).inner_text(), lines.nth(i).get_attribute("data-ok")) for i in range(lines.count())]
    f(page, "wrong").click()                                 # the wrong-only round counts its own questions
    answer(page, 2)
    again = page.locator(".evoui-pq-case").all_inner_texts()
    page.close()
    assert got == [("概率：1 / 2 — 回看补集", "false"), ("数论：2 / 2", "true")]
    assert again == ["概率：1 / 1"]


def test_confetti_reads_the_colours_set_on_body(browser):
    page = browser.new_page()
    page.set_content("<!doctype html><meta charset=utf-8><body style='--viz-1:#010203;--known:#0a0b0c'>"
                     "<div id=m></div></body>")
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate("""() => { window.styles = new Set(); const P = CanvasRenderingContext2D.prototype, f = P.fillRect;
      P.fillRect = function (...a) { styles.add(this.fillStyle); return f.apply(this, a); }; }""")
    page.evaluate(SETUP, [BLOCK, None])
    for r in RIGHT:
        answer(page, r)
    page.wait_for_timeout(400)
    styles = page.evaluate("[...window.styles]")
    page.close()
    assert "#010203" in styles and "#0a0b0c" in styles
