"""EvoUI v2 Task 4 in headless Chromium (skipped, saying why, without it): the flashcards node (ChatGPT
LearningFlashcards parity) driven by keyboard and by pointer (mouse and touch): flip, know / missed,
drag lighting, click-after-drag suppression, undo, shuffle, summary rounds, request, confetti, progress
restore and the events. Every emitted item event is also graded by the backend grader."""

import copy

import pytest

from evowiki.deep.evoui import JS_PATH, quiz_cells, validate
from evowiki.wikiapp.block_results import _grade_item
from evowiki.wikiapp.wiki_schemas import BlockEventIn

CARDS = {"kind": "flashcards", "id": "fc", "title": "离散程度", "cards": [
    {"front": "标准差", "back": "方差的平方根", "hint": "平均离多远", "cell": "c1.interpret"},
    {"front": "σ", "back": "总体标准差的符号"},
    {"front": "z 分数", "back": "z = (x − μ) / σ\n离均值几个标准差", "cell": "c2.define"},
    {"front": "μ ± 1σ", "back": "约 68%"}]}
BLOCK = {"type": "evoui", "title": "闪卡", "question": "记得住吗？", "state": {}, "derived": {}, "body": CARDS,
         "fallback": "标准差是方差的平方根。"}

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


def mount(browser, block=BLOCK, progress=None, reduced=False, width=420):
    page = browser.new_page(reduced_motion="reduce" if reduced else "no-preference",
                            viewport={"width": width, "height": 700})
    page.set_content("<!doctype html><meta charset=utf-8><body><div id=m></div></body>")
    page.add_style_tag(content=(JS_PATH.parent / "evoui.css").read_text(encoding="utf-8"))
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate(SETUP, [block, progress])
    return page


def f(page, name):
    return page.locator(f"[data-f='{name}']")


def front(page):
    return page.locator(".evoui-fc-card .evoui-fc-face.front .evoui-fc-text").inner_text()


def flipped(page):
    return page.evaluate("document.querySelector('.evoui-fc-card').classList.contains('flipped')")


def node(page):
    return page.evaluate("h.progress().nodes.fc")


def events(page):
    return page.evaluate("window.got.events")


def rated(page):
    """The round's ratings so far, from the progress history: [(card, "know" | "missed")]."""
    return [(x["card"], "know" if x["dir"] == "right" else "missed") for x in node(page)["history"]]


def lit(page):
    return page.evaluate("""() => ['miss', 'know'].map(k => { const b = document.querySelector(`[data-f=${k}]`);
      return [Number(b.style.getPropertyValue('--lit') || 0), b.dataset.hot === 'true']; })""")


def graded(ev, block=BLOCK):
    n = block["body"]
    return _grade_item(BlockEventIn.model_validate(ev), n, {id(n): 0}).correct


def card_center(page):
    b = page.locator(".evoui-fc-card").bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def drag(page, dx, *, release=True, steps=8, dy=0):
    x, y = card_center(page)
    page.mouse.move(x, y)
    page.mouse.down()
    for i in range(1, steps + 1):
        page.mouse.move(x + dx * i / steps, y + dy * i / steps)
    if release:
        page.mouse.up()


def touch_swipe(page, dx, dy=0):
    """A touch swipe as Pointer Events (pointerType touch) on the card."""
    page.evaluate("""([dx, dy]) => {
      const c = document.querySelector('.evoui-fc-card'), r = c.getBoundingClientRect();
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      const ev = (type, k) => c.dispatchEvent(new PointerEvent(type, {pointerId: 7, pointerType: 'touch', isPrimary: true,
        button: type === 'pointermove' ? -1 : 0, buttons: type === 'pointerup' ? 0 : 1, clientX: x + dx * k, clientY: y + dy * k,
        bubbles: true, cancelable: true}));
      ev('pointerdown', 0); for (let i = 1; i <= 8; i++) ev('pointermove', i / 8); ev('pointerup', 1);
    }""", [dx, dy])


