"""The EvoUI block render check in a real headless Chromium (skipped, saying why, without it)."""

import copy

import pytest

from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.render import PlaywrightChecker, block_probes
from tests.deep.test_evoui_flashcards import BLOCK as FLASH_BLOCK
from tests.deep.test_evoui_parity import BASE
from tests.deep.test_evoui_quiz_paged import BLOCK as PAGED_BLOCK


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


def block(**kw) -> EvoUIBlock:
    return EvoUIBlock.model_validate({**copy.deepcopy(BASE), **kw})


def test_auto_probes_are_two_values_per_control():
    b = block(probes=())
    assert block_probes(b) == [({"deg": 0}, {"deg": 180}), ({"on": True}, {"on": False}),
                               ({"mode": "a"}, {"mode": "b"})]
    assert block_probes(block()) == [({}, {"deg": 90})]


def test_a_working_block_passes(checker):
    r = checker.check_block("angle-lab", block())
    assert r.ok, r
    assert (r.interactive_id, r.probes_passed, r.probes_total, r.network_requests,
            r.overflow_px) == ("angle-lab", 1, 1, 0, 0)


def test_a_block_failing_at_runtime_shows_its_fallback_and_fails(checker):
    r = checker.check_block("broken", block(derived={**BASE["derived"], "bad": "xs[9].length"}))
    assert not r.ok and any("fallback" in e for e in r.page_errors), r


def test_a_control_that_changes_nothing_fails_its_probe(checker):
    b = copy.deepcopy(BASE)
    b["state"]["unused"] = 1
    b["body"]["children"].insert(0, {"kind": "slider", "bind": "unused", "min": 0, "max": 9,
                                     "step": 1, "label": "没用的滑块"})
    b.pop("probes")
    r = checker.check_block("dead-control", EvoUIBlock.model_validate(b))
    # the new slider changes nothing, and neither does BASE's `mode` choice (nothing reads it)
    assert not r.ok and (r.probes_total, r.probes_passed) == (4, 2), r
    assert "unused" in r.detail and "mode" in r.detail and "'on'" not in r.detail


def test_a_static_quiz_passes_with_no_probes(checker):
    quiz = {"type": "evoui", "title": "检查一下", "question": "q", "state": {}, "derived": {},
            "body": {"kind": "quiz", "questions": [{"q": "1+1？", "options": ["1", "2"],
                                                    "correct": 1, "why": "就是 2"}]},
            "fallback": "1+1=2"}
    r = checker.check_block("quiz", EvoUIBlock.model_validate(quiz))
    assert r.ok and r.probes_total == 0, r


def test_quiz_results_reach_the_host_hook(checker):
    from playwright.sync_api import sync_playwright

    from evowiki.deep.evoui import JS_PATH
    quiz = {"type": "evoui", "title": "检查一下", "question": "q", "state": {}, "derived": {},
            "body": {"kind": "quiz", "questions": [{"q": "1+1？", "options": ["1", "2"],
                                                    "correct": 1, "why": "就是 2"}]},
            "fallback": "1+1=2"}
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.set_content("<!doctype html><meta charset=utf-8><div id=m></div>")
        page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
        page.evaluate("q => { window.got = []; EvoUI.render(q, document.getElementById('m'),"
                      " {onQuizResult: r => window.got.push(r)}); }", quiz)
        page.locator("input[type=radio]").nth(0).check()
        page.locator(".evoui-check").click()
        got = page.evaluate("window.got")
        b.close()
    assert got == [{"quiz": 0, "question": 0, "chosen": 0, "correct": False, "case": None,
                    "cell": None}]


def test_a_v1_block_renders_at_both_widths_and_its_probes_change_text(checker):
    from tests.deep.test_evoui_parity import V1
    b = EvoUIBlock.model_validate({**copy.deepcopy(V1), "probes": [
        {"set": {"r": 10}}, {"set": {"n": 30}}, {"set": {"show": False}}, {"set": {"p": 5000}}]})
    r = checker.check_block("compound", b)
    assert r.ok, r
    assert (r.probes_passed, r.probes_total, r.network_requests, r.overflow_px) == (4, 4, 0, 0)


