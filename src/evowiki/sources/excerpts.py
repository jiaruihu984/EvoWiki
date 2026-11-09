"""The material text a page writer sees for the sections a page's node maps to (spec §6): the
whole section when everything mapped fits FULL_TEXT_TOKENS, else navigation (summary / lead).
A material with an exam use and extracted items keeps its exercise sections out (they are
practised as 真题 on the page, not written into its text: materials phase 5, decision 14)."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from psycopg_pool import ConnectionPool

from evowiki.goals.context import estimate_tokens
from evowiki.goals.reading import UnitSpan
from evowiki.sources import config, exam_store, store
from evowiki.sources.document import SourceDocument
from evowiki.sources.labels import lead, paragraphs, section_labels
from evowiki.sources.models import LinkRole, LinkRow, SectionRow, SourceRow
from evowiki.sources.titles import prompt_title

_RANK: dict[str, int] = {"reads": 0, "teaches": 1, "exercises": 2}


@dataclass(frozen=True)
class SourceExcerpt:
    """One mapped section for the page writer. `text` is the whole section when the page's
    mapped sections fit in FULL_TEXT_TOKENS, else None and `summary` (or the lead) stands in."""
    source_id: str
    source_title: str
    section_id: str
    label: str              # 「§3.2」
    title: str
    page_from: int | None
    page_to: int | None
    role: LinkRole
    text: str | None
    summary: str
    page_offset: int | None = None      # the material's PDF page − printed page (None: unknown)
    glossary: tuple[dict, ...] = ()     # the material's notation / terms ({"term", "means", "page"})
    lead: str = ""                      # the section's opening paragraph (`labels.lead`)


class SourceExcerpts(Protocol):
    def for_nodes(self, user_id: str, node_ids: Sequence[str],
                  parents: Mapping[str, Sequence[str]] | None = None) -> list[SourceExcerpt]: ...

    def page_texts(self, user_id: str, source_id: str,
                   pages: Sequence[int]) -> dict[int, str]: ...


@runtime_checkable
class DocumentSource(Protocol):
    """A material reader that also gives the stored layout document (`sources.locate`); a citation
    read through one gets a locator, any other keeps its page only."""

    def document(self, user_id: str, source_id: str) -> SourceDocument | None: ...


def pages_of(sections: Sequence[SectionRow], pages: Sequence[int]) -> dict[int, str]:
    """The text of each wanted PDF page, from the sections' 〔p.N〕 marks (text before a section's
    first mark belongs to its `page_from`); a page spread over sections is joined in `ord` order;
    pages with no text are left out. Unreadable (`none`) sections give nothing."""
    wanted = set(pages)
    out: dict[int, list[str]] = {}
    for sec in sorted(sections, key=lambda s: (s.ord, s.section_id)):
        if sec.read_by == "none":
            continue
        page, pos = sec.page_from, 0
        for m in [*config.PAGE_MARK_RE.finditer(sec.text), None]:
            end = m.start() if m is not None else len(sec.text)
            chunk = sec.text[pos:end].strip()
            if chunk and page in wanted:
                out.setdefault(page, []).append(chunk)
            if m is not None:
                page, pos = int(m.group(1)), m.end()
    return {p: "\n".join(xs) for p, xs in out.items()}


def with_parents(links: Sequence[LinkRow], node_ids: Sequence[str],
                 parents: Mapping[str, Sequence[str]]) -> list[LinkRow]:
    """The links a page's nodes use (stage 3, spec 2026-10-28 §2.5): each node's own; a node with
    none uses its nearest ancestor's that has some (`parents`: node → its part_of ancestors,
    nearest first — a block's section links serve its parts until entering refines them)."""
    own: dict[str, list[LinkRow]] = {}
    for k in links:
        own.setdefault(k.node_id, []).append(k)
    out: list[LinkRow] = []
    for n in node_ids:
        got = own.get(n) or next((own[a] for a in parents.get(n, ()) if own.get(a)), [])
        out += [k for k in got if k not in out]
    return out


def _wanted(node_ids: Sequence[str], parents: Mapping[str, Sequence[str]]) -> list[str]:
    return list(dict.fromkeys([*node_ids, *(a for n in node_ids for a in parents.get(n, ()))]))


