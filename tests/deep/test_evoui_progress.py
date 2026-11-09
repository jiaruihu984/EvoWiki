"""EvoUI v2 result protocol in headless Chromium (skipped, saying why, without it): the renderer's
serializable progress object (restore, revision, debounced onProgress, flush) and its result events
(item + summary, the unsent log with ack), driven through the list quiz."""

import copy
import itertools
import re

import pytest

from evowiki.deep.evoui import JS_PATH, validate
from evowiki.wikiapp.block_results import event_key
from evowiki.wikiapp.wiki_schemas import BlockEventIn

QUIZ_A = {"kind": "quiz", "id": "first", "questions": [
    {"q": "1+1？", "options": ["1", "2", "3"], "correct": 1, "why": "就是 2"},
    {"q": "2+2？", "options": ["4", "5"], "correct": 0, "why": "就是 4"}]}
QUIZ_B = {"kind": "quiz", "questions": [{"q": "3+3？", "options": ["6", "7"], "correct": 0, "why": "6"}]}
BLOCK = {"type": "evoui", "title": "加法", "question": "会加吗？", "state": {"n": 1, "on": True},
         "derived": {}, "body": {"kind": "box", "children": [
             {"kind": "slider", "bind": "n", "min": 0, "max": 9, "step": 1, "label": "n = {n}"},
             {"kind": "toggle", "bind": "on", "label": "显示"},
             QUIZ_A,
             {"kind": "if", "cond": "on", "then": QUIZ_B, "else": None}]},
         "fallback": "1+1=2"}
ITEM_FIELDS = {"type", "block", "session", "node", "kind", "item", "attempt", "revision", "response", "correct",
               "hintUsed", "latencyMs"}
SUMMARY_FIELDS = {"type", "block", "session", "node", "kind", "attempt", "revision", "total", "right", "missed",
                  "timeMs", "hintsUsed"}

SETUP = """([b, p]) => {
  window.got = {events: [], progress: [], quiz: [], warn: []};
  const w = console.warn; console.warn = (...a) => { window.got.warn.push(a.join(' ')); w(...a); };
  const opts = {blockId: 'blk-1', onEvent: e => got.events.push(e), onProgress: x => got.progress.push(x),
                onQuizResult: r => got.quiz.push(r)};
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


def mount(browser, block=BLOCK, progress=None):
    page = browser.new_page()
    page.set_content("<!doctype html><meta charset=utf-8><div id=m></div>")
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate(SETUP, [block, progress])
    return page


def quiz(page, i):
    return page.locator(".evoui-quiz").nth(i)


def test_the_block_is_valid():
    assert validate(BLOCK) == []


def test_answering_a_list_quiz_emits_item_and_summary_events(browser):
    page = mount(browser)
    a = quiz(page, 0)
    a.locator("input[type=radio]").nth(1).check()          # q0 right
    a.locator("input[type=radio]").nth(4).check()          # q1 option 1: wrong
    page.wait_for_timeout(30)
    a.locator(".evoui-check").click()
    got = page.evaluate("window.got")
    events = page.evaluate("h.events()")
    page.close()
    assert got["events"] == events
    item0, item1, summary = events
    assert set(item0) == ITEM_FIELDS and set(summary) == SUMMARY_FIELDS
    assert re.fullmatch(r"[0-9a-f]{16}", item0["session"]) and {e["session"] for e in events} == {item0["session"]}
    assert {k: item0[k] for k in ITEM_FIELDS - {"latencyMs", "revision", "session"}} == {
        "type": "item", "block": "blk-1", "node": "first", "kind": "quiz", "item": 0, "attempt": 1,
        "response": 1, "correct": True, "hintUsed": False}
    assert (item1["item"], item1["response"], item1["correct"]) == (1, 1, False)
    assert {k: summary[k] for k in SUMMARY_FIELDS - {"timeMs", "revision", "session"}} == {
        "type": "summary", "block": "blk-1", "node": "first", "kind": "quiz", "attempt": 1,
        "total": 2, "right": 1, "missed": [1], "hintsUsed": 0}
    assert isinstance(item0["latencyMs"], int) and item0["latencyMs"] >= 30
    assert summary["timeMs"] == item0["latencyMs"]
    # the press came after 2 picks: revision 3, the same on every event of that press
    assert {e["revision"] for e in events} == {3}
    # onQuizResult is unchanged
    assert got["quiz"] == [
        {"quiz": 0, "question": 0, "chosen": 1, "correct": True, "case": None, "cell": None},
        {"quiz": 0, "question": 1, "chosen": 1, "correct": False, "case": None, "cell": None}]


def _answer_first_quiz(browser):
    page = mount(browser)
    a = quiz(page, 0)
    a.locator("input[type=radio]").nth(1).check()
    a.locator("input[type=radio]").nth(4).check()
    a.locator(".evoui-check").click()
    events = page.evaluate("h.events()")
    page.close()
    return events


def test_two_renders_without_restored_progress_differ_only_in_their_session(browser):
    """A host that does not restore progress starts attempt / revision over: the same answers in a
    second render send the same node / item / attempt / revision — only `session` tells them apart,
    so the server's duplicate key (block_results.event_key) keeps both."""
    one, two = _answer_first_quiz(browser), _answer_first_quiz(browser)
    def strip(e):
        return {k: v for k, v in e.items() if k not in ("session", "latencyMs", "timeMs")}
    assert [strip(e) for e in one] == [strip(e) for e in two]
    assert one[0]["session"] != two[0]["session"]
    k1 = [event_key(BlockEventIn.model_validate(e)) for e in one]
    k2 = [event_key(BlockEventIn.model_validate(e)) for e in two]
    assert not set(k1) & set(k2) and len(set(k1)) == len(k1)
    assert k1 == [event_key(BlockEventIn.model_validate(e)) for e in one]     # a resend: the same keys


