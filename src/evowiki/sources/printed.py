"""Printed page numbers (pure). Citations link to the PDF page but show the number printed on it:
`page_offset` = PDF page − printed page, found from lone numbers at the top or bottom of pages.
None when the pages do not agree; the display then falls back to the PDF page."""

import re
from collections import Counter
from collections.abc import Sequence

from evowiki.sources.parsed import PageText

EDGE_LINES = 2          # lines looked at from the top and from the bottom of each page
MIN_AGREEING_PAGES = 3
# a line that is only a page number: 「12」「- 12 -」「第 12 页」
_LONE_NUMBER = re.compile(r"^[-–—\s]*(?:(?i:page)\s*|第\s*)?(\d{1,4})\s*(?:页)?[-–—\s]*$")


def _candidates(p: PageText) -> set[int]:
    lines = [ln.strip() for ln in p.text.splitlines() if ln.strip()]
    edges = lines[:EDGE_LINES] + lines[EDGE_LINES:][-EDGE_LINES:]
    return {int(m.group(1)) for ln in edges if (m := _LONE_NUMBER.match(ln))}


def find_page_offset(pages: Sequence[PageText]) -> int | None:
    """The offset that at least MIN_AGREEING_PAGES pages give and that is the majority of the
    pages with any candidate number; None otherwise."""
    votes: Counter[int] = Counter()
    with_candidates = 0
    for p in pages:
        if p.read_by == "none":
            continue
        nums = _candidates(p)
        if nums:
            with_candidates += 1
            votes.update({p.page - n for n in nums})
    if not votes:
        return None
    (best, n), *rest = votes.most_common(2) + [(None, 0)]
    if n < MIN_AGREEING_PAGES or n * 2 <= with_candidates or rest[0][1] == n:
        return None
    return best


def printed_page(pdf_page: int, offset: int | None) -> int | None:
    """The printed number of `pdf_page`; None when unknown or before printed page 1 (front
    matter numbered in roman numerals): show the PDF page instead."""
    if offset is None:
        return None
    n = pdf_page - offset
    return n if n >= 1 else None