def excerpts_for(sections_by_source: Mapping[str, tuple[SourceRow, Sequence[SectionRow]]],
                 links: Sequence[LinkRow], *, budget: int,
                 exam_sources: Collection[str] = ()) -> list[SourceExcerpt]:
    """One excerpt per mapped section in link order; the role is the strongest of its links
    (reads > teaches > exercises). Links to unknown sources or sections are skipped, and so are
    `exercises` sections of `exam_sources` (materials whose exercises are 真题)."""
    order: list[tuple[str, str]] = []
    role: dict[tuple[str, str], LinkRole] = {}
    for k in links:
        key = (k.source_id, k.section_id)
        if key not in role:
            order.append(key)
            role[key] = k.role
        elif _RANK[k.role] < _RANK[role[key]]:
            role[key] = k.role
    found: list[tuple[SourceRow, SectionRow, str, LinkRole]] = []
    labels: dict[str, dict[str, str]] = {}
    for source_id, section_id in order:
        if source_id in exam_sources and role[(source_id, section_id)] == "exercises":
            continue
        entry = sections_by_source.get(source_id)
        if entry is None:
            continue
        source, secs = entry
        sec = next((s for s in secs if s.section_id == section_id), None)
        if sec is None:
            continue
        if source_id not in labels:
            labels[source_id] = section_labels(secs)
        found.append((source, sec, labels[source_id][section_id], role[(source_id, section_id)]))
    full = sum(sec.tokens for _, sec, _, _ in found) <= budget
    return [SourceExcerpt(source_id=src.source_id, source_title=prompt_title(src.title),
                          section_id=sec.section_id, label=label, title=sec.title,
                          page_from=sec.page_from, page_to=sec.page_to, role=r,
                          text=sec.text if full else None,
                          summary=sec.summary or lead(sec.text),
                          page_offset=src.page_offset, glossary=src.glossary,
                          lead=lead(sec.text))
            for src, sec, label, r in found]


class PooledSourceExcerpts:
    """Reads links and sections from the database, one connection per call."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def for_nodes(self, user_id: str, node_ids: Sequence[str],
                  parents: Mapping[str, Sequence[str]] | None = None) -> list[SourceExcerpt]:
        """`parents`: node → its ancestors, nearest first (`with_parents`)."""
        with self._pool.connection() as conn:
            links = with_parents(store.links_for_nodes(conn, user_id,
                                                       _wanted(node_ids, parents or {})),
                                 node_ids, parents or {})
            by_source: dict[str, tuple[SourceRow, Sequence[SectionRow]]] = {}
            exam: set[str] = set()
            for source_id in dict.fromkeys(k.source_id for k in links):
                source = store.get_source(conn, user_id, source_id)
                if source is not None and source.deleted_at is None:
                    by_source[source_id] = (source, store.sections(conn, user_id, source_id))
                    if (any(u["use"] == "exam" for u in store.uses_of(conn, user_id, source_id))
                            and exam_store.items_of_source(conn, user_id, source_id)):
                        exam.add(source_id)
        return excerpts_for(by_source, links, budget=config.full_text_tokens(),
                            exam_sources=exam)

    def for_spans(self, user_id: str, source_id: str,
                  spans: Sequence[UnitSpan]) -> list[SourceExcerpt]:
        """A reading unit's own text (`excerpts_for_spans`). LookupError for an unknown, deleted
        or other user's material (fail closed)."""
        with self._pool.connection() as conn:
            source = store.get_source(conn, user_id, source_id)
            if source is None or source.deleted_at is not None:
                raise LookupError(f"没有这份资料：{source_id}")
            secs = store.sections(conn, user_id, source_id)
        return excerpts_for_spans(source, secs, spans, budget=config.full_text_tokens())

    def document(self, user_id: str, source_id: str) -> SourceDocument | None:
        with self._pool.connection() as conn:
            return store.get_document(conn, user_id, source_id)

    def page_texts(self, user_id: str, source_id: str,
                   pages: Sequence[int]) -> dict[int, str]:
        """LookupError for an unknown, deleted or other user's material (fail closed)."""
        with self._pool.connection() as conn:
            source = store.get_source(conn, user_id, source_id)
            if source is None or source.deleted_at is not None:
                raise LookupError(f"没有这份资料：{source_id}")
            return pages_of(store.sections(conn, user_id, source_id), pages)


