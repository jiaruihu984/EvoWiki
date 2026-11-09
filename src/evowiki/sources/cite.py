"""Program-verified citations (materials Task 6b). The page writer cites a material as
`〔名 p.N：「原文片段」〕` (N = the PDF page the fragment is on, as the material's 〔p.N〕 marks say)
and flags a disagreement as `⟦和资料不同：说明；资料原话：「…」⟧`. `verify` checks each against the
material's own text before the page is stored. Pure: the page text comes from the caller.

- A citation whose fragment is found (normalized: NFKC, casefold, no whitespace or punctuation;
  fuzzy, `difflib` ratio ≥ QUOTE_MATCH over a sliding window) on page N, or across N and N+1 (a
  sentence that runs over the page break), becomes the stored form `〔名 p.印码｜N〕` (印码 = the
  printed page number, `sources.printed.printed_page`; N when unknown or before printed page 1).
  It is never moved to another page.
- Anything else — the fragment not found, no fragment (`〔名 p.N〕`), a name that is no material of
  the page — is removed from the text entirely (nothing shows on the page: the user never sees the
  internal check) and recorded as a `CiteIssue` (kept on the page record for RSI).
- `⟦和资料不同：…；资料原话：「…」⟧`: the quote found in the page's mapped sections → the note stays
  and the quote leaves the text (it stays on the issue record, kind `differs_verified`); not found
  (or no quote) → `⟦待查证：说明⟧` (the existing mark) and a `differs_unverified` issue.
- A quote shorter than MIN_QUOTE_CHARS (normalized) proves nothing: not found.
- The stored form written by the writer is kept only when exactly that mark is already in the
  section's stored text the write started from (`kept`: an earlier verified citation carried over
  by a rewrite); any other — forged, or copied from another section — is removed
  (`unverified_stored`).
- Other marks pass through untouched, e.g. bare `〔p.N〕` page marks.
- A citation that passes is recorded too (kind `verified`, `mark` = the stored form, with its quote):
  the stored mark drops the quote, so this is what locates it in the material; the caller adds its
  `locator` (`wikiapp.refine_apply.checked_citations`, `sources.locate`)."""

import difflib
import re
import unicodedata
from collections.abc import Callable, Collection, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from evowiki.sources.config import PAGE_MARK_RE
from evowiki.sources.printed import printed_page

QUOTE_MATCH = 0.85
MIN_QUOTE_CHARS = 6         # a shorter quote (normalized) matches almost any page: it verifies nothing

CiteKind = Literal["quote_not_found", "no_quote", "unknown_source", "differs_unverified",
                   "differs_verified", "unverified_stored", "verified"]
NO_MISMATCH: frozenset[str] = frozenset({"differs_verified", "verified"})  # recorded, not a problem

# the stored (verified) form 〔名 p.印码｜PDF页〕
STORED_RE = re.compile(r"(?P<lead>[ \t　]*)(?P<mark>〔(?P<name>[^〔〕｜\n]+?)\s*p\.\s*\d+｜"
                       r"(?P<page>\d+)〕)")

# 〔名 p.N：「片段」〕 / 〔名 p.N〕 / 〔名 p.N–M〕 (the name holds no 〔〕｜, so the stored
# 〔名 p.印码｜N〕 never matches); `lead`: the spaces before it, removed with a dropped mark
CITE_RE = re.compile(r"(?P<lead>[ \t　]*)〔(?P<name>[^〔〕｜\n]+?)\s*p\.\s*(?P<page>\d+)"
                     r"(?:\s*[–—\-~～]\s*\d+)?"
                     r"\s*(?:[：:]\s*「(?P<quote>[^」\n]*)」)?\s*〕")
DIFFERS_RE = re.compile(r"⟦和资料不同[：:](?P<note>[^⟧]*?)"
                        r"(?:[；;]\s*资料原话[：:]\s*「(?P<quote>[^」⟧]*)」\s*)?⟧")


class CiteSource(Protocol):
    """A material of the page (`wikiapp.events.PageSource`)."""

    @property
    def source_id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def page_offset(self) -> int | None: ...