def test_revision_strictly_increases_and_progress_is_debounced(browser):
    page = mount(browser)
    revs = [page.evaluate("h.progress().revision")]
    page.locator(".evoui-slider input").fill("5")               # state change
    revs.append(page.evaluate("h.progress().revision"))
    page.locator(".evoui-toggle input").uncheck()               # state change (hides quiz B)
    revs.append(page.evaluate("h.progress().revision"))
    a = quiz(page, 0)
    a.locator("input[type=radio]").nth(1).check()
    revs.append(page.evaluate("h.progress().revision"))
    a.locator("input[type=radio]").nth(3).check()
    revs.append(page.evaluate("h.progress().revision"))
    a.locator(".evoui-check").click()
    revs.append(page.evaluate("h.progress().revision"))
    assert all(x < y for x, y in itertools.pairwise(revs)), revs
    page.wait_for_timeout(400)
    assert page.evaluate("got.progress.at(-1).revision") == revs[-1]
    # a burst of changes inside the 250 ms debounce is sent once, with the last revision
    burst = page.evaluate("""async () => { got.progress = [];
        h.set('n', 2); h.set('n', 3); h.set('n', 4); const during = got.progress.length;
        await new Promise(r => setTimeout(r, 400)); return [during, got.progress]; }""")
    assert burst[0] == 0 and len(burst[1]) == 1 and burst[1][0]["revision"] == revs[-1] + 3
    revs.append(revs[-1] + 3)
    assert page.evaluate("h.flush(), got.progress.length") == 1  # nothing pending: flush sends nothing
    page.locator(".evoui-slider input").fill("6")
    flushed = page.evaluate("h.flush()")
    after = page.evaluate("got.progress")
    page.close()
    assert len(after) == 2 and after[1] == flushed and flushed["revision"] == revs[-1] + 1
    assert flushed["v"] == 2 and flushed["state"] == {"n": 6, "on": False}
    assert flushed["nodes"] == {"first": {"answers": [1, 0], "checked": True, "attempt": 1, "done": True},
                                "quiz#1": {"answers": [None], "checked": False, "attempt": 0, "done": False}}


def test_an_unchanged_recheck_only_repeats_on_quiz_result_and_a_changed_one_is_attempt_2(browser):
    page = mount(browser)
    b = quiz(page, 1)
    b.locator("input[type=radio]").nth(1).check()
    b.locator(".evoui-check").click()
    b.locator(".evoui-check").click()                            # unchanged: no new events
    assert page.evaluate("[got.events.length, got.quiz.length]") == [2, 2]
    b.locator("input[type=radio]").nth(0).check()
    b.locator(".evoui-check").click()
    events = page.evaluate("h.events()")
    page.evaluate("h.ack(2)")
    left = page.evaluate("h.events()")
    page.close()
    assert [(e["type"], e["node"], e["attempt"]) for e in events] == [
        ("item", "quiz#1", 1), ("summary", "quiz#1", 1), ("item", "quiz#1", 2), ("summary", "quiz#1", 2)]
    assert (events[0]["correct"], events[2]["correct"]) == (False, True)
    assert events[2]["revision"] > events[0]["revision"]
    assert left == events[2:]


def test_reload_with_progress_restores_picks_and_checked_and_emits_nothing(browser):
    page = mount(browser)
    page.locator(".evoui-slider input").fill("7")
    a = quiz(page, 0)
    a.locator("input[type=radio]").nth(0).check()                # q0 wrong
    a.locator("input[type=radio]").nth(3).check()                # q1 right
    a.locator(".evoui-check").click()
    quiz(page, 1).locator("input[type=radio]").nth(1).check()    # picked, not checked
    saved = page.evaluate("h.flush()")
    text = page.locator("#m").inner_text()
    page.close()

    page = mount(browser, progress=saved)
    page.wait_for_timeout(400)
    out = page.evaluate("""() => ({got, events: h.events(), progress: h.progress(),
        radios: [...document.querySelectorAll('input[type=radio]')].map(r => r.checked),
        slider: document.querySelector('.evoui-slider input').value})""")
    restored_text = page.locator("#m").inner_text()
    page.close()
    assert out["got"]["events"] == [] and out["events"] == [] and out["got"]["progress"] == []
    assert out["got"]["quiz"] == [] and out["got"]["warn"] == []
    assert out["progress"] == saved
    assert out["radios"] == [True, False, False, True, False, False, True]
    assert out["slider"] == "7"
    assert restored_text == text and "答对 1 / 2" in restored_text