class MemorySourceExcerpts:
    """Test double: {user_id: [(source, sections, links)]}; only that user's materials.
    `exam_sources`: the materials with an exam use and extracted items."""

    def __init__(self, data: Mapping[str, Sequence[tuple[SourceRow, Sequence[SectionRow],
                                                         Sequence[LinkRow]]]],
                 exam_sources: Collection[str] = ()) -> None:
        self._data = data
        self._exam = frozenset(exam_sources)

    def for_nodes(self, user_id: str, node_ids: Sequence[str],
                  parents: Mapping[str, Sequence[str]] | None = None) -> list[SourceExcerpt]:
        wanted = set(_wanted(node_ids, parents or {}))
        entries = self._data.get(user_id, [])
        by_source = {src.source_id: (src, secs) for src, secs, _ in entries}
        links = with_parents([k for _, _, ls in entries for k in ls if k.node_id in wanted],
                             node_ids, parents or {})
        return excerpts_for(by_source, links, budget=config.full_text_tokens(),
                            exam_sources=self._exam)

    def for_spans(self, user_id: str, source_id: str,
                  spans: Sequence[UnitSpan]) -> list[SourceExcerpt]:
        for src, secs, _ in self._data.get(user_id, []):
            if src.source_id == source_id and src.deleted_at is None:
                return excerpts_for_spans(src, secs, spans, budget=config.full_text_tokens())
        raise LookupError(f"没有这份资料：{source_id}")

    def page_texts(self, user_id: str, source_id: str,
                   pages: Sequence[int]) -> dict[int, str]:
        for src, secs, _ in self._data.get(user_id, []):
            if src.source_id == source_id and src.deleted_at is None:
                return pages_of(secs, pages)
        raise LookupError(f"没有这份资料：{source_id}")


# --- a reading unit's own text (reading-mode spec §4) --------------------------------------------

@runtime_checkable
class SpanExcerpts(Protocol):
    """A material reader that also cuts a reading unit's own text (the pooled and memory readers
    here); a reading page is written only through one (`refine_apply.with_sources`)."""

    def for_spans(self, user_id: str, source_id: str,
                  spans: Sequence[UnitSpan]) -> list[SourceExcerpt]: ...


class SpanGone(LookupError):
    """A reading unit's span is not in its material any more (its section gone, or its range
    outside the section's paragraphs). The text names the section id: for the log — a route
    tells the learner `MSG_SPAN_GONE` instead."""


MSG_SPAN_GONE = "这一段的原文在资料里找不到了，请重新整理这份资料后再试"
SpanGone.public_message = "This source passage is no longer available. Reorganize the material and try again."
SpanGone.error_code = "source_passage_missing"
SpanGone.params = {}


def _span_text(sec: SectionRow, span: UnitSpan) -> str:
    """The paragraphs of `span` (the whole section when it has no range), cut by the one
    paragraph split (`labels.paragraphs`) and joined as the section has them; a part that does not
    open with a page mark gets its first page's 〔p.N〕 in front (the writer cites by the marks).
    SpanGone: the range is outside the section's paragraphs now (its text changed and the
    reading was not re-anchored: fail closed, never another stretch of text)."""
    if span.whole:
        return sec.text
    paras = paragraphs(sec.text, page=sec.page_from)
    a, b = span.para_from or 0, span.para_to if span.para_to is not None else 0
    if not 0 <= a <= b < len(paras):
        raise SpanGone(f"第 {a}-{b} 段不在这一节里了：{sec.section_id}")
    picked = paras[a:b + 1]
    text = "\n\n".join(p.text for p in picked)
    first = picked[0]
    if first.page is not None and not config.PAGE_MARK_RE.match(first.text):
        text = f"〔p.{first.page}〕{text}"
    return text


def excerpts_for_spans(source: SourceRow, sections: Sequence[SectionRow],
                       spans: Sequence[UnitSpan], *, budget: int) -> list[SourceExcerpt]:
    """One excerpt per span of a reading unit, in span order (reading-mode spec §4: a reading page
    reads its unit's paragraphs, never the whole sections its teaches link to): the span's text
    when all of them fit `budget` (tokens), else None with the section's summary (or the part's
    opening) standing in, as `excerpts_for` does; pages from the span (its paragraphs' 〔p.N〕
    marks), else the section's. Role `teaches` (`reads` stays the older reading pages' links).
    SpanGone: a section the material no longer has, or a range outside it."""
    by_id = {s.section_id: s for s in sections}
    labels = section_labels(sections)
    found: list[tuple[SectionRow, UnitSpan, str]] = []
    for sp in spans:
        sec = by_id.get(sp.section_id)
        if sec is None:
            raise SpanGone(f"没有这一节：{sp.section_id}")
        found.append((sec, sp, _span_text(sec, sp)))
    full = sum(sec.tokens if sp.whole else estimate_tokens(text)
               for sec, sp, text in found) <= budget
    out = []
    for sec, sp, text in found:
        out.append(SourceExcerpt(
            source_id=source.source_id, source_title=prompt_title(source.title),
            section_id=sec.section_id, label=labels[sec.section_id], title=sec.title,
            page_from=sp.page_from if sp.page_from is not None else sec.page_from,
            page_to=sp.page_to if sp.page_to is not None else sec.page_to,
            role="teaches", text=text if full else None,
            # a part of a section: its own opening (the section's summary is about all of it)
            summary=(sec.summary if sp.whole and sec.summary else "") or lead(text),
            page_offset=source.page_offset, glossary=source.glossary, lead=lead(text)))
    return out