def test_the_block_is_valid_and_card_cells_count_as_k_cells():
    assert validate(BLOCK) == []
    assert quiz_cells(BLOCK) == {"c1.interpret", "c2.define"}


def test_keyboard_flip_know_missed_undo(browser):
    page = mount(browser)
    assert front(page) == "标准差" and page.locator(".evoui-fc-count").inner_text() == "1/4"
    assert page.locator(".evoui-fc-title").inner_text() == "离散程度"
    assert f(page, "undo").is_disabled()
    page.keyboard.press("ArrowRight")                          # focus is not in the deck: nothing
    assert events(page) == [] and node(page)["index"] == 0
    f(page, "stage").focus()
    page.wait_for_timeout(60)
    page.keyboard.press("Space")
    assert flipped(page) and node(page)["flipped"] is True
    assert page.evaluate("document.querySelector('.evoui-fc-face.back').getAttribute('aria-hidden')") == "false"
    page.keyboard.press("ArrowLeft")                           # missed
    assert lit(page) == [[1, True], [0, False]]                 # ✗ lit right after the commit …
    page.wait_for_timeout(330)
    assert lit(page) == [[0, False], [0, False]]                # … and only for 240 ms
    assert front(page) == "σ" and not flipped(page) and page.locator(".evoui-fc-count").inner_text() == "2/4"
    page.keyboard.press("ArrowRight")                          # know
    page.wait_for_timeout(300)
    p = node(page)
    assert events(page) == []                                  # ratings are sent when the round ends
    assert rated(page) == [(0, "missed"), (1, "know")] and p["history"][0]["ms"] >= 60
    assert (p["index"], p["missed"], [x["dir"] for x in p["history"]]) == (2, [0], ["left", "right"])
    assert p["history"][0]["flipped"] is True and p["history"][1]["flipped"] is False
    page.keyboard.press("z")                                   # undo the know: card 1 comes back
    page.wait_for_timeout(350)
    assert front(page) == "σ" and node(page)["index"] == 1
    page.keyboard.press("z")                                   # undo the missed: card 0, flipped as it was
    page.wait_for_timeout(350)
    p = node(page)
    assert front(page) == "标准差" and flipped(page) and (p["index"], p["missed"], p["history"]) == (0, [], [])
    assert f(page, "undo").is_disabled() and events(page) == []
    page.close()


def test_pointer_drag_lights_buttons_commits_and_ignores_the_click_after(browser):
    page = mount(browser)
    drag(page, -40, release=False)
    (miss, miss_hot), (know, _) = lit(page)
    assert 0.2 < miss < 0.5 and not miss_hot and know == 0     # lit in proportion, not yet hot
    assert page.evaluate("document.querySelector('.evoui-fc-card').dataset.edge") == "left"
    assert "translate(-40px" in page.evaluate("document.querySelector('.evoui-fc-card').style.transform")
    page.mouse.up()                                            # short drag: springs back, no rating
    assert node(page)["index"] == 0 and not flipped(page)      # and the click that followed is ignored
    page.wait_for_timeout(400)
    assert lit(page) == [[0, False], [0, False]]
    assert page.evaluate("document.querySelector('.evoui-fc-card').style.transform") == ""
    page.locator(".evoui-fc-card").click()                     # a plain click flips
    assert flipped(page)
    drag(page, 160)                                            # far right: know
    assert lit(page)[1] == [1, True]
    f(page, "miss").click()                                    # within 350 ms of the drag: ignored
    page.wait_for_timeout(400)
    assert rated(page) == [(0, "know")]
    assert front(page) == "σ"
    drag(page, -200)                                           # far left: missed
    page.wait_for_timeout(400)
    assert rated(page) == [(0, "know"), (1, "missed")]
    page.close()


