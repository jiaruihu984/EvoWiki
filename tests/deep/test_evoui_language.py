from evowiki.deep.evoui import JS_PATH
from tests.deep.test_evoui_flashcards import BLOCK
from tests.deep.test_evoui_motion_nodes import browser  # noqa: F401


def test_runtime_language_switch_preserves_dom_progress_and_source_text(browser):  # noqa: F811
    page = browser.new_page()
    page.set_content("<!doctype html><meta charset=utf-8><div id=m></div>")
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate("b => { window.handle = EvoUI.render(b, document.getElementById('m'), {language:'en'}); }", BLOCK)
    assert page.get_by_role("button", name="Shuffle", exact=True).count() == 1
    assert "标准差" in page.locator("#m").inner_text()
    page.locator("[data-f=stage]").focus()
    page.keyboard.press("Space")
    page.evaluate("() => { window.original = document.querySelector('.evoui'); window.before = handle.progress(); handle.setLanguage('zh'); }")
    assert page.get_by_role("button", name="洗牌", exact=True).count() == 1
    assert page.evaluate("() => original === document.querySelector('.evoui')")
    assert page.evaluate("() => JSON.stringify(before) === JSON.stringify(handle.progress())")
    page.evaluate("() => handle.setLanguage('en')")
    assert page.get_by_role("button", name="Shuffle", exact=True).count() == 1
    assert "标准差" in page.locator("#m").inner_text()
    page.close()