def test_a_misfit_node_progress_is_discarded_and_the_rest_restored(browser):
    page = mount(browser)
    a = quiz(page, 0)
    a.locator("input[type=radio]").nth(1).check()
    a.locator("input[type=radio]").nth(3).check()
    a.locator(".evoui-check").click()
    quiz(page, 1).locator("input[type=radio]").nth(0).check()
    saved = page.evaluate("h.flush()")
    page.close()
    bad = copy.deepcopy(saved)
    bad["nodes"]["first"]["answers"] = [7, 0]                    # option 7 does not exist
    bad["nodes"]["nobody"] = {"x": 1}                            # unknown node: ignored
    bad["state"]["n"] = "seven"                                  # wrong JSON type: block value kept
    bad["state"]["ghost"] = 1                                    # unknown state: ignored
    page = mount(browser, progress=bad)
    out = page.evaluate("""() => ({got, progress: h.progress(),
        radios: [...document.querySelectorAll('input[type=radio]')].map(r => r.checked),
        sums: [...document.querySelectorAll('.evoui-quizsum')].map(s => s.hidden)})""")
    page.close()
    assert len(out["got"]["warn"]) == 2
    assert any("first" in w for w in out["got"]["warn"]) and any("state n" in w for w in out["got"]["warn"])
    assert out["got"]["events"] == []
    assert out["progress"]["revision"] == saved["revision"]
    assert out["progress"]["state"] == {"n": 1, "on": True}
    assert out["progress"]["nodes"] == {
        "first": {"answers": [None, None], "checked": False, "attempt": 0, "done": False},
        "quiz#1": saved["nodes"]["quiz#1"]}
    assert out["radios"] == [False] * 5 + [True, False]
    assert out["sums"] == [True, True]


def test_a_quiz_in_a_hidden_branch_keeps_its_progress(browser):
    """Progress of a node that is not built (an `if` branch not shown) is carried over unchanged, and a
    rebuilt branch gets its live progress back."""
    page = mount(browser)
    quiz(page, 1).locator("input[type=radio]").nth(1).check()
    page.locator(".evoui-toggle input").uncheck()
    page.locator(".evoui-toggle input").check()
    rebuilt = page.evaluate("[...document.querySelectorAll('input[type=radio]')].slice(5).map(r => r.checked)")
    page.locator(".evoui-toggle input").uncheck()
    saved = page.evaluate("h.flush()")
    page.close()
    assert rebuilt == [False, True]
    page = mount(browser, progress=saved)
    hidden = page.evaluate("h.progress()")
    page.locator(".evoui-toggle input").check()
    shown = page.evaluate("[...document.querySelectorAll('input[type=radio]')].slice(5).map(r => r.checked)")
    page.close()
    assert hidden["nodes"]["quiz#1"] == saved["nodes"]["quiz#1"] and shown == [False, True]


def test_a_bad_progress_object_is_ignored_with_a_warning(browser):
    page = mount(browser, progress={"v": 1, "revision": 9})
    out = page.evaluate("({warn: got.warn, p: h.progress()})")
    page.close()
    assert len(out["warn"]) == 1 and out["p"]["revision"] == 0
    assert out["p"]["nodes"]["first"] == {"answers": [None, None], "checked": False, "attempt": 0,
                                          "done": False}


def test_removable_host_hands_off_native_input_before_deferred_paint(browser):
    page = browser.new_page()
    page.set_content("<!doctype html><div id=m></div>")
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate("""block => {
      window.snapshots = []; window.paints = [];
      window.h = EvoUI.render(block, document.getElementById('m'), {
        immediateProgress: true,
        scheduleUpdate: paint => paints.push(paint),
        onProgress: progress => snapshots.push(progress),
        progress: {v:2, revision:1, state:{n:9,on:true},nodes:{}}
      });
    }""", BLOCK)
    slider = page.locator("input[type=range]")
    slider.focus()
    slider.press("End")
    for _ in range(3):
        slider.press("ArrowLeft")
    assert slider.input_value() == "6"
    observed = page.evaluate("({state:h.state.n, first:snapshots[0], last:snapshots.at(-1), paints:paints.length})")
    assert observed["state"] == observed["last"]["state"]["n"] == 6
    assert observed["first"]["state"]["n"] == 8  # The earlier snapshot must not change with the live renderer state.
    assert observed["paints"] == 3
    page.evaluate("h.destroy()")
    assert page.evaluate("snapshots.at(-1).state.n") == 6
    page.close()