class CiteIssue(BaseModel):
    """One citation or disagreement mark the check did not keep as written, or a citation that
    passed (`verified`). `mark`: the mark as the writer wrote it (`verified`: as stored);
    `section_id`: the page section it was in (set by the caller); `locator`: a verified citation's
    place in the stored document (`sources.locate` JSON), None when unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CiteKind
    mark: str
    source_id: str | None = None
    page: int | None = None
    quote: str = ""
    note: str = ""
    section_id: str = ""
    locator: dict | None = None


PageText = Callable[[str, int], str]     # (source_id, PDF page) -> that page's text ("" = none)


def normalize(text: str) -> str:
    """NFKC, casefold, without page marks, whitespace or punctuation."""
    t = unicodedata.normalize("NFKC", PAGE_MARK_RE.sub("", text)).casefold()
    return "".join(c for c in t if not c.isspace() and not unicodedata.category(c).startswith("P"))


def _windows_match(q: str, t: str, starts: range) -> bool:
    """Some window of `t` as long as `q`, starting at one of `starts`, has ratio ≥ QUOTE_MATCH."""
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(q)
    for i in starts:
        sm.set_seq1(t[i:i + len(q)])
        if sm.real_quick_ratio() >= QUOTE_MATCH and sm.quick_ratio() >= QUOTE_MATCH \
                and sm.ratio() >= QUOTE_MATCH:
            return True
    return False


_DIGITS = re.compile(r"\d+")


def _numbers_in(q: str, t: str) -> bool:
    """Every number of the (normalized) quote is in the text: a fuzzy match is for OCR and
    typesetting noise, and 0.05 against 0.01 or 120 against 320 is one digit of a longer quote,
    well above QUOTE_MATCH, that would certify a wrong figure."""
    return all(d in t for d in _DIGITS.findall(q))


def quote_in(quote: str, text: str) -> bool:
    """The quote occurs in `text`, exactly after normalizing or with a window ratio ≥ QUOTE_MATCH
    (and every number of it in `text`). A quote shorter than MIN_QUOTE_CHARS never does."""
    q, t = normalize(quote), normalize(text)
    if len(q) < MIN_QUOTE_CHARS or not t:
        return False
    return q in t or (_numbers_in(q, t) and _windows_match(q, t, range(max(1, len(t) - len(q) + 1))))


def quote_on(quote: str, page: str, next_page: str) -> bool:
    """The quote is on `page`, or starts there and runs over onto `next_page`: a window across the
    break counts only with enough of it on `page` that a quote lying wholly on the next page
    cannot reach QUOTE_MATCH (the citation's page is never moved)."""
    if quote_in(quote, page):
        return True
    q, a, b = normalize(quote), normalize(page), normalize(next_page)
    n = len(q)
    if n < MIN_QUOTE_CHARS or not a or not b:
        return False
    tail = a[-(n - 1):]
    least = int(n * (1 - QUOTE_MATCH)) + 1          # characters of the window on `page`
    window = tail + b[:n - 1]
    return _numbers_in(q, window) and _windows_match(q, window, range(len(tail) - least + 1))


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().strip("《》").strip().casefold()


def match_source(name: str, sources: Sequence[CiteSource]) -> CiteSource | None:
    """The material `name` means: its id is what counts from here on, the caller shows its current
    title. An earlier name (a model that read the file name or url before the real title arrived)
    still finds it; a current title goes first."""
    key = _name_key(name)
    return (next((s for s in sources if _name_key(s.title) == key), None)
            or next((s for s in sources for old in getattr(s, "earlier_titles", ())
                     if _name_key(old) == key), None))


def cited_pages(body: str, sources: Sequence[CiteSource]) -> dict[str, set[int]]:
    """The pages `verify` will read for this body: N and N+1 of every quoted citation of a
    material of the page, by source id."""
    out: dict[str, set[int]] = {}
    for m in CITE_RE.finditer(body):
        src = match_source(m.group("name"), sources)
        if src is not None and m.group("quote") is not None:
            n = int(m.group("page"))
            out.setdefault(src.source_id, set()).update((n, n + 1))
    return out


def stored_marks(body: str) -> set[str]:
    """The stored-form marks in a body, as written."""
    return {m.group("mark") for m in STORED_RE.finditer(body)}


def stored_titles(body: str) -> set[str]:
    """The material names the stored-form marks of a body cite."""
    return {m.group("name").strip() for m in STORED_RE.finditer(body)}


def verify(body: str, sources: Sequence[CiteSource], page_text: PageText,
           mapped_text: str = "", kept: Collection[str] = ()) -> tuple[str, list[CiteIssue]]:
    """`body` with its citations and disagreement marks checked (module doc), and the issues.
    `mapped_text`: the text of the page's mapped sections (where a disagreement's quote must be);
    `kept`: the stored-form marks already in this section's stored text (`stored_marks`), the only
    ones the writer may carry over. Whatever `page_text` raises propagates (the caller fails the
    write)."""
    issues: list[CiteIssue] = []

    def stored(m: re.Match[str]) -> str:
        if m.group("mark") in kept:
            return m.group(0)
        src = match_source(m.group("name"), sources)
        issues.append(CiteIssue(kind="unverified_stored", mark=m.group("mark"),
                                source_id=src.source_id if src is not None else None,
                                page=int(m.group("page"))))
        return ""

    def cite(m: re.Match[str]) -> str:
        mark, name, n, quote = (m.group(0).strip(), m.group("name"), int(m.group("page")),
                                m.group("quote"))
        src = match_source(name, sources)
        if src is None:
            issues.append(CiteIssue(kind="unknown_source", mark=mark, page=n, quote=quote or ""))
            return ""
        if quote is None:
            issues.append(CiteIssue(kind="no_quote", mark=mark, source_id=src.source_id, page=n))
            return ""
        if not quote_on(quote, page_text(src.source_id, n), page_text(src.source_id, n + 1)):
            issues.append(CiteIssue(kind="quote_not_found", mark=mark, source_id=src.source_id,
                                    page=n, quote=quote))
            return ""
        printed = printed_page(n, src.page_offset) or n
        kept_mark = f"〔{src.title} p.{printed}｜{n}〕"
        issues.append(CiteIssue(kind="verified", mark=kept_mark, source_id=src.source_id, page=n,
                                quote=quote))
        return f"{m.group('lead')}{kept_mark}"

    def differs(m: re.Match[str]) -> str:
        mark, note, quote = m.group(0), m.group("note").strip().rstrip("；;").strip(), m.group("quote")
        if quote is not None and quote_in(quote, mapped_text):
            issues.append(CiteIssue(kind="differs_verified", mark=mark, quote=quote, note=note))
            return f"⟦和资料不同：{note}⟧"
        issues.append(CiteIssue(kind="differs_unverified", mark=mark, quote=quote or "", note=note))
        return f"⟦待查证：{note}⟧"

    out = STORED_RE.sub(stored, body)      # first: the verified ones `cite` writes are new
    out = CITE_RE.sub(cite, out)
    out = DIFFERS_RE.sub(differs, out)
    return out, issues
