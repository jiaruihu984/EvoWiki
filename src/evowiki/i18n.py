"""Instance based gettext. Language is explicit; context is local to an async task/thread."""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from gettext import translation
from pathlib import Path

_LANGUAGE: ContextVar[str] = ContextVar("evowiki_language", default="zh")


def current_language() -> str:
    return _LANGUAGE.get()


@contextmanager
def language_context(lang: str) -> Iterator[None]:
    token = _LANGUAGE.set("en" if lang == "en" else "zh")
    try:
        yield
    finally:
        _LANGUAGE.reset(token)


@lru_cache(maxsize=2)
def _catalog(lang: str):
    return translation("evowiki", Path(__file__).parent / "locales",
                       languages=["en" if lang == "en" else "zh"])


def tr(message: str, *, lang: str = "zh", **params: object) -> str:
    if not message:
        return ""
    text = _catalog(lang).gettext(message)
    return text.format(**params) if params else text
