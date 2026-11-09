"""Decide which parsed pages have no usable text layer (pure, no IO).

judge_page looks at a page's text plus two facts the PDF parser measured (a font without a
Unicode map, the share of the page covered by images) and records every signal that fired.

- Unreadable (read_by="none" + reason): any strong signal, two or more weak signals, or too
  few characters. Later phases hand those pages to OCR; if OCR fails they stay unreadable.
- Try OCR, keep the text (read_by="text" + a hint in OCR_HINTS): formula / table pages,
  pages with a large image area, and pages whose font has no Unicode map (alone; with any
  other strong signal or two weak points the page is unreadable). OCR's Markdown / LaTeX
  replaces the text layer when it works; otherwise the text layer is kept. Whether an image
  page is a figure that needs a seeing model is decided by the vision step after OCR
  (PageText.figure), not here.
- Contract: hinted pages (wants_ocr but read_by="text") go to the LOCAL reader only, never to
  Claude and never into a vision quote; their cost is local GPU time only.

v2 (phase 2): phase 1's eval found recall 0.62; the misses were garbled text layers with
enough characters, which the old "share of garble characters > 30%" rule never reached.
When in doubt we flag the page: sending a readable page to OCR costs a little, missing an
unreadable one loses content.
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from evowiki.sources.models import ReadBy
from evowiki.sources.parsed import PageText

# -- phase 1 values (kept) --
MIN_CHARS_PER_PAGE = 40          # fewer non-space chars than this: "no text layer" ...
SHORT_PAGE_CHARS = 10            # ... unless the page has no picture and at least this many: a
#                                  short slide ("Practice"), all of it read
MAX_GARBLE_RATIO = 0.3           # share of U+FFFD / private-use / control / stray combining chars
MAX_FRAGMENT_RATIO = 0.7         # share of whitespace tokens that are 1-char or "(cid:N)" fragments
# Phase 1 flagged fragments > 0.7 on their own; v2 makes that a weak signal. A page that is
# (almost) nothing but fragments still goes on its own, as "sparse".
ALL_FRAGMENTS_RATIO = 0.9

# -- strong signals: any one sends the page --
REPLACEMENT_RATIO = 0.02         # U+FFFD
PRIVATE_USE_RATIO = 0.05         # U+E000-U+F8FF
CID_MIN = 3                      # "(cid:N)" left in the text
CONTROL_RATIO = 0.01             # control chars other than tab / newline / form feed

# -- weak signals: one point each, WEAK_POINTS send the page --
RARE_HANZI_RATIO = 0.3           # hanzi outside the 3500 common ones ...
RARE_HANZI_MIN = 20              # ... counted only when the page has this many hanzi
RUN_LENGTH = 6                   # one char repeated this many times in a row ...
RUN_RATIO = 0.1                  # ... covering this share of the page
WEAK_POINTS = 2

# -- formula / table pages --
MATH_RATIO = 0.03                # math symbols + Greek letters, share of non-space chars
SHORT_LINE_RATIO = 0.4           # share of lines that are short and mostly digits / symbols
SHORT_LINE_CHARS = 12
SHORT_LINE_MIN_LINES = 5         # fewer lines than this: no table rule (a title + page number)

# -- image pages --
IMAGE_AREA = 0.3
PICTURE_AREA = 0.005             # about 7% by 7% of the page: a pasted formula, not a bullet icon
DRAWN_MARKS = 8                  # small vector shapes on a page (`parse_pdf._drawn_marks`): the formula
#                                  pages of sample lecture have 8 to 44, its text pages at most 2

OCR_HINTS = frozenset({"formula_or_table", "image_page", "no_unicode_map"})

_CID = re.compile(r"\(cid:\d+\)")
_WS_CONTROLS = frozenset("\t\n\r\x0b\x0c")
_RUN = re.compile(rf"(\S)\1{{{RUN_LENGTH - 1},}}")
# Repeats that layout makes on purpose: dot leaders, rules, fill-in blanks.
_LEADERS = frozenset(".·…-_—–=*~．。、－＿＝～")
_MATH = frozenset("∑∫√≤≥±×÷∞∂∇") | frozenset(
    chr(c) for c in (*range(0x391, 0x3AA), *range(0x3B1, 0x3CA), 0x3D1, 0x3D5, 0x3D6, 0x3F5)
    if unicodedata.category(chr(c)).startswith("L"))

_HANZI_FILE = Path(__file__).with_name("data") / "common_hanzi_3500.txt"


def _load_common_hanzi() -> frozenset[str]:
    lines = _HANZI_FILE.read_text(encoding="utf8").splitlines()
    chars = frozenset(s for line in lines if (s := line.strip()) and not s.startswith("#"))
    if len(chars) != 3500 or any(len(c) != 1 for c in chars):
        raise RuntimeError(f"{_HANZI_FILE} must list 3500 single characters")
    return chars


COMMON_HANZI = _load_common_hanzi()


def _is_hanzi(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF
            or 0x20000 <= o <= 0x3134F)


def _is_garble(ch: str, prev: str | None) -> bool:
    if ch == "\ufffd":
        return True
    cat = unicodedata.category(ch)
    if cat == "Co" or cat == "Cn" or cat == "Cs":
        return True
    if cat == "Cc" and ch not in _WS_CONTROLS:
        return True
    if cat in ("Mn", "Mc", "Me"):
        # A combining mark needs a base character right before it.
        return prev is None or prev.isspace() or unicodedata.category(prev).startswith(("M", "C"))
    return False


def _garble_ratio(text: str) -> float:
    total = 0
    bad = 0
    prev: str | None = None
    for ch in text:
        if not ch.isspace():
            total += 1
            if _is_garble(ch, prev):
                bad += 1
        prev = ch
    return bad / total if total else 0.0


def _fragment_ratio(text: str) -> float:
    tokens = text.split()
    if not tokens:
        return 0.0
    # A fragment is a 1-char token or one made only of "(cid:N)" codes, glued or not.
    fragments = sum(1 for t in tokens if len(t) == 1 or _CID.sub("", t) == "")
    return fragments / len(tokens)


def _rare_hanzi(text: str) -> bool:
    hanzi = [ch for ch in text if _is_hanzi(ch)]
    if len(hanzi) < RARE_HANZI_MIN:
        return False
    return sum(1 for ch in hanzi if ch not in COMMON_HANZI) / len(hanzi) >= RARE_HANZI_RATIO


def _run_ratio(text: str, chars: int) -> float:
    covered = sum(len(m.group(0)) for m in _RUN.finditer(text) if m.group(1) not in _LEADERS)
    return covered / chars


def _short_symbol_line(line: str) -> bool:
    body = [ch for ch in line if not ch.isspace()]
    if not body or len(line) > SHORT_LINE_CHARS:
        return False
    return sum(1 for ch in body if not ch.isalpha()) * 2 > len(body)


def _formula_or_table(text: str, chars: int) -> bool:
    if sum(1 for ch in text if ch in _MATH) / chars >= MATH_RATIO:
        return True
    lines = [s for line in text.splitlines() if (s := line.strip())]
    if len(lines) < SHORT_LINE_MIN_LINES:
        return False
    return sum(1 for s in lines if _short_symbol_line(s)) / len(lines) >= SHORT_LINE_RATIO


def _signals(text: str, fonts_without_tounicode: bool, image_area: float
             ) -> tuple[tuple[str, ...], str | None]:
    """(signals that fired, reason if the page is unreadable else None)."""
    chars = sum(1 for ch in text if not ch.isspace())
    strong: list[str] = []
    weak: list[str] = []
    hints: list[str] = []
    if chars:
        if text.count("\ufffd") / chars >= REPLACEMENT_RATIO:
            strong.append("replacement_chars")
        if sum(1 for ch in text if "\ue000" <= ch <= "\uf8ff") / chars >= PRIVATE_USE_RATIO:
            strong.append("private_use")
        if len(_CID.findall(text)) >= CID_MIN:
            strong.append("cid_codes")
        controls = sum(1 for ch in text
                       if unicodedata.category(ch) == "Cc" and ch not in _WS_CONTROLS)
        if controls / chars >= CONTROL_RATIO:
            strong.append("control_chars")
        if _garble_ratio(text) > MAX_GARBLE_RATIO:          # phase 1 rule: combining marks etc.
            strong.append("garbled_chars")
        if fonts_without_tounicode:
            # A hint, not a strong signal: as "any unmapped character sends the page" it
            # flagged many born-digital pages (math symbol fonts) in the phase-2 eval.
            hints.append("no_unicode_map")
        if _rare_hanzi(text):
            weak.append("rare_hanzi")
        if _run_ratio(text, chars) >= RUN_RATIO:
            weak.append("char_runs")
        if _formula_or_table(text, chars):
            hints.append("formula_or_table")
    fragments = _fragment_ratio(text)
    if fragments >= MAX_FRAGMENT_RATIO:
        weak.append("fragments")
    if chars < MIN_CHARS_PER_PAGE:
        weak.append("few_chars")
    if image_area >= IMAGE_AREA:
        hints.append("image_page")

    if chars < MIN_CHARS_PER_PAGE and (image_area > 0 or chars < SHORT_PAGE_CHARS):
        reason: str | None = "no_text"
    elif fragments >= ALL_FRAGMENTS_RATIO:
        reason = "sparse"
    elif strong or len(weak) >= WEAK_POINTS:
        reason = "garbled"
    else:
        reason = None
    return (*strong, *weak, *hints), reason


def judge_page(page: PageText, fonts_without_tounicode: bool, image_area: float) -> PageText:
    """Return the page with read_by / reason / signals set from its text and the two PDF
    facts. Signals are recomputed from scratch, so judging twice gives the same page."""
    signals, reason = _signals(page.text, fonts_without_tounicode, image_area)
    read_by: ReadBy = "text" if reason is None else "none"
    return dataclasses.replace(page, read_by=read_by, reason=reason, signals=signals)


def judge(page: PageText) -> PageText:
    """judge_page with the facts the parser stored on the page."""
    return judge_page(page, page.fonts_without_tounicode, page.image_area)


def judge_all(pages: Sequence[PageText]) -> tuple[PageText, ...]:
    return tuple(judge(p) for p in pages)


def wants_ocr(page: PageText) -> bool:
    """Should the vision step read this page? Unreadable pages must be read (on failure they
    stay unreadable); hinted pages are tried and keep their text layer if OCR fails."""
    return page.read_by == "none" or any(s in OCR_HINTS for s in page.signals)