def test_touch_swipes_and_vertical_gestures(browser):
    page = mount(browser)
    touch_swipe(page, 0, 120)                                  # vertical: left to the page
    page.wait_for_timeout(50)
    assert node(page)["index"] == 0
    touch_swipe(page, 170)
    page.wait_for_timeout(400)
    touch_swipe(page, -170)
    page.wait_for_timeout(400)
    assert rated(page) == [(0, "know"), (1, "missed")]
    page.close()


def test_shuffle_reorders_only_the_unrated_part(browser):
    deck = copy.deepcopy(BLOCK)
    deck["body"]["cards"] = [{"front": f"f{i}", "back": f"b{i}"} for i in range(8)]
    page = mount(browser, block=deck)
    f(page, "stage").focus()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowLeft")
    for _ in range(12):
        before = node(page)
        f(page, "shuffle").click()
        page.wait_for_timeout(5)
        after = node(page)
        assert after["order"][:2] == before["order"][:2]                     # rated cards stay
        assert sorted(after["order"]) == list(range(8))
        assert after["order"][2] != before["order"][2]                       # never the current card first
        assert front(page) == f"f{after['order'][2]}"
    assert page.locator(".evoui-fc-stage.shuffling").count() == 1          # the 300 ms shuffle motion
    page.wait_for_timeout(350)
    assert page.locator(".evoui-fc-stage.shuffling").count() == 0
    for _ in range(5):
        page.keyboard.press("ArrowRight")
    assert f(page, "shuffle").is_disabled()                                   # one card left
    page.close()
    plain = copy.deepcopy(BLOCK)
    plain["body"]["shuffle"] = False
    q = mount(browser, block=plain)
    assert f(q, "shuffle").count() == 0
    q.close()


def test_rounds_summary_review_missed_more_and_confetti(browser):
    page = mount(browser)
    f(page, "stage").focus()
    for k in ["ArrowRight", "ArrowLeft", "ArrowRight", "ArrowLeft"]:          # cards 1 and 3 missed
        page.keyboard.press(k)
    page.wait_for_timeout(400)
    assert page.locator(".evoui-fc-sum").count() == 1 and page.locator(".evoui-fc-deck").is_hidden()
    assert page.locator(".evoui-fc-verdict").inner_text() == "记住了 50%"
    assert page.locator(".evoui-fc-chip").all_inner_texts() == ["2", "2"]
    assert f(page, "review").inner_text() == "复习 2 张没记住的" and f(page, "more").count() == 0
    assert page.locator("canvas.evoui-confetti").count() == 0
    summary = events(page)[-1]
    assert {k: summary[k] for k in ("type", "node", "kind", "attempt", "total", "right", "missed", "hintsUsed")} == {
        "type": "summary", "node": "fc", "kind": "flashcard", "attempt": 1, "total": 4, "right": 2,
        "missed": [1, 3], "hintsUsed": 0}
    assert summary["timeMs"] == sum(e["latencyMs"] for e in events(page)[:-1])
    BlockEventIn.model_validate(summary)
    assert page.evaluate("document.activeElement.dataset.f") == "review"   # focus stays in the node
    page.keyboard.press("Enter")                               # review the 2 missed: attempt 2
    p = node(page)
    assert (p["attempt"], p["round"], p["order"], p["phase"]) == (2, "missed", [1, 3], "cards")
    assert front(page) == "σ" and page.locator(".evoui-fc-count").inner_text() == "1/2"
    assert page.evaluate("document.activeElement.dataset.f") == "stage"
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(450)
    assert page.locator(".evoui-fc-verdict").inner_text() == "全部记住了"
    assert page.locator(".evoui-fc-round").inner_text() == "第 2 轮 · 复习没记住的"
    assert page.locator("canvas.evoui-confetti").count() == 1
    f(page, "more").click()
    assert f(page, "more").is_disabled() and f(page, "more").inner_text() == "已请求更多卡片"
    got = events(page)
    att2 = [e for e in got if e.get("attempt") == 2]
    assert [(e["type"], e.get("item"), e.get("response")) for e in att2] == [
        ("item", 1, "know"), ("item", 3, "know"), ("summary", None, None)]
    req = got[-1]
    assert {k: req[k] for k in ("type", "block", "node", "what")} == {"type": "request", "block": "blk", "node": "fc",
                                                                      "what": "more"}
    BlockEventIn.model_validate(req)
    f(page, "restart").click()                                  # all cards again: attempt 3
    p = node(page)
    assert (p["attempt"], p["round"], p["order"], p["done"]) == (3, "all", [0, 1, 2, 3], True)
    page.close()


