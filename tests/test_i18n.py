from concurrent.futures import ThreadPoolExecutor

from evowiki.i18n import current_language, language_context, tr


def test_gettext_catalog_and_isolated_contexts():
    message = "The action failed. Try again."
    def render(lang):
        with language_context(lang):
            return current_language(), tr(message, lang=lang)
    with ThreadPoolExecutor(max_workers=2) as executor:
        zh, en = list(executor.map(render, ("zh", "en")))
    assert zh == ("zh", "操作失败，请重试")
    assert en == ("en", message)
    assert current_language() == "zh"
