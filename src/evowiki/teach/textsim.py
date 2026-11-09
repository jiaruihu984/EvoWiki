"""Near-duplicate rule texts, shared by the RSI-B review (`rsib.review.duplicate_of`), the guideline
proposal dedup and the import's structural checks: normalize (NFKC, casefolded, without punctuation
or whitespace), then character-bigram Jaccard; ≥ DUP_JACCARD is a duplicate, unless one text has a
negation token the other lacks (`negations`). `squash` / `find_squashed`
locate a quoted span in a source text up to whitespace (the quote / span checks)."""

import re
import unicodedata

DUP_JACCARD = 0.8


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in t
                   if not ch.isspace() and unicodedata.category(ch)[0] not in "PZ")


def _bigrams(t: str) -> set[str]:
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else ({t} if t else set())


def bigram_jaccard(a: str, b: str) -> float:
    """Character-bigram Jaccard of two texts that are already in the caller's comparison form."""
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def jaccard(a: str, b: str) -> float:
    return bigram_jaccard(normalize(a), normalize(b))


# A rule and its negation share most bigrams; they are never duplicates.
NEGATION_ZH = ("不", "没", "别", "勿", "非", "无")
NEGATION_EN = frozenset({"not", "never", "don't", "dont", "no"})
_NOT_NEGATION_ZH = ("非常", "无论", "不仅", "不断", "并非")   # common words that are not a negation
_WORD = re.compile(r"[a-z']+")


def negations(text: str) -> frozenset[str]:
    """The negation tokens in a text: the Chinese characters (outside a few common words that are
    not negations, e.g. 非常 / 无论) and the English words not / never / don't / no."""
    t = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    for w in _NOT_NEGATION_ZH:
        t = t.replace(w, " ")
    return frozenset({c for c in NEGATION_ZH if c in t}
                     | {w for w in _WORD.findall(t) if w in NEGATION_EN})


def near_duplicate(a: str, b: str) -> bool:
    """Equal after strip, or bigram Jaccard ≥ DUP_JACCARD — unless one contains a negation token
    the other lacks (「先给例子」 vs 「不要先给例子」 are opposite rules, not duplicates)."""
    if a.strip() == b.strip():
        return True
    return negations(a) == negations(b) and jaccard(a, b) >= DUP_JACCARD


def squash(text: str) -> str:
    """The text without any whitespace."""
    return "".join(ch for ch in text if not ch.isspace())


def find_squashed(span: str, source: str) -> tuple[int, int] | None:
    """[start, end) character offsets in `source` of the first occurrence of `span`, both compared
    without whitespace; None when the span is blank or not there."""
    want = squash(span)
    if not want:
        return None
    idx = [i for i, ch in enumerate(source) if not ch.isspace()]
    pos = "".join(source[i] for i in idx).find(want)
    if pos < 0:
        return None
    return idx[pos], idx[pos + len(want) - 1] + 1
