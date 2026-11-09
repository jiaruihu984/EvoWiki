"""The citations of one assistant turn (`sources.chat_cite` over the learner's materials): the check a
committed answer goes through, and the hold that keeps a raw mark off the screen while the answer
streams. Reading a material fails closed: the citation then says it was not checked.

Words the model has from memory are no reading (`saw`): a citation counts against what this turn's
request holds, without recalled history and without the model's own earlier answers, or against a
stored read of the page's image, and the mark says which. A page with a picture has no text to
check against but its image's read (`ChatSource.image_pages`)."""

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from psycopg_pool import ConnectionPool

from evowiki.llm.chat import ChatRequest, DocPart, ImagePart, StreamSink
from evowiki.sources import store
from evowiki.sources.chat_cite import ChatSource, bare_pages, check_marks, needs_check
from evowiki.sources.cite import quote_in
from evowiki.sources.config import PAGE_MARK_RE
from evowiki.sources.excerpts import pages_of
from evowiki.sources.models import SectionRow, SourceRow

log = logging.getLogger(__name__)

MAX_HOLD = 2000                  # chars: a bracket this long is no citation
MEMORY_FENCE = "<历史资料>"       # a memory tool's result (`llm.chat.memory_fenced`)


def _chat_source(row: SourceRow) -> ChatSource:
    return ChatSource(row.source_id, row.title, row.page_offset, row.pages is not None,
                      row.image_pages, row.earlier_titles)


class CitationReader(Protocol):
    def sources(self, user_id: str) -> list[ChatSource]:
        """His live materials."""
        ...

    def sections(self, user_id: str, source_id: str) -> list[SectionRow]: ...

    def page_read(self, user_id: str, source_id: str, page: int) -> str:
        """The page as a seeing model read it, "" when it did not."""
        ...

    def gone(self, user_id: str, ids: set[str]) -> set[str]:
        """Those of `ids` that are deleted or cleaned up by now."""
        ...


class PgCitationReader:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def sources(self, user_id: str) -> list[ChatSource]:
        with self._pool.connection() as conn:
            return [_chat_source(r) for r in store.list_sources(conn, user_id)]

    def sections(self, user_id: str, source_id: str) -> list[SectionRow]:
        with self._pool.connection() as conn:
            return store.sections(conn, user_id, source_id)

    def page_read(self, user_id: str, source_id: str, page: int) -> str:
        with self._pool.connection() as conn:
            got = store.page_reads(conn, user_id, source_id).get(page)
        return (got.text or "") if got is not None and got.by == "claude" else ""

    def gone(self, user_id: str, ids: set[str]) -> set[str]:
        with self._pool.connection() as conn:
            rows = {i: store.get_source(conn, user_id, i) for i in ids}
        return {i for i, r in rows.items() if r is None or r.deleted_at is not None}


class NoCitations:
    """No materials: every citation says it was not checked."""

    def sources(self, user_id: str) -> list[ChatSource]:
        return []

    def sections(self, user_id: str, source_id: str) -> list[SectionRow]:
        return []

    def page_read(self, user_id: str, source_id: str, page: int) -> str:
        return ""

    def gone(self, user_id: str, ids: set[str]) -> set[str]:
        return set()


class MemoryCitations:
    """Test double: {source: its sections} per reader; `deleted`: ids `gone` reports."""

    def __init__(self, data: Mapping[ChatSource, Sequence[SectionRow]],
                 deleted: set[str] | None = None,
                 reads: Mapping[tuple[str, int], str] | None = None) -> None:
        self._data = data
        self._deleted = deleted or set()
        self._reads = reads or {}

    def sources(self, user_id: str) -> list[ChatSource]:
        return list(self._data)

    def sections(self, user_id: str, source_id: str) -> list[SectionRow]:
        return next(list(v) for k, v in self._data.items() if k.source_id == source_id)

    def page_read(self, user_id: str, source_id: str, page: int) -> str:
        return self._reads.get((source_id, page), "")

    def gone(self, user_id: str, ids: set[str]) -> set[str]:
        return ids & self._deleted


