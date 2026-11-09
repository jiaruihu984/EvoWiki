"""Series note (plan B2): what the pages of one learning path have already said (running example,
terms, notation, one line per page), so the next page can continue instead of restarting.

Pure: `merge` folds the writer's `finish` report into the note and enforces the size cap,
`render` makes the text the writer reads. No I/O, no LLM."""

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

SERIES_MAX_CHARS = 3000
TITLE_MAX = 200                  # a path page title is `f"{goal}（i/n）"` and a goal may be longer
_NONE = "（还没有）"
_OLD_SUMMARY_CHARS = 40
_KEEP_TERMS = 60
_SENTENCE_END = re.compile(r"[。；.]")

_Notation = Annotated[str, Field(max_length=120)]
_NewTerm = Annotated[str, Field(max_length=80)]


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SeriesTerm(_M):
    term: str = Field(min_length=1, max_length=80)
    page_id: str                     # the page that introduced it
    page_title: str = Field(min_length=1, max_length=TITLE_MAX)


class SeriesPage(_M):
    page_id: str
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    summary: str = Field(max_length=300)


class SeriesNote(_M):
    example: str = Field(default="", max_length=600)
    terms: tuple[SeriesTerm, ...] = ()
    notation: tuple[_Notation, ...] = ()
    pages: tuple[SeriesPage, ...] = ()           # path order of writing


class SeriesUpdate(_M):
    """What the writer reports at `finish` for the page it just wrote."""

    example: str = Field(default="", max_length=600)
    terms: tuple[_NewTerm, ...] = Field(default=(), max_length=20)      # new terms this page introduced
    notation: tuple[_Notation, ...] = Field(default=(), max_length=10)  # new conventions
    summary: str = Field(min_length=1, max_length=300)


def render(note: SeriesNote, *, exclude_page: str | None = None) -> str:
    """The text the writer reads. `exclude_page`: leave out that page's own line and terms (the
    page being rewritten must not take what it taught itself for "already taught")."""
    if exclude_page is not None:
        note = note.model_copy(update={
            "terms": tuple(t for t in note.terms if t.page_id != exclude_page),
            "pages": tuple(p for p in note.pages if p.page_id != exclude_page)})
    by_page: dict[str, list[SeriesTerm]] = {}       # grouped by the page that introduced them
    for t in note.terms:
        by_page.setdefault(t.page_id, []).append(t)
    terms = "；".join(f"{ts[0].page_title}：{'、'.join(t.term for t in ts)}"
                     for ts in by_page.values()) or _NONE
    notation = "；".join(note.notation) or _NONE
    lines = [
        f"贯穿的例子：{note.example or _NONE}",
        f"已经讲过的术语：{terms}",
        f"记号约定：{notation}",
        "前面各页：",
    ]
    for p in note.pages:
        link = f"- [{p.title}](#p:{p.page_id})"
        lines.append(f"{link}：{p.summary}" if p.summary else link)
    return "\n".join(lines)


def _fits(note: SeriesNote) -> bool:
    return len(render(note)) <= SERIES_MAX_CHARS


def _first_sentence(text: str) -> str:
    m = _SENTENCE_END.search(text)
    head = text[: m.start()] if m and m.start() > 0 else text
    return head[:_OLD_SUMMARY_CHARS]


def _with_pages(note: SeriesNote, pages: list[SeriesPage]) -> SeriesNote:
    return note.model_copy(update={"pages": tuple(pages)})


def _fit(
    note: SeriesNote,
    protect_id: str,
    own_terms: frozenset[SeriesTerm] = frozenset(),
    own_notation: frozenset[str] = frozenset(),
) -> SeriesNote:
    """Shrink deterministically until the rendered note fits. Never touched: the example, the
    protected (just merged) page's line, and this update's own new terms and notation (the update
    is bounded in the model, so they fit for realistic updates; a pathological one can still end
    over the cap). Only older material is dropped."""
    if _fits(note):
        return note
    # 1. older summaries -> first sentence, oldest first
    pages = list(note.pages)
    for i, p in enumerate(pages):
        if p.page_id == protect_id:
            continue
        pages[i] = p.model_copy(update={"summary": _first_sentence(p.summary)})
        if _fits(_with_pages(note, pages)):
            return _with_pages(note, pages)
    # 2. older summaries -> empty, oldest first
    for i, p in enumerate(pages):
        if p.page_id == protect_id:
            continue
        pages[i] = p.model_copy(update={"summary": ""})
        if _fits(_with_pages(note, pages)):
            return _with_pages(note, pages)
    note = _with_pages(note, pages)
    # 3. older terms: beyond the newest 60 first, then further oldest-first (own terms stay)
    terms = list(note.terms[-_KEEP_TERMS:])
    note = note.model_copy(update={"terms": tuple(terms)})
    while not _fits(note):
        drop = next((i for i, t in enumerate(terms) if t not in own_terms), None)
        if drop is None:
            break
        terms.pop(drop)
        note = note.model_copy(update={"terms": tuple(terms)})
    # 4. last resort: older notation oldest-first, then older page lines oldest-first
    notation = list(note.notation)
    while not _fits(note):
        drop = next((i for i, n in enumerate(notation) if n not in own_notation), None)
        if drop is None:
            break
        notation.pop(drop)
        note = note.model_copy(update={"notation": tuple(notation)})
    pages = list(note.pages)
    while not _fits(note):
        # Unreachable under the update bounds (terms <= 20, notation <= 10) except for a
        # synthetic note whose older page lines alone are huge; the protected line stays.
        drop = next((i for i, p in enumerate(pages) if p.page_id != protect_id), None)
        if drop is None:
            break
        pages.pop(drop)
        note = _with_pages(note, pages)
    return note


def merge(
    note: SeriesNote | None, update: SeriesUpdate, *, page_id: str, title: str
) -> SeriesNote:
    base = note or SeriesNote()
    title = title[:TITLE_MAX]              # the event's title is unbounded; the fold must not raise
    example = base.example or update.example
    terms = list(base.terms)
    seen = {t.term for t in terms}
    own_terms: set[SeriesTerm] = set()
    for raw in update.terms:
        term = raw.strip()
        if term and term not in seen:
            seen.add(term)
            new = SeriesTerm(term=term, page_id=page_id, page_title=title)
            terms.append(new)
            own_terms.add(new)
    notation = list(base.notation)
    for n in update.notation:
        if n not in notation:
            notation.append(n)
    line = SeriesPage(page_id=page_id, title=title, summary=update.summary)
    pages = list(base.pages)
    for i, p in enumerate(pages):
        if p.page_id == page_id:
            pages[i] = line
            break
    else:
        pages.append(line)
    merged = SeriesNote(
        example=example, terms=tuple(terms), notation=tuple(notation), pages=tuple(pages)
    )
    return _fit(merged, page_id, frozenset(own_terms), frozenset(update.notation))
