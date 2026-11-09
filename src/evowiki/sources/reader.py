"""User-scoped material reading contracts and their storage implementations."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol

from psycopg_pool import ConnectionPool

from evowiki.retrieval.embed import Embedder
from evowiki.sources import store
from evowiki.sources.claude_pages import ClaudePages
from evowiki.sources.config import PAGE_MARK_RE
from evowiki.sources.document import SourceDocument
from evowiki.sources.models import SectionRow, SourceRow
from evowiki.sources.precise import read_precise
from evowiki.sources.search import SourceHit, lexical_sources, search_sources


class SourceReader(Protocol):
    def list(self, user_id: str) -> list[SourceRow]: ...

    def get(self, user_id: str, source_id: str) -> SourceRow | None:
        """The user's live source (another user's, unknown or deleted → None)."""
        ...

    def sections(self, user_id: str, source_id: str) -> list[SectionRow]: ...

    def search(self, user_id: str, query: str, source_ids: list[str] | None,
               k: int) -> list[SourceHit]: ...

    def lexical(self, user_id: str, phrases: Sequence[str], source_ids: list[str] | None,
                k: int) -> list[SourceHit]:
        """Only chunks that contain one of the phrases word for word (`sources.search.
        phrase_hits`): no vector neighbours."""
        ...

    def precise(self, user_id: str, source_id: str, pages: Sequence[int],
                progress: Callable[[str], None] | None = None) -> dict[int, str | None]:
        """A PDF's pages read from their images (`sources.precise`); None: could not be read.
        `progress`: the line to show while it waits."""
        ...


class PooledSourceReader:
    """`SourceReader` over the source tables; one pool connection per call, returned right
    after (a turn never keeps one open). Deleted sources are invisible."""

    def __init__(self, pool: ConnectionPool, embedder: Embedder,
                 pages: ClaudePages | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._pool, self._embedder, self._page_reader, self._clock = pool, embedder, pages, clock

    def precise(self, user_id: str, source_id: str, pages: Sequence[int],
                progress: Callable[[str], None] | None = None) -> dict[int, str | None]:
        reader = self._page_reader.for_user(user_id) if self._page_reader is not None else None

        def say(todo: Sequence[int]) -> None:
            if progress is not None:
                progress(f"正在看第 {page_ranges(todo)} 页")
        return read_precise(self._pool, reader, user_id, source_id, pages, now=self._clock, say=say)

    def list(self, user_id: str) -> list[SourceRow]:
        with self._pool.connection() as conn:
            return store.list_sources(conn, user_id)

    def get(self, user_id: str, source_id: str) -> SourceRow | None:
        with self._pool.connection() as conn:
            row = store.get_source(conn, user_id, source_id)
        return row if row is not None and row.deleted_at is None else None

    def sections(self, user_id: str, source_id: str) -> list[SectionRow]:
        with self._pool.connection() as conn:
            return store.sections(conn, user_id, source_id)

    def document(self, user_id: str, source_id: str) -> SourceDocument | None:
        with self._pool.connection() as conn:
            return store.get_document(conn, user_id, source_id)

    def search(self, user_id: str, query: str, source_ids: list[str] | None,
               k: int) -> list[SourceHit]:
        return search_sources(self._pool, user_id, query, embedder=self._embedder,
                              source_ids=source_ids, k=k)

    def lexical(self, user_id: str, phrases: Sequence[str], source_ids: list[str] | None,
                k: int) -> list[SourceHit]:
        return lexical_sources(self._pool, user_id, phrases, source_ids=source_ids, k=k)


def split_pages(text: str) -> list[tuple[int | None, str]]:
    """(page, text) blocks of a section's text, split at its 〔p.N〕 markers. Text before the
    first marker (or text without markers) has page None."""
    out: list[tuple[int | None, str]] = []
    page: int | None = None
    pos = 0
    for m in PAGE_MARK_RE.finditer(text):
        if text[pos:m.start()].strip():
            out.append((page, text[pos:m.start()].strip()))
        page, pos = int(m.group(1)), m.end()
    if text[pos:].strip():
        out.append((page, text[pos:].strip()))
    return out


class MemorySourceReader:
    """In-memory `SourceReader` for tests: {user_id: [(source, sections)]}. Search is a plain
    substring match over page blocks (the whole query, else its whitespace-separated words)."""

    def __init__(self, data: Mapping[str, Sequence[tuple[SourceRow, Sequence[SectionRow]]]],
                 pages: Mapping[int, str] | None = None):
        self._data = data
        self._pages = pages or {}

    def precise(self, user_id: str, source_id: str, pages: Sequence[int],
                progress: Callable[[str], None] | None = None) -> dict[int, str | None]:
        return {p: self._pages.get(p) for p in pages}

    def list(self, user_id: str) -> list[SourceRow]:
        return [s for s, _ in self._data.get(user_id, ()) if s.deleted_at is None]

    def get(self, user_id: str, source_id: str) -> SourceRow | None:
        return next((s for s in self.list(user_id) if s.source_id == source_id), None)

    def sections(self, user_id: str, source_id: str) -> list[SectionRow]:
        for s, secs in self._data.get(user_id, ()):
            if s.source_id == source_id and s.deleted_at is None:
                return sorted(secs, key=lambda x: x.ord)
        return []

    def search(self, user_id: str, query: str, source_ids: list[str] | None,
               k: int) -> list[SourceHit]:
        words = [query] + query.split()
        hits: list[SourceHit] = []
        for s in self.list(user_id):
            if source_ids is not None and s.source_id not in source_ids:
                continue
            for sec in self.sections(user_id, s.source_id):
                for i, (page, block) in enumerate(split_pages(sec.text)):
                    score = sum(1 for w in words if w and w in block)
                    if score:
                        hits.append(SourceHit(chunk_id=f"{sec.section_id}-{i}",
                                              source_id=s.source_id, section_id=sec.section_id,
                                              page=page, text=block, score=float(score)))
        hits.sort(key=lambda h: (-h.score, h.chunk_id))
        return hits[:k]

    def lexical(self, user_id: str, phrases: Sequence[str], source_ids: list[str] | None,
                k: int) -> list[SourceHit]:
        """A page block containing one of the phrases verbatim."""
        hits: list[SourceHit] = []
        for s in self.list(user_id):
            if source_ids is not None and s.source_id not in source_ids:
                continue
            for sec in self.sections(user_id, s.source_id):
                for i, (page, block) in enumerate(split_pages(sec.text)):
                    score = sum(1 for p in phrases if p and p in block)
                    if score:
                        hits.append(SourceHit(chunk_id=f"{sec.section_id}-{i}",
                                              source_id=s.source_id, section_id=sec.section_id,
                                              page=page, text=block, score=float(score)))
        hits.sort(key=lambda h: (-h.score, h.chunk_id))
        return hits[:k]


def page_ranges(pages: Iterable[int]) -> str:
    """[5, 6, 7, 9] -> "5–7、9"."""
    ps = sorted(set(pages))
    runs: list[list[int]] = []
    for p in ps:
        if runs and p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return "、".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)