class TurnCitations:
    """One turn's check; the sections it read are kept for the turn. `attached`: the session's
    attachments, which a name means first (two materials can share a title)."""

    def __init__(self, reader: CitationReader, user_id: str, attached: Sequence[str] = ()) -> None:
        self._reader, self._user, self._attached = reader, user_id, list(attached)
        self._known: list[ChatSource] | None = None
        self._read: dict[str, list[SectionRow]] = {}
        self._seen = ""                  # what the turn's latest request holds (`saw`)
        self._pictures = False

    def saw(self, request: ChatRequest) -> None:
        """What this request puts in front of the model: not a recalled message or a memory tool's
        result, not an earlier answer. A page shown as a picture has no words to compare: the
        turn's quotes count as read."""
        out = [request.system]
        for m in request.messages:
            if m.role == "assistant" or m.origin == "memory" or MEMORY_FENCE in m.text:
                continue
            out.append(m.text)
            out.extend(p.text for p in m.parts if isinstance(p, DocPart))
            self._pictures = self._pictures or any(isinstance(p, ImagePart) for p in m.parts)
        self._seen = "\n".join(out)

    def _quote_seen(self, quote: str) -> bool:
        return self._pictures or quote_in(quote, self._seen)

    def _sources(self) -> list[ChatSource]:
        if self._known is None:
            try:
                got = self._reader.sources(self._user)
            except Exception:
                log.warning("could not list the materials of %s", self._user, exc_info=True)
                return []
            first = {sid: i for i, sid in enumerate(self._attached)}
            self._known = sorted(got, key=lambda s: first.get(s.source_id, len(first)))
        return self._known

    def _sections(self, source_id: str) -> list[SectionRow]:
        if source_id not in self._read:
            try:
                self._read[source_id] = self._reader.sections(self._user, source_id)
            except Exception:
                log.warning("could not read material %s", source_id, exc_info=True)
                return []
        return self._read[source_id]

    def _stored(self, source_id: str, page: int) -> str:
        try:
            return self._reader.page_read(self._user, source_id, page)
        except Exception:
            log.warning("could not read page %s of %s", page, source_id, exc_info=True)
            return ""

    def _page(self, source_id: str, page: int) -> str:
        src = next((s for s in self._sources() if s.source_id == source_id), None)
        text = ("" if src is not None and page in src.image_pages
                else pages_of(self._sections(source_id), [page]).get(page, ""))
        return f"{text}\n{self._stored(source_id, page)}"

    def _whole(self, source_id: str) -> str:
        return "\n".join(PAGE_MARK_RE.sub("", s.text) for s in self._sections(source_id)
                         if s.read_by != "none")

    def __call__(self, text: str) -> str:
        if not needs_check(text):
            return text
        return bare_pages(check_marks(text, self._sources(), self._page, self._whole,
                                      self._quote_seen, self._stored), self._sources())


class HeldSink:
    """A `StreamSink` that shows no raw mark: text from a 〔 or （ on waits until its closing bracket,
    and goes out checked (the same text the committed answer will have). What is still held when
    the call ends is the committed text's to show. A bracket with a line break, or over MAX_HOLD,
    is no citation."""

    def __init__(self, inner: StreamSink, check: Callable[[str], str]) -> None:
        self._inner, self._check = inner, check
        self._held = ""

    def text(self, chunk: str) -> None:
        buf, out = self._held + chunk, ""
        while (start := min((i for i in (buf.find("〔"), buf.find("（")) if i >= 0),
                            default=-1)) >= 0:
            out += buf[:start]
            buf = buf[start:]
            end, nl = buf.find("〕" if buf[0] == "〔" else "）"), buf.find("\n")
            if end >= 0 and (nl < 0 or end < nl):
                out += self._check(buf[:end + 1])
                buf = buf[end + 1:]
            elif nl >= 0 or len(buf) > MAX_HOLD:
                out += buf[0]
                buf = buf[1:]
            else:
                break
        else:
            out, buf = out + buf, ""
        self._held = buf
        if out:
            self._inner.text(out)