def test_v1_controls_quiz_cases_and_the_results_handle():
    """input clamps, select / checkbox / button set state, the diagnostic quiz tallies per case,
    results() returns the answers and the final state, math falls back to its source."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")

    from evowiki.deep.evoui import JS_PATH
    from tests.deep.test_evoui_parity import V1
    with sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium not available ({type(exc).__name__})")
        page = br.new_page()
        page.set_content("<!doctype html><meta charset=utf-8><div id=m></div>")
        page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
        page.evaluate("b => { window.got = []; window.h = EvoUI.render(b, document.getElementById('m'),"
                      " {onQuizResult: r => window.got.push(r)}); }", V1)
        page.locator(".evoui-input input").nth(1).fill("99")          # r: clamped to 20
        page.locator(".evoui-input input").nth(1).dispatch_event("change")
        page.locator(".evoui-select select").select_option("2")        # n = 30
        page.locator(".evoui-btn").click()                             # n = 30, r = r + 1 = 21
        page.locator(".evoui-toggle input").first.uncheck()             # show = false
        radios = page.locator("input[type=radio]")
        radios.nth(0).check()                                           # q0 wrong (rate)
        radios.nth(2).check()                                           # q1 right (time)
        page.locator(".evoui-check").click()
        out = page.evaluate("({res: window.h.results(), got: window.got,"
                            " sum: document.querySelector('.evoui-quizsum').innerText,"
                            " math: document.querySelector('.evoui-math').innerText,"
                            # a branch the `if` just left may still fade out (120 ms, `data-leaving`)
                            " tables: [...document.querySelectorAll('.evoui-table')]"
                            ".filter(t => !t.closest('[data-leaving]')).length})")
        br.close()
    st = out["res"]["state"]
    assert (st["r"], st["n"], st["show"]) == (21, 30, False)
    assert out["res"]["answers"] == [{"quiz": 0, "question": 0, "chosen": 0},
                                     {"quiz": 0, "question": 1, "chosen": 0}]
    assert [(g["case"], g["cell"], g["correct"]) for g in out["got"]] == [
        ("rate", "c1.interpret", False), ("time", None, True)]
    assert "答对 1 / 2" in out["sum"] and "利率：0 / 1 — 回去看利率那段" in out["sum"]
    assert "时间：1 / 1" in out["sum"]
    assert out["math"] == "FV = P(1+r)^n" and out["tables"] == 1      # the if-table is hidden


def test_the_render_check_passes_a_paged_quiz(checker):
    """A paged quiz (no controls, no probes) passes on the rendering checks at 375 and 1280."""
    r = checker.check_block("paged-quiz", EvoUIBlock.model_validate(PAGED_BLOCK))
    assert r.ok, r
    assert (r.network_requests, r.overflow_px, r.probes_total, r.page_errors) == (0, 0, 0, ())


def test_the_render_check_passes_flashcards(checker):
    """A flashcards block (no controls, no probes) passes on the rendering checks at 375 and 1280."""
    r = checker.check_block("flashcards", EvoUIBlock.model_validate(FLASH_BLOCK))
    assert r.ok, r
    assert (r.network_requests, r.overflow_px, r.probes_total, r.page_errors) == (0, 0, 0, ())


def test_the_render_check_passes_every_task_5a_node(checker):
    """The bubble-sort demo (player, plot glide, transition, if, reveal steps / after, animate, timer,
    celebrate, paged quiz) passes at 375 and 1280; its player is probed min -> max like a slider."""
    from tests.deep.test_evoui_motion_nodes import DEMO
    b = EvoUIBlock.model_validate(DEMO)
    assert block_probes(b) == [({"step": 0}, {"step": 28})]
    r = checker.check_block("bubble-sort", b)
    assert r.ok, r
    assert (r.network_requests, r.overflow_px, r.probes_total, r.probes_passed, r.page_errors) == (0, 0, 1, 1, ())


def test_the_render_check_passes_every_task_5b_node(checker):
    """Route (bound: probed first -> last option like a choice) + a gated pager holding a fill-blank, a
    sentence builder and a paged quiz pass at 375 and 1280."""
    from tests.deep.test_evoui_practice_nodes import DEMO
    b = EvoUIBlock.model_validate(DEMO)
    assert block_probes(b) == [({"mode": "guided"}, {"mode": "quiz"})]
    r = checker.check_block("practice", b)
    assert r.ok, r
    assert (r.network_requests, r.overflow_px, r.probes_total, r.probes_passed, r.page_errors) == (0, 0, 1, 1, ())


def test_the_render_check_passes_a_timeline_and_opens_every_deep_dive(checker):
    """A timeline (dated and numbered) and a deep-dive pass at 375 and 1280. The check opens the deep-dive first:
    the only text the slider changes is inside it, so its probe passes only because the part was opened."""
    b = EvoUIBlock.model_validate({
        "type": "evoui", "title": "相对论的来龙去脉", "question": "这几件事是按什么顺序发生的？", "fallback": "1905 狭义，1915 广义。",
        "state": {"n": 1}, "derived": {}, "body": {"kind": "box", "children": [
            {"kind": "slider", "bind": "n", "min": 1, "max": 5, "step": 1, "label": "看第几项"},
            {"kind": "timeline", "events": [{"when": "1905", "title": "狭义相对论", "text": "光速不变\n时间会变慢",
                                             "tone": "accent"}, {"when": "1915", "title": "广义相对论"}]},
            {"kind": "timeline", "events": [{"title": "读题"}, {"title": "列式", "text": "把已知量代进去"}]},
            {"kind": "deep-dive", "label": "深入一点：为什么光速不变", "children": [
                {"kind": "text", "text": "你选的是第 {n} 项。"}, {"kind": "text", "text": "迈克耳孙–莫雷实验没有测到以太风。"}]}]}})
    r = checker.check_block("timeline", b)
    assert r.ok, r
    assert (r.network_requests, r.overflow_px, r.probes_total, r.probes_passed, r.page_errors) == (0, 0, 1, 1, ())
