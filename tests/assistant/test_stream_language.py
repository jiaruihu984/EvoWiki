from contextvars import copy_context
from types import SimpleNamespace

from evowiki.assistant.loop import _turn_language
from evowiki.i18n import current_language, language_context


def test_stream_language_is_bound_per_advance_and_restored_on_close():
    seen = []

    @_turn_language
    def stream(deps, info):
        try:
            yield current_language()
            yield current_language()
        finally:
            seen.append(current_language())

    deps = SimpleNamespace(data=SimpleNamespace(language=lambda uid: uid))
    english = stream(deps, SimpleNamespace(user_id="en"))
    chinese = stream(deps, SimpleNamespace(user_id="zh"))
    with language_context("zh"):
        assert copy_context().run(next, english) == "en"
        assert current_language() == "zh"
        assert copy_context().run(next, chinese) == "zh"
        assert copy_context().run(next, english) == "en"
        assert current_language() == "zh"
        copy_context().run(english.close)
        copy_context().run(chinese.close)
    assert seen == ["en", "zh"]
