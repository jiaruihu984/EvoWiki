"""How a material is meant to be used when the client does not say (one 「开始」 button, user
2026-10-21). Pure functions over sources/models.py rows; the word lists are guesses about wording,
not content guardrails."""

import re
from collections.abc import Sequence
from typing import Literal

from evowiki.sources.models import SectionRow, SourceRow

# the goal text asks to understand the attached material (「帮我读懂这篇」「看懂它」)
UNDERSTAND_TEXT = re.compile(r"(读懂|看懂)[^。！？\n]{0,6}(这篇|这份|它)|\b(?:understand|make sense of|read through)\s+(?:this|the|it)\b", re.IGNORECASE)
# … says the material is only a reference (「这本书作为参考」「参考这本」; not 「参考文献」) …
REFERENCE_TEXT = re.compile(r"参考(?!文献)|\b(?:as (?:a )?reference|refer to|for reference)\b", re.IGNORECASE)
# … or to learn by it (「按这本教材学」「跟着这本书学」「照《线性代数》学」)
FOLLOW_TEXT = re.compile(r"(按照?|跟着|照着?)\s*(这|它|该|此|《)[^。！？\n]{0,15}学|\b(?:learn|study|follow)\s+(?:from|with|using|along with)\s+(?:this|the|it)\b", re.IGNORECASE)
# a paper: at most this many pages (unknown: decided by the TOC alone) and section titles that
# start with an abstract heading and a references heading (case-insensitive)
PAPER_MAX_PAGES = 60
PAPER_ABSTRACT = ("摘要", "abstract")
PAPER_REFERENCES = ("参考文献", "references", "bibliography")
# an exam: the title or a section title contains one of these (case-insensitive; a Latin word
# only as a whole word, plural too, so 「Examples」 is no exam) …
EXAM_WORDS = ("试卷", "真题", "期末", "期中", "考试", "模拟题", "exam")
_EXAM = re.compile("|".join(rf"\b{w}s?\b" if w.isascii() else re.escape(w) for w in EXAM_WORDS))
# … or its text is mostly numbered questions: at least QUESTION_LINES_MIN lines that start with a
# question number (「1.」「1、」「（1）」「(1)」), hold an empty answer bracket (「（ ）」「( )」) or
# are an option line (「A.」「A、」「A．」), making up at least QUESTION_SHARE_MIN of the non-empty
# lines
QUESTION_LINES_MIN = 15
QUESTION_SHARE_MIN = 0.2
QUESTION_LINE = re.compile(r"^\s*(?:(?i:question|problem|exercise)\s+\d+\b|\d+[.、．](?!\d)|[（(]\d+[)）]|[A-H][.、．])"
                           r"|[（(]\s*[)）]")


def guess_text_use(goal: str) -> Literal["reference", "follow", "understand"] | None:
    """The use the goal text states plainly (spec 2026-10-28 §2.5): understand (UNDERSTAND_TEXT),
    reference (REFERENCE_TEXT), follow (FOLLOW_TEXT), checked in that order; None for a blank goal
    or other wording (the assistant, which reads the sentence anyway, names the use — no keyword
    guess beyond these)."""
    if not goal.strip():
        return None
    if UNDERSTAND_TEXT.search(goal):
        return "understand"
    if REFERENCE_TEXT.search(goal):
        return "reference"
    return "follow" if FOLLOW_TEXT.search(goal) else None


def _mostly_questions(sections: Sequence[SectionRow]) -> bool:
    lines = [ln for s in sections for ln in s.text.splitlines() if ln.strip()]
    hits = sum(1 for ln in lines if QUESTION_LINE.search(ln))
    return hits >= QUESTION_LINES_MIN and hits >= QUESTION_SHARE_MIN * len(lines)


def guess_material_use(source: SourceRow,
                       sections: Sequence[SectionRow]) -> Literal["learn", "understand", "exam"]:
    """From the material itself: exam-like (EXAM_WORDS in its title or a section title, or text
    that is mostly numbered questions) → exam, checked first; paper-like (PAPER_MAX_PAGES,
    abstract + references headings) → understand; else learn."""
    titles = [s.title.strip().casefold() for s in sections]
    if any(_EXAM.search(t) for t in (source.title.casefold(), *titles)) \
            or _mostly_questions(sections):
        return "exam"
    if source.pages is not None and source.pages > PAPER_MAX_PAGES:
        return "learn"

    def starts(words: Sequence[str]) -> bool:
        return any(t.startswith(w) for t in titles for w in words)

    return "understand" if starts(PAPER_ABSTRACT) and starts(PAPER_REFERENCES) else "learn"
