"""Where a quote sits in the stored document: the block (and the character range in it) whose
text contains it, found on the page or one page either side. Never guesses a block.

Extraction results store the Locator as JSON (`locator_json`: glossary entries, exam items,
matched sections — spec 2026-10-29-sources-verifiable-extraction); a result whose quote is not
found keeps its page and section and has no locator."""

import dataclasses
from typing import Any

from evowiki.sources.document import Block, Locator, SourceDocument

_PREFIX = 40


def _squash(text: str) -> tuple[str, list[int]]:
    """Case-folded text without whitespace and math delimiters (`$`, `\\(` `\\)` `\\[`
    `\\]`: the model and the parser write them differently), and each kept character's index in
    `text`."""
    out: list[str] = []
    idx: list[int] = []
    skip = False
    for i, ch in enumerate(text):
        if skip:
            skip = False
            continue
        if ch == "\\" and text[i + 1:i + 2] in ("(", ")", "[", "]"):
            skip = True
            continue
        if ch.isspace() or ch == "$":
            continue
        for c in ch.casefold():
            out.append(c)
            idx.append(i)
    return "".join(out), idx


def _in_block(block: Block, needle: str) -> tuple[int, int] | None:
    hay, idx = _squash(block.text)
    at = hay.find(needle)
    if at < 0:
        return None
    return idx[at], idx[at + len(needle) - 1] + 1


def locate(doc: SourceDocument, source_id: str, page: int, quote: str) -> Locator | None:
    needle = _squash(quote)[0][:_PREFIX]
    if not needle:
        return None
    for pg in (page, page - 1, page + 1):
        for b in doc.blocks:
            if b.page != pg:
                continue
            span = _in_block(b, needle)
            if span is not None:
                return Locator(source_id, b.page, b.bbox, b.block_id, span[0], span[1])
    return None


def locator_json(loc: Locator | None) -> dict[str, Any] | None:
    return None if loc is None else dataclasses.asdict(loc)


def locate_json(doc: SourceDocument | None, source_id: str, page: int | None,
                quote: str) -> dict[str, Any] | None:
    """`locate` as stored JSON; None without a document, a page or a match."""
    if doc is None or page is None:
        return None
    return locator_json(locate(doc, source_id, page, quote))