def test_hint_counts_as_hint_used(browser):
    page = mount(browser)
    assert f(page, "hint").is_visible()
    f(page, "stage").focus()
    page.keyboard.press("h")
    assert page.locator(".evoui-fc-cardhint").inner_text() == "平均离多远"
    assert f(page, "hint").get_attribute("aria-pressed") == "true"
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    assert f(page, "hint").is_hidden()                         # card 2 has no hint
    for _ in range(3):
        page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    got = events(page)
    page.close()
    assert [e["hintUsed"] for e in got if e["type"] == "item"] == [True, False, False, False]
    assert got[-1]["hintsUsed"] == 1


def test_fast_input_is_never_lost_to_an_animation(browser):
    page = mount(browser)
    f(page, "stage").focus()
    for k in ["ArrowRight", "ArrowLeft", "z", "ArrowRight", "ArrowRight"]:
        page.keyboard.press(k)                                  # far inside 240 ms each
    p = node(page)
    assert (p["index"], [x["dir"] for x in p["history"]]) == (3, ["right", "right", "right"])
    page.wait_for_timeout(300)
    assert front(page) == "μ ± 1σ"
    page.close()


def test_the_first_card_tip_rotates_and_leaves_after_an_action(browser):
    page = mount(browser)
    tip = page.locator(".evoui-fc-tip")
    assert tip.is_visible() and tip.inner_text() == "点击翻面"
    page.wait_for_timeout(3300)
    assert tip.inner_text() == "左滑没记住"
    page.locator(".evoui-fc-card").click()
    page.locator(".evoui-fc-card").click()                     # back to the front: the tip stays gone
    assert tip.is_hidden()
    page.close()


def test_progress_restores_without_events_and_misfits_are_discarded(browser):
    page = mount(browser)
    f(page, "stage").focus()
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Space")
    page.wait_for_timeout(50)
    saved = page.evaluate("h.flush()")
    page.close()
    p = saved["nodes"]["fc"]
    assert {k: p[k] for k in ("order", "index", "flipped", "missed", "phase", "round", "attempt", "hint", "hintUsed",
                              "done", "celebrated", "requested")} == {
        "order": [0, 1, 2, 3], "index": 2, "flipped": True, "missed": [0], "phase": "cards", "round": "all",
        "attempt": 1, "hint": False, "hintUsed": [], "done": False, "celebrated": 0, "requested": 0}
    assert [(x["card"], x["dir"], x["flipped"]) for x in p["history"]] == [(0, "left", False), (1, "right", False)]

    again = mount(browser, progress=saved)
    assert front(again) == "z 分数" and flipped(again) and again.locator(".evoui-fc-count").inner_text() == "3/4"
    assert again.evaluate("h.progress().revision") == saved["revision"]
    assert not again.locator(".evoui-fc-tip").is_visible()
    again.wait_for_timeout(300)
    assert again.evaluate("window.got") == {"events": [], "warn": [], "progress": []}
    f(again, "stage").focus()
    again.keyboard.press("z")                                   # history survives the reload
    assert front(again) == "σ"
    again.close()

    done = copy.deepcopy(saved)
    d = done["nodes"]["fc"]
    d.update(index=4, flipped=False, phase="summary", done=True, celebrated=1, missed=[],
             history=[{"card": i, "flipped": False, "dir": "right", "ms": 10} for i in range(4)])
    s = mount(browser, progress=done)
    s.wait_for_timeout(300)
    assert s.locator(".evoui-fc-verdict").inner_text() == "全部记住了"
    assert s.locator("canvas.evoui-confetti").count() == 0      # never on restore
    assert s.evaluate("window.got") == {"events": [], "warn": [], "progress": []}
    s.close()

    bad = copy.deepcopy(saved)
    bad["nodes"]["fc"]["missed"] = [1]                          # disagrees with the history
    b = mount(browser, progress=bad)
    assert front(b) == "标准差" and b.locator(".evoui-fc-count").inner_text() == "1/4"
    warn = b.evaluate("window.got.warn")
    b.close()
    assert any("fc" in w and "discarded" in w for w in warn)


