"""Shared fakes for the toc / chunk / summary tests (no real LLM)."""

from itertools import count

from evowiki.sources.parsed import OutlineEntry, PageText, ParsedDoc


class FakeLLM:
    """Scripted JsonLLM: each item is a model instance to return or an exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, role, purpose, prompt, schema):
        self.calls.append((role, purpose, prompt))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def id_gen(prefix="s"):
    c = count(1)
    return lambda: f"{prefix}{next(c)}"


def pdf(n, *, outline=(), none_pages=()):
    pages = tuple(
        PageText(p, "" if p in none_pages else f"Page {p} heading\n\nbody text of page {p}.\nl3 of {p}\nl4 of {p}",
                 "none" if p in none_pages else "text", "no_text" if p in none_pages else None)
        for p in range(1, n + 1))
    return ParsedDoc("pdf", "doc", pages, tuple(OutlineEntry(*o) for o in outline), False)


def fixture_doc(name, kind):
    """A recorded fixture through the real parser, pages judged like the pipeline does."""
    from dataclasses import replace
    from pathlib import Path

    from evowiki.sources.parse import parse_bytes
    from evowiki.sources.unreadable import judge_all
    data = (Path(__file__).parents[1] / "fixtures" / "sources" / name).read_bytes()
    doc = parse_bytes(kind, data, title_hint=name)
    return replace(doc, pages=judge_all(doc.pages))
