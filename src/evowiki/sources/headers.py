"""Strip running headers, footers and page numbers from per-page text (pdf pages, pptx slides)."""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace

from evowiki.sources.parsed import PageText

MIN_PAGES = 4        # fewer pages: a repeat tells us nothing
EDGE_LINES = 2       # lines inspected at the top and at the bottom of each page
REPEAT_SHARE = 0.5   # a normalised edge line on more than this share of pages is running text

_DIGITS = re.compile(r"\d+")
_ROMAN = r"(?=[ivxlcdm])m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_NUMBERED = re.compile(
    r"^(?:\d+|[-–—]\s*\d+\s*[-–—]|第\s*\d+\s*页|\d+\s*/\s*\d+|page\s+\d+)$", re.IGNORECASE
)
_ROMAN_LINE = re.compile(rf"^{_ROMAN}$", re.IGNORECASE)
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
MIN_SEQUENCE = 3     # edge numbers on this many pages with one (value - page) offset are page numbers


def _roman_value(text: str) -> int:
    vals = [_ROMAN_VALUES[ch] for ch in text.lower()]
    return sum(-v if i + 1 < len(vals) and vals[i + 1] > v else v for i, v in enumerate(vals))


def _page_number_value(line: str) -> int | None:
    """The number a page-number-looking line shows, else None. Roman numerals are
    case-sensitive: "iv" and "IV" count, "Mix" is a word."""
    t = line.strip()
    if _NUMBERED.match(t):
        m = _DIGITS.search(t)
        return int(m.group()) if m else None
    if _ROMAN_LINE.match(t) and (t == t.lower() or t == t.upper()):
        return _roman_value(t)
    return None


def _norm(line: str) -> str:
    # a page-number-looking line keeps its digits: only the same number on many pages repeats;
    # numbers that change with the page are judged by the sequence rule instead
    if _page_number_value(line) is not None:
        return line.strip()
    return _DIGITS.sub("#", line.strip())


def _page_number_chains(cands: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Candidates (page, line, value) that behave like page numbers: chains of at least
    MIN_SEQUENCE whose value rises with the page and never falls behind the page (gaps in the
    numbering are fine, a lone chapter number is not part of any chain)."""
    left = sorted(cands)
    found: list[tuple[int, int, int]] = []
    while len(left) >= MIN_SEQUENCE:
        best: list[int] = []     # best[i]: length of the longest chain ending at left[i]
        prev: list[int] = []
        for i, (pg, _, v) in enumerate(left):
            length, back = 1, -1
            for j in range(i):
                pj, _, vj = left[j]
                if pj < pg and vj < v and vj - pj <= v - pg and best[j] + 1 > length:
                    length, back = best[j] + 1, j
            best.append(length)
            prev.append(back)
        end = max(range(len(left)), key=best.__getitem__)
        if best[end] < MIN_SEQUENCE:
            break
        chain = set()
        while end >= 0:
            chain.add(end)
            end = prev[end]
        found += [left[i] for i in chain]
        left = [c for i, c in enumerate(left) if i not in chain]
    return found


def _edge_indices(lines: list[str]) -> set[int]:
    filled = [i for i, ln in enumerate(lines) if ln.strip()]
    return set(filled[:EDGE_LINES]) | set(filled[-EDGE_LINES:])


def strip_running_lines(pages: Sequence[PageText]) -> tuple[PageText, ...]:
    """Drop repeated edge lines and page-number lines; documents under MIN_PAGES pass through."""
    if len(pages) < MIN_PAGES:
        return tuple(pages)
    split = [p.text.split("\n") for p in pages]
    edges = [_edge_indices(lines) for lines in split]
    seen: Counter[str] = Counter()
    for lines, idx in zip(split, edges):
        seen.update({_norm(lines[i]) for i in idx})
    running = {k for k, n in seen.items() if n > len(pages) * REPEAT_SHARE}
    numbered = _page_number_chains([
        (page.page, i, v)
        for page, lines, idx in zip(pages, split, edges)
        for i in sorted(idx)
        if (v := _page_number_value(lines[i])) is not None
    ])
    page_numbers = {(pg, i) for pg, i, _ in numbered}

    def is_page_number(page: PageText, line_index: int) -> bool:
        return (page.page, line_index) in page_numbers

    out: list[PageText] = []
    for page, lines, idx in zip(pages, split, edges):
        kept = [
            ln for i, ln in enumerate(lines)
            if not (i in idx and (_norm(ln) in running or is_page_number(page, i)))
        ]
        text = "\n".join(kept)
        out.append(page if text == page.text else replace(page, text=text))
    return tuple(out)