def test_reduced_motion_has_no_confetti_and_no_lingering_motion(browser):
    page = mount(browser, reduced=True)
    f(page, "stage").focus()
    for _ in range(4):
        page.keyboard.press("ArrowRight")
    assert page.locator(".evoui-fc-sum").count() == 1           # no swipe delay
    page.wait_for_timeout(100)
    assert page.locator("canvas.evoui-confetti").count() == 0
    assert page.locator(".evoui-fc-sum.evoui-enter").count() == 0
    page.close()


def test_a_flashcards_node_inside_a_block_with_other_nodes(browser):
    block = {**BLOCK, "body": {"kind": "box", "children": [{"kind": "text", "text": "先翻卡"}, CARDS,
                                                          {"kind": "flashcards", "cards": CARDS["cards"][:2]}]}}
    assert validate(block) == []
    page = mount(browser, block=block)
    assert page.locator(".evoui-fc").count() == 2
    page.locator("[data-f=stage]").nth(1).focus()
    page.keyboard.press("ArrowRight")                          # only the focused deck reacts
    first, second = page.evaluate("[h.progress().nodes.fc.index, h.progress().nodes['flashcards#1'].index]")
    page.close()
    assert (first, second) == (0, 1)


def test_item_events_are_sent_once_per_card_when_the_round_ends(browser):
    """Undo + re-rate never double-counts: one item event per card with its final rating, sent just
    before the summary; the batch passes the backend grader with distinct dedup keys."""
    from evowiki.wikiapp.block_results import event_key
    page = mount(browser)
    f(page, "stage").focus()
    page.wait_for_timeout(60)
    page.keyboard.press("ArrowLeft")                           # card 0 missed …
    page.keyboard.press("z")                                   # … undone …
    page.keyboard.press("h")
    page.keyboard.press("ArrowRight")                          # … and re-rated know (hint used)
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowRight")
    assert events(page) == []
    page.keyboard.press("ArrowLeft")                           # the last card ends the round
    got = events(page)
    page.close()
    items = [e for e in got if e["type"] == "item"]
    assert [e["type"] for e in got] == ["item"] * 4 + ["summary"]
    assert [(e["item"], e["response"], e["correct"], e["hintUsed"]) for e in items] == [
        (0, "know", True, True), (1, "missed", False, False), (2, "know", True, False), (3, "missed", False, False)]
    first = items[0]
    assert {k: first[k] for k in ("type", "block", "node", "kind", "attempt")} == {
        "type": "item", "block": "blk", "node": "fc", "kind": "flashcard", "attempt": 1}
    assert all(isinstance(e["latencyMs"], int) and e["latencyMs"] >= 0 for e in items)
    assert got[-1]["missed"] == [1, 3] and got[-1]["timeMs"] == sum(e["latencyMs"] for e in items)
    assert [graded(e) for e in items] == [True, False, True, False]
    keys = [event_key(BlockEventIn.model_validate(e)) for e in got]
    assert len(set(keys)) == len(keys)
