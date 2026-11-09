"""The import workflow body (design §5), shared by both runners (`sources.runs`): inline it runs
in a virtual workflow, under DBOS it is `sources.flows.source_workflow`. Nothing here knows DBOS.

Every external or nondeterministic thing happens inside a `durable.step` (reads of the stored
file, parsing, the readers, the model, the embedder, every write, ids); the body between steps
only computes from step results, so a recovered workflow replays to where it stopped. Step
values are JSON.

Each step first checks that the source is still there and that this run is still its newest
generation (`ImportJob.gen` = the row's `run_gen`; a retry and a learning use without a full
import start the next one); a write that fails because the source was deleted meanwhile (the store refuses
writes to deleted sources) is the same thing: the run stops quietly, without `done`. Progress
items are written to the source row (`set_progress`) before they are emitted, so the list shows
them without following the stream; the end record (`end`: done / error) is stored before the
end item is emitted, so a follower that stops at it never sees an unfinished row. A step re-run
after a crash emits its progress items again (harmless: followers may see a repeated progress
item; end items are never repeated).

Steps: parse (running headers stripped, then pages judged) → layout (every PDF run, whatever is
configured, so a replay has the same steps: a document an earlier run stored is merged page by page
into the text layer, which keeps its bookmarks (`document.merge_text_layer`); pages taken from the
layout carry no OCR hint (they were read from their image already). With none stored, `wants_layout`
says whether this run parses the whole PDF with MinerU / Docling and stores it (`source_document`):
a learning run does, a chat attachment only for its scanned pages or `ImportJob.layout_now`. A parse
that fails stores a pypdfium2 placeholder with pages only, is logged, and the run goes on with the
text layer; a later parsing run retries a placeholder; no parser or an unavailable one leaves the
text layer as it is; a delete stops the parse between batches) → vision (pages an earlier generation
read already; the unreadable pages themselves when there are at most PRE_READ_MAX of them) → toc
→ chunk (chunks stored without vectors: BM25 finds them). The source is usable from there
(status `ready` / `partial`, the `usable` item; the parse step writes the printed-page offset,
found before the running lines are stripped); vectors take minutes on a CPU, so they come
after. Then, in the same workflow: embed (chunk and section vectors, batch by batch) → ocr (the
hinted pages and the remaining unreadable pages) → patch (each page read is swapped into its
section by its 〔p.N〕 mark, no new toc; only the changed sections are re-chunked and
re-embedded; status and unreadable pages updated) → summary → `done`. While these run the row's
status stays usable (progress stages "embed" / "ocr").

The glossary step (mode "full" only, after chunk, before `usable`) is one fast-tier call; its failing never
fails the import (the glossary stays empty and the failure is logged; nothing is shown to the
user).

Reading pages (`_Run.read_pages`): what a reader read is stored (`page_reads`) and never read
again. The local reader goes first. Claude reads only unreadable pages with something to see (a
figure block, or an image on the page) the local reader could not read or judged a figure; a page
of few characters and no image is not sent, and one the local reader found empty counts as read.
Only in mode "full", however many there are: there is no cost
quote, the user's own spending limit is the only stop (the run then ends with that reason; what
was read is kept). Hinted pages (wants_ocr on a text page) are read locally
or not at all; when that fails they keep their text layer.

SCAN_ON_DEMAND_PAGES (`reads_on_demand`): a short scan in a chat is read page by page as it is asked
about, not parsed. Mode "ask" (a chat attachment): no Claude, no summaries; embedded only when it is too
long to be handed over whole (`full_text_tokens`); an image is read locally only when the
assistant's model cannot see images.

Text first: a chat attachment's PDF whose pages have a text layer is not parsed (`wants_layout`). The
run reads no page image and asks no model for its toc, so it is usable in seconds and takes no GPU;
its text never changes after that (no background page reads: a running turn keeps what it read). The
row says so (`text_first`, `imprecise_pages`: the pages the judge finds formulas, tables or pictures
on), the turn's statement tells the model, and it reads such a page from its image on demand
(`sources.precise`), stored as a page read, never swapped into the sections by this mode. A slide's
formula pasted as a picture leaves clean text with nothing broken to notice, so any page with a
picture of some size counts (`PICTURE_AREA`), and so does one with formulas drawn in vector shapes
(`DRAWN_MARKS`). A fully parsed PDF has them too, unless it is a scan: the parser's text of such a page
is a recognition with the same errors (and for a formula set in Unicode math, the same pieces), and a
chat turn reads it from its image all the same. The page writer works from the parsed sections. In a chat attachment those pages are `image_pages`:
the turn's prompt leaves their text out, read_source reads them from their image only, so the model
has nothing to guess from (`assistant.attachments`). Such a page is not unreadable, whatever its
text layer holds: a turn reads it.
A learning need parses it: `uses.await_layout` before a goal is planned (`layout_now`, the sections
are rebuilt and the planner waits) or the full run of a learning use.

Keeping the sections (`ImportJob.keep_sections`, a later run of a source: a learning use of a chat
attachment, a retry): the file has not changed, only what the run does with it, so when the
source has sections already they stay as they are: their ids, the titles and levels the user
edited, the knowledge points linked to them. The keep step takes the place of vision and toc;
the run then only does what its mode adds to what is there: chunks for the sections that have
none, the glossary (mode "full"), the missing vectors, the unreadable pages read (what was read
before is reused; Claude in mode "full" only) and swapped into the section that holds them (a
read already in the sections is not swapped in again), summaries when the material is long and
the mode is "full". With no sections yet (the earlier run had not got to its toc) the toc is
built as usual. Which later runs keep them: `sources.uses.next_run`. The one exception: text-first
sections with a parsed document now are built again from it, when the user has not edited the
outline and no node links to them (nothing can point at the old ids); otherwise they stay and only
their imprecise pages take the parsed text.

A failure after the source became usable (embedding; a 402 or the user's spending cap at the
toc, while Claude reads pages or while summarizing) leaves that status as it is and ends the
stream with `error`; a failure before it marks the source `failed` with the user-facing reason."""

import dataclasses
import logging
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent import durable
from evowiki.clock import Clock
from evowiki.goals.context import estimate_tokens
from evowiki.goals.observe import JsonLLM
from evowiki.llm.providers import ProviderRejectedError
from evowiki.llm.run_context import traced_entry
from evowiki.llm.usage import BudgetExceededError
from evowiki.retrieval.embed import Embedder
from evowiki.sources import store
from evowiki.sources.chunk import chunk_section
from evowiki.sources.config import PAGE_MARK, PAGE_MARK_RE
from evowiki.sources.document import DocPage, SourceDocument, merge_text_layer
from evowiki.sources.glossary import extract_glossary, with_locators
from evowiki.sources.headers import strip_running_lines
from evowiki.sources.models import ChunkRow, PageRead, SectionRow, SourceRow, SourceStatus
from evowiki.sources.parsed import LayoutHead, OutlineEntry, PageText, ParsedDoc, ParseError
from evowiki.sources.printed import find_page_offset
from evowiki.sources.summary import needs_summaries, summarize, text_hash
from evowiki.sources.titles import clean_title, generic_title, looks_like_title
from evowiki.sources.toc import build_toc
from evowiki.sources.unreadable import DRAWN_MARKS, OCR_HINTS, PICTURE_AREA, wants_ocr
from evowiki.sources.vision import FIGURE_MIN_AREA, VisionReader, strip_markup
from evowiki.store.bot import renamed_in_drafts
from evowiki.wikiapp.llm import LLM_BUDGET, PROVIDER_REJECTED_DETAIL

log = logging.getLogger("evowiki.sources.importer")

EMBED_BATCH = 64
SECTION_HEAD_CHARS = 500         # a section's vector: its title plus the start of its text
PRE_READ_MAX = 20                # at most this many unreadable pages are read before usable
RENDER_BATCH = 8                 # page images rendered at a time (150 dpi PNGs are large)
NOTE_NO_LLM = "没有生成摘要（模型不可用）"
MSG_NO_FILE = "找不到这份资料的原件，可以删掉重新上传"
MSG_CRASHED = "导入时出错了，可以删掉重新上传"
MSG_LATE_CRASH = "整理资料时出错了，已经读好的部分照常能用"
MSG_VISION_FAILED = "有几页看图没读成，已经读好的部分照常能用"
PLACEHOLDER_BACKEND = "pypdfium2"   # source_document of a failed layout parse (pages only)
_PAGED = ("pdf", "pptx")          # kinds whose page count means something to the user
_SEEN = ("pdf", "image")          # kinds whose pages have an image a reader can read
_SPACE = re.compile(r"\s+")

Stage = Literal["parse", "detect", "toc", "embed", "ocr", "summary"]
Mode = Literal["full", "ask"]


class LayoutParser(Protocol):
    """Parses a whole PDF into a `SourceDocument` (`layout_client.LayoutWorker`); None when it
    could not (logged by the parser). `on_batch(done_pages, pages)` after each batch;
    `should_stop()` before each batch: True stops the parse (None)."""

    def available(self) -> bool: ...

    def parse_pdf(self, data: bytes, pages: int, *,
                  on_batch: Callable[[int, int], None] | None = None,
                  should_stop: Callable[[], bool] | None = None) -> SourceDocument | None: ...


SCAN_ON_DEMAND_PAGES = 30
MSG_LAYOUT_START = "正在启动版面解析"
MSG_TOC = "正在排目录"


class ImportJob(BaseModel):
    """The workflow's only argument (JSON)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: str
    source_id: str
    gen: int = Field(default=0, ge=0)   # the source's run_gen this run was started for
    mode: Mode = "full"                 # "ask": a chat attachment (module doc)
    # a later run (a use change, a retry): the sections there are stay (module doc)
    keep_sections: bool = False
    # mode "ask" too parses the whole PDF: a goal is about to be planned from it (module doc)
    layout_now: bool = False


class SourceProgress(BaseModel):
    """SSE event "progress"; also the latest record on the source row."""

    stage: Stage
    done: int
    total: int
    message: str                  # what the card shows, e.g. 「正在读 3/12 节」


class SourceDone(BaseModel):
    """SSE events "usable" (the source can be used, reading goes on) and "done" (finished)."""

    status: SourceStatus
    pages: int | None
    sections: int
    unreadable_pages: list[int]
    note: str | None
    title: str | None = None      # the title now: the file name or url may have given way


class SourceFailed(BaseModel):
    """SSE event "error"; code is semantic and detail is presentation text."""

    detail: str
    code: str | None = None


@dataclass
class ImportDeps:
    pool: ConnectionPool
    embedder: Embedder
    llm_for: Callable[[str], JsonLLM | None]   # user_id -> fast-tier JsonLLM; None: no gateway
    local: VisionReader                        # LayoutWorker, or NoVision
    claude: VisionReader                       # ClaudePages, or NoVision
    assistant_sees_images: Callable[[str], bool]   # user_id -> the assistant runs on Claude
    clock: Clock
    new_id: Callable[[], str]
    # (user_id, source_id) once the import has finished well: the supplement match
    # (`sources.match_flow.start_match`); None: nothing follows an import
    on_ready: Callable[[str, str], None] | None = None
    # (user_id, source_id, changed section ids) once page reads changed sections' text: the
    # readings of the material are re-anchored (`wikiapp.reading.reanchor_readings`; raising
    # fails the step: fail closed); None: nothing to re-anchor
    on_text_changed: Callable[[str, str, list[str]], None] | None = None
    # parses whole PDFs before the page reading (module doc); None: no layout step
    layout: LayoutParser | None = None


class _Deleted(Exception):
    """The source was deleted (or is not this user's), or a newer run took over: stop without
    an end item."""


BUDGET_OUTCOME = "这份资料没有读完"
HEADING_TITLE_PAGES = 2     # a first heading further in is a chapter's, not the document's title


class _TocBudget(Exception):
    """The toc step met the user's spending cap (its step value holds the sentence to end with)."""


class _VisionStop(Exception):
    """Claude stopped reading pages; the durable value carries code/detail/params."""

    def __init__(self, stop: dict[str, Any]):
        super().__init__(stop["detail"])
        self.stop = stop


# ---- JSON forms of the step values ----------------------------------------------------------

def _doc_json(doc: ParsedDoc) -> dict[str, Any]:
    return dataclasses.asdict(doc)


def _doc_from(d: dict[str, Any]) -> ParsedDoc:
    return ParsedDoc(kind=d["kind"], title=d["title"],
                     pages=tuple(PageText(**p) for p in d["pages"]),
                     outline=tuple(OutlineEntry(**o) for o in d["outline"]),
                     headings_inline=d["headings_inline"],
                     # absent in step values recorded before layout parsing
                     layout_heads=tuple(LayoutHead(**h) for h in d.get("layout_heads", ())))


def _pdfium_version() -> str:
    from importlib.metadata import version
    return version("pypdfium2")


def _judged(doc: ParsedDoc) -> ParsedDoc:
    """Ruling A: the density / garble judge is for PDF text layers only. Elsewhere only an
    empty page is unreadable (the parsers mark those already; this repeats it, harmlessly)."""
    if doc.kind == "pdf":
        from evowiki.sources.unreadable import judge_all
        pages = judge_all(doc.pages)
    else:
        pages = tuple(dataclasses.replace(p, read_by="none", reason="no_text")
                      if p.read_by == "text" and not p.text.strip() else p for p in doc.pages)
    return dataclasses.replace(doc, pages=pages)


def _layout_judged(doc: ParsedDoc, taken: set[int]) -> ParsedDoc:
    """`_judged`, then the pages taken from the layout parse lose their OCR hints: the layout
    parser read them from their image already (an OCR pass would read them again and the patch
    step would replace the layout text). A layout page the judge finds unreadable stays so."""
    judged = _judged(doc)
    pages = tuple(dataclasses.replace(p, signals=tuple(s for s in p.signals if s not in OCR_HINTS))
                  if p.page in taken else p for p in judged.pages)
    return dataclasses.replace(judged, pages=pages)


def _stripped(doc: ParsedDoc) -> ParsedDoc:
    """Running headers, footers and page numbers off paged text, before the judge sees it."""
    if doc.kind not in _PAGED:
        return doc
    return dataclasses.replace(doc, pages=strip_running_lines(doc.pages))


def _parse_stored(row: SourceRow, data: bytes, mime: str) -> ParsedDoc:
    from evowiki.sources.parse import parse_bytes, parse_fetched, parse_text
    from evowiki.sources.parse_other import decode_text
    if row.kind == "text":                 # pasted text: the stored bytes are the text
        return parse_text(decode_text(data), title_hint=row.title)
    if row.kind == "url":                  # Ruling B: fetched once at upload, re-parsed by mime
        return parse_fetched(data, mime, url=row.origin or "", title_hint=None)
    return parse_bytes(row.kind, data, title_hint=row.title)


def _with_reads(doc: ParsedDoc, reads: list[list[Any]]) -> ParsedDoc:
    """Pages read from their image (items [page, text, by, figure]) replace their text layer."""
    got = {int(r[0]): r for r in reads}
    if not got:
        return doc
    pages = tuple(dataclasses.replace(p, text=str(got[p.page][1]), read_by="vision", reason=None,
                                      figure=bool(got[p.page][3]))
                  if p.page in got else p for p in doc.pages)
    return dataclasses.replace(doc, pages=pages)


def _hinted(doc: ParsedDoc) -> list[int]:
    """Text pages the judge would rather have OCR'd (they keep their text layer if that fails)."""
    return [p.page for p in doc.pages if p.read_by == "text" and wants_ocr(p)]


def _shows_more(p: PageText) -> bool:
    """The page shows what its text layer does not hold: a picture of some size, or formulas drawn
    in vector shapes (sample lecture: the text 「Mn x (t)」 for M_x^(n)(t), a clean-looking line)."""
    return p.image_area >= PICTURE_AREA or p.drawn_marks >= DRAWN_MARKS


def _pictured(doc: ParsedDoc) -> list[int]:
    return [p.page for p in doc.pages if p.read_by == "text" and _shows_more(p)]


def _imprecise(doc: ParsedDoc) -> list[int]:
    """The pages whose text layer may not hold what the page shows (module doc)."""
    return sorted({*_hinted(doc), *_pictured(doc)})


def _image_pages(doc: ParsedDoc) -> list[int]:
    return [p.page for p in doc.pages if _shows_more(p)]


def image_pages_of(kind: str, data: bytes) -> list[int] | None:
    """The same PDF attached again: its pages as this version of the check sees them. A row keeps
    what the check said when it was first imported, and the file comes back without a run."""
    from evowiki.sources.parse import parse_bytes
    doc = _judged(_stripped(parse_bytes(kind, data, title_hint="")))
    return None if kind == "pdf" and scanned(doc) else _image_pages(doc)   # a scan: what its run decided stays


def scanned(doc: ParsedDoc) -> bool:
    """Most of the pages unreadable. A slide that is mostly a picture or one garbled formula page
    is not a scan: the rest of the PDF can be used now and that page is read on demand."""
    return sum(1 for p in doc.pages if p.read_by == "none") * 2 > len(doc.pages)


def reads_on_demand(job: ImportJob, doc: ParsedDoc, claude: "VisionReader") -> bool:
    """A short scan attached to a chat: its pages have no text, and a seeing model reads the one
    asked about in about 2 seconds, for less than a cent. The parser's cold start alone is one to
    two minutes (live 2026-11-04: 3.5 minutes for two pages, with other work on the GPU)."""
    return (job.mode == "ask" and not job.layout_now and scanned(doc) and bool(_image_pages(doc))
            and len(doc.pages) <= SCAN_ON_DEMAND_PAGES and claude.available())


def wants_layout(job: ImportJob, text_layer: ParsedDoc) -> bool:
    """Whether this run parses the whole PDF when no document is stored yet (module doc)."""
    if job.mode == "full" or job.layout_now:
        return True
    return not job.keep_sections and scanned(text_layer)


def _swap_page(text: str, page: int, new: str) -> str:
    """Section text with page `page`'s segment (after its 〔p.N〕 mark) replaced by `new`, or
    put in page order when the section had no text for that page."""
    parts = PAGE_MARK_RE.split(text)       # [lead, page, segment, page, segment, ...]
    lead = parts[0].strip()
    segs = [(int(parts[i]), parts[i + 1].strip()) for i in range(1, len(parts), 2)]
    segs = sorted([s for s in segs if s[0] != page] + [(page, new.strip())], key=lambda s: s[0])
    blocks = [lead] if lead else []
    blocks += [PAGE_MARK.format(n=n) + "\n" + seg for n, seg in segs if seg]
    return "\n\n".join(blocks)



def _squash(text: str) -> str:
    """Text without page marks and whitespace: a page read split over two sections by the toc is
    still found whole in the sections' texts."""
    return _SPACE.sub("", PAGE_MARK_RE.sub("", text))


def _chunked(items: list[int], n: int) -> list[list[int]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def page_images(kind: str, data: bytes, pages: list[int]) -> list[bytes]:
    """The page images a reader reads (an image source is its one page)."""
    if kind == "image":
        return [data for _ in pages]
    from evowiki.sources.parse_pdf import render_pages
    return render_pages(data, pages)


def _require_generation(conn, uid: str, sid: str, gen: int | None) -> int:
    """Lock and validate the live generation on the transaction that commits its effects."""
    row = conn.execute("select run_gen,deleted_at from source where user_id=%s "
                       "and source_id=%s for update", (uid, sid)).fetchone()
    if row is None or row["deleted_at"] is not None or (gen is not None and row["run_gen"] != gen):
        raise _Deleted(sid)
    return row["run_gen"]


@contextmanager
def _generation_connection(pool: ConnectionPool, uid: str, sid: str, gen: int | None):
    """Fence short database work, never a parser/model/embedding call."""
    with pool.connection() as conn, conn.transaction():
        _require_generation(conn, uid, sid, gen)
        yield conn


def swap_reads(pool: ConnectionPool, uid: str, sid: str,
               reads: list[tuple[int, str]], *, gen: int | None = None) -> list[str]:
    """Each page read goes into the section that holds it (by its 〔p.N〕 mark, no new toc; a
    start page shared by sections is the last one's); returns the changed section ids (their
    summary and vector are dropped by the store)."""
    with _generation_connection(pool, uid, sid, gen) as conn:
        if gen is None:
            gen = store.get_source(conn, uid, sid).run_gen
        secs = store.sections(conn, uid, sid)
    texts = {s.section_id: s.text for s in secs}
    changed: list[str] = []
    for p, text in reads:
        holder = None
        for s in secs:
            if s.page_from is not None and s.page_to is not None \
                    and s.page_from <= int(p) <= s.page_to:
                holder = s
        if holder is None:
            log.warning("source %s/%s: no section holds page %s", uid, sid, p)
            continue
        texts[holder.section_id] = _swap_page(texts[holder.section_id], int(p), str(text))
        if holder.section_id not in changed:
            changed.append(holder.section_id)
    with _generation_connection(pool, uid, sid, gen) as conn:
        for sec_id in changed:
            t = texts[sec_id]
            store.update_section_text(conn, uid, sid, sec_id, text=t,
                                      tokens=estimate_tokens(t), read_by="text")
    return changed


def section_chunks(secs: list[SectionRow], new_id: Callable[[], str]) -> list[ChunkRow]:
    return [c for s in secs if s.read_by != "none" and s.text.strip()
            for c in chunk_section(s, new_id=new_id)]


def rechunk(pool: ConnectionPool, uid: str, sid: str, section_ids: list[str],
            new_id: Callable[[], str], *, gen: int | None = None) -> None:
    """New chunks (without vectors) for these sections."""
    with _generation_connection(pool, uid, sid, gen) as conn:
        mine = [s for s in store.sections(conn, uid, sid) if s.section_id in section_ids]
        store.put_chunks(conn, uid, sid, section_chunks(mine, new_id), section_ids=section_ids)


def embed_pending(pool: ConnectionPool, uid: str, sid: str, emb: Embedder,
                  on_chunks: Callable[[int, int], None] | None = None, *,
                  gen: int | None = None) -> int:
    """Vectors for the chunks and sections that have none or have another vector space's (a
    changed embedding model), written batch by batch (search uses
    each batch as soon as it is in; a re-run after a crash does only what is left).
    `on_chunks(done, total)` after each chunk batch."""
    with _generation_connection(pool, uid, sid, gen) as conn:
        if gen is None:
            gen = store.get_source(conn, uid, sid).run_gen
        chunks = store.unembedded_chunks(conn, uid, sid, emb)
        todo = set(store.unembedded_sections(conn, uid, sid, emb))
    for i in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[i:i + EMBED_BATCH]
        vectors = emb.embed_documents([c.text for c in batch])
        with _generation_connection(pool, uid, sid, gen) as conn:
            store.set_chunk_embeddings(
                conn, uid, sid, [(c.chunk_id, v) for c, v in zip(batch, vectors, strict=True)],
                emb)
        if on_chunks is not None:
            on_chunks(min(i + EMBED_BATCH, len(chunks)), len(chunks))
    with _generation_connection(pool, uid, sid, gen) as conn:
        secs = [s for s in store.sections(conn, uid, sid) if s.section_id in todo]
    heads = [(s.title + "\n" + PAGE_MARK_RE.sub("", s.text)[:SECTION_HEAD_CHARS]).strip()
             for s in secs]
    for i in range(0, len(secs), EMBED_BATCH):
        vectors = emb.embed_documents(heads[i:i + EMBED_BATCH])
        with _generation_connection(pool, uid, sid, gen) as conn:
            store.set_section_embeddings(
                conn, uid, sid,
                [(s.section_id, v) for s, v in zip(secs[i:i + EMBED_BATCH], vectors,
                                                     strict=True)], emb)
    return len(chunks)


# ---- the run --------------------------------------------------------------------------------

class _Run:
    def __init__(self, job: ImportJob, emit: Callable[[str, BaseModel], None],
                 deps: ImportDeps):
        self.job = job
        self.uid, self.sid = job.user_id, job.source_id
        self.emit, self.deps = emit, deps

    def gone(self) -> bool:
        with self.deps.pool.connection() as conn:
            row = store.get_source(conn, self.uid, self.sid)
        return row is None or row.deleted_at is not None or row.run_gen != self.job.gen

    def step(self, name: str, fn: Callable[[], Any]) -> Any:
        def body() -> dict[str, Any]:
            if self.gone():
                return {"deleted": True}
            try:
                return {"ok": fn()}
            except Exception:
                if self.gone():          # e.g. LookupError from a write after a delete
                    return {"deleted": True}
                raise
        out = durable.step(name, body)
        if out.get("deleted"):
            raise _Deleted(self.sid)
        return out["ok"]

    def report(self, stage: Stage, done: int, total: int, message: str) -> None:
        """Inside a step: store the record, then emit it."""
        p = SourceProgress(stage=stage, done=done, total=total, message=message)
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            store.set_progress(conn, self.uid, self.sid, p.model_dump())
        self.emit("progress", p)

    def _file(self) -> tuple[SourceRow, bytes, str]:
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            row = store.get_source(conn, self.uid, self.sid)
            f = store.get_file(conn, self.uid, self.sid)
        if row is None:
            raise LookupError(f"source {self.sid} not found for user {self.uid}")
        if f is None:
            raise ParseError(MSG_NO_FILE)
        return row, f[0], f[1]

    # -- steps --

    def parse(self) -> dict[str, Any]:
        self.report("parse", 0, 1, "正在解析")
        row, data, mime = self._file()
        pptx_doc = None
        if row.kind == "pptx":              # the slide document is stored beside the section text
            from evowiki.sources.parse_pptx import parse_pptx
            raw, pptx_doc = parse_pptx(data, title_hint=row.title)
        else:
            raw = _parse_stored(row, data, mime)
        # printed page numbers are found before the running lines (page numbers too) are stripped
        offset = find_page_offset(raw.pages) if raw.kind == "pdf" else None
        doc = _judged(_stripped(raw))
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            store.set_page_offset(conn, self.uid, self.sid, offset)
            if pptx_doc is not None:
                store.put_document(conn, self.uid, self.sid, pptx_doc, self.deps.clock())
            if row.kind == "pdf" and generic_title(row.title) and not generic_title(doc.title):
                # a generic file name (「document」, a hash …): the PDF's own Title, cleaned
                self._retitle(conn, clean_title(doc.title), "document")
        n = len(doc.pages)
        bad = sum(1 for p in doc.pages if p.read_by == "none")
        self.report("detect", n, n, f"有 {bad} 页读不出字" if bad else "每一页都读得出字")
        return _doc_json(doc)

    def title(self) -> str | None:
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            row = store.get_source(conn, self.uid, self.sid)
        return None if row is None else row.title

    def _retitle(self, conn: Any, title: str, source: str) -> None:
        """A better title than the file name or url; drafts of goals still say what it replaced."""
        was = store.auto_title(conn, self.uid, self.sid, title, source)
        if was is not None:
            renamed_in_drafts(conn, self.uid, self.sid, was, title)

    def layout(self, text_layer: ParsedDoc) -> dict[str, Any]:
        """The layout parser's document merged into the text layer (module doc): {"doc": the
        merged ParsedDoc}, or {"unchanged": True} when the text layer stays as it is (no parser,
        unavailable, the parse failed, or this run does not parse: `wants_layout`). A document an
        earlier run stored is used again; a pypdfium2 placeholder (an earlier failed parse) counts
        as none."""
        layout = self.deps.layout
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            doc = store.get_document(conn, self.uid, self.sid)
        if doc is not None and doc.backend == PLACEHOLDER_BACKEND:
            doc = None
        if doc is None:
            if (layout is None or not wants_layout(self.job, text_layer)
                    or reads_on_demand(self.job, text_layer, self.deps.claude)):
                return {"unchanged": True}
            if not layout.available():
                log.warning("source %s/%s: layout parser unavailable: text layer only",
                            self.uid, self.sid)
                return {"unchanged": True}
            data = self._file()[1]
            n = len(text_layer.pages)
            self.report("parse", 0, n, MSG_LAYOUT_START)
            doc = layout.parse_pdf(data, n, on_batch=self._layout_progress,
                                   should_stop=self.gone)
            if doc is None and self.gone():      # deleted or re-run meanwhile: the parse stopped
                raise _Deleted(self.sid)
            if doc is None:
                log.warning("source %s/%s: layout parse failed: text layer only (pypdfium2)",
                            self.uid, self.sid)
                placeholder = SourceDocument(
                    kind="pdf", backend=PLACEHOLDER_BACKEND, version=_pdfium_version(),
                    pages=tuple(DocPage(p.page) for p in text_layer.pages), blocks=())
                with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
                    store.put_document(conn, self.uid, self.sid, placeholder, self.deps.clock())
                return {"unchanged": True}
            with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
                store.put_document(conn, self.uid, self.sid, doc, self.deps.clock())
        first = next(iter(doc.headings), None)
        if first is not None and first.page <= HEADING_TITLE_PAGES and looks_like_title(first.title):
            with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
                self._retitle(conn, clean_title(first.title), "document")
        laid = doc.to_parsed(text_layer.title)
        merged = _layout_judged(merge_text_layer(laid, text_layer),
                                {p.page for p in laid.pages if p.text})
        bad = sum(1 for p in merged.pages if p.read_by == "none")
        n = len(merged.pages)
        self.report("detect", n, n, f"有 {bad} 页读不出字" if bad else "每一页都读得出字")
        return {"doc": _doc_json(merged)}

    def _layout_progress(self, done: int, total: int) -> None:
        """`parse_pdf`'s batch callback: never raises (a raise there counts as a worker
        failure); a progress write that fails (e.g. the source was deleted) is logged."""
        try:
            self.report("parse", done, total, f"正在读版面 {done}/{total} 页")
        except Exception:
            log.warning("source %s/%s: layout progress not recorded", self.uid, self.sid,
                        exc_info=True)

    # -- reading pages from their images --

    def _images(self, kind: str, data: bytes, pages: list[int]) -> list[bytes]:
        return page_images(kind, data, pages)

    def _store_read(self, read: PageRead) -> None:
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            store.put_page_reads(conn, self.uid, self.sid, [read], self.deps.clock())

    def _local_ok(self, doc: ParsedDoc) -> bool:
        """Ask-mode images are the turn's: read here only when its model cannot see them."""
        if self.job.mode == "ask" and doc.kind == "image":
            return not self.deps.assistant_sees_images(self.uid)
        return True

    def _claude(self) -> VisionReader:
        reader = self.deps.claude
        for_user = getattr(reader, "for_user", None)    # ClaudePages: meter to this user
        return for_user(self.uid) if callable(for_user) else reader

    def read_pages(self, doc: ParsedDoc, unread: list[int], hinted: list[int], *, stage: Stage,
                   claude_ok: bool) -> dict[str, Any]:
        """Reads `unread` (read_by none) and `hinted` pages (module doc). Returns {"reads":
        [[page, text, by, figure]] with text, "stop": why Claude stopped, or None}."""
        wanted = sorted(set(unread) | set(hinted))
        out: dict[int, list[Any]] = {}
        stop: dict[str, Any] | None = None
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            stored = store.page_reads(conn, self.uid, self.sid) if wanted else {}
        pages = {x.page: x for x in doc.pages}

        def has_image(p: int) -> bool:
            return doc.kind == "image" or pages[p].image_area >= FIGURE_MIN_AREA

        def blank(p: int, r: PageRead) -> bool:
            """The local reader read this page and found nothing: not an unreadable page."""
            return p in unread and r.by == "local" and not r.text and not r.figure

        for p in wanted:
            r = stored.get(p)
            if r is not None and (r.text or blank(p, r)):
                out[p] = [p, r.text or "", r.by, r.figure]
        data: bytes | None = None

        def file_data() -> bytes:
            nonlocal data
            if data is None:
                data = self._file()[1]
            return data

        seen = doc.kind in _SEEN
        local = self.deps.local
        todo = ([p for p in wanted if p not in stored]
                if seen and self._local_ok(doc) and local.available() else [])
        done = 0
        for batch in _chunked(todo, RENDER_BATCH):
            for p, img in zip(batch, self._images(doc.kind, file_data(), batch), strict=True):
                res = local.read([img])
                if len(res) != 1:
                    raise ValueError(f"local reader returned {len(res)} reads for one page")
                done += 1
                self.report(stage, done, len(todo), f"正在看图读页 {done}/{len(todo)}")
                if res[0].text is None:
                    log.warning("source %s/%s page %d: local reader could not read it (%s)%s",
                                self.uid, self.sid, p, res[0].reason,
                                "" if p in unread else "; its text layer stays")
                    continue
                text = strip_markup(res[0].text).strip()
                figure = res[0].figure and (not res[0].thin_only or has_image(p))
                read = PageRead(p, text or None, "local", figure)
                self._store_read(read)
                stored[p] = read
                if text or blank(p, read):
                    out[p] = [p, text, "local", figure]

        if claude_ok and seen:
            # unreadable pages only: not read by Claude yet, a figure, or no local read of a page
            # that shows an image
            cands = [p for p in unread
                     if (r.by != "claude" and r.figure if (r := stored.get(p)) is not None
                         else has_image(p))]
            reader = self.deps.claude
            if cands and not reader.available():
                log.info("source %s/%s: %d pages need a seeing model; none configured",
                         self.uid, self.sid, len(cands))
            elif cands:
                stop = self._claude_read(doc, cands, out, file_data, stage)
        return {"reads": [out[p] for p in sorted(out)], "stop": stop}

    def _claude_read(self, doc: ParsedDoc, pages: list[int], out: dict[int, list[Any]],
                     file_data: Callable[[], bytes], stage: Stage) -> dict[str, Any] | None:
        """Claude reads `pages` one by one, each stored as soon as it is back (a 402 halfway
        keeps what was paid for). Returns why it stopped early, or None."""
        reader = self._claude()
        done = 0
        try:
            for batch in _chunked(pages, RENDER_BATCH):
                for p, img in zip(batch, self._images(doc.kind, file_data(), batch), strict=True):
                    res = reader.read([img])
                    if len(res) != 1:
                        raise ValueError(f"Claude reader returned {len(res)} reads for one page")
                    done += 1
                    self.report(stage, done, len(pages), f"正在看图读页 {done}/{len(pages)}")
                    text = (res[0].text or "").strip()
                    if not text:
                        log.warning("source %s/%s page %d: Claude read nothing (%s)",
                                    self.uid, self.sid, p, res[0].reason)
                        continue
                    self._store_read(PageRead(p, text, "claude", False))
                    out[p] = [p, text, "claude", False]
        except ProviderRejectedError:
            log.warning("source %s/%s: Claude page reading rejected (402?)", self.uid, self.sid)
            return {"code": "provider_rejected", "detail": PROVIDER_REJECTED_DETAIL, "params": {}}
        except BudgetExceededError as exc:
            log.warning("source %s/%s: Claude page reading over the user's budget",
                        self.uid, self.sid)
            return _budget_stop(exc)
        except Exception:
            if self.gone():
                raise
            log.exception("source %s/%s: Claude page reading failed", self.uid, self.sid)
            return {"code": "vision_failed", "detail": MSG_VISION_FAILED, "params": {}}
        return None

    def vision(self, doc: ParsedDoc, *, read: bool = True) -> dict[str, Any]:
        """Before the toc: pages an earlier generation read, and the unreadable pages when there
        are at most PRE_READ_MAX (`read` False: none, a text-first run reads no image).
        `attempted`: the pages read here (the ocr step skips them)."""
        full = self.job.mode == "full"
        unread = [p.page for p in doc.pages if p.read_by == "none"]
        now = unread if read and len(unread) <= PRE_READ_MAX else []
        res = self.read_pages(doc, now, [], stage="detect", claude_ok=full)
        rest = [p for p in unread + _hinted(doc) if p not in now]
        if rest:
            with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
                stored = store.page_reads(conn, self.uid, self.sid)
            got = {int(r[0]) for r in res["reads"]}
            res["reads"] = sorted(res["reads"] + [
                [p, r.text, r.by, r.figure] for p in rest
                if p not in got and (r := stored.get(p)) is not None and r.text])
        res["attempted"] = now
        # an ask image the assistant's model sees: the turn reads it, it is not unreadable
        res["turn"] = ([p.page for p in doc.pages if p.read_by == "none"]
                       if self.job.mode == "ask" and doc.kind == "image"
                       and not self._local_ok(doc) else [])
        return res

    def ocr(self, doc: ParsedDoc, unread: list[int], hinted: list[int],
            claude_ok: bool) -> dict[str, Any]:
        full = self.job.mode == "full"
        return self.read_pages(doc, unread, hinted, stage="ocr", claude_ok=claude_ok and full)

    def toc(self, doc: ParsedDoc, turn: list[int], *, quick: bool = False,
            imprecise: tuple[bool, list[int], list[int]] = (False, [], [])) -> dict[str, Any]:
        """Sections + usable status (`turn`: pages the chat turn sees itself, not unreadable). A 402 or the user's spending cap at the model's toc falls
        back to page groups (the source stays usable; the run ends with that reason). Returns
        metadata only: the later steps read the sections back from the store. `quick`: a
        text-first run, no model. `imprecise`: the row's (`text_first`, `imprecise_pages`,
        `image_pages`), written with the sections they describe."""
        rejected, budget = False, None
        if not quick:
            self.report("toc", 0, 1, MSG_TOC)
        try:
            toc = build_toc(doc, self.sid, llm=None if quick else self.deps.llm_for(self.uid),
                            new_id=self.deps.new_id)
        except (ProviderRejectedError, *LLM_BUDGET) as exc:
            budget = _budget_stop(exc) if isinstance(exc, BudgetExceededError) else None
            log.warning("source %s/%s: toc model %s: page groups", self.uid, self.sid,
                        "over the user's budget" if budget else "rejected (402?)")
            rejected = True
            toc = build_toc(doc, self.sid, llm=None, new_id=self.deps.new_id)
        for d in toc.diagnostics:
            log.info("source %s/%s toc: %s", self.uid, self.sid, d)
        unreadable = [p.page for p in doc.pages if p.read_by == "none" and p.page not in turn]
        status: SourceStatus = "partial" if unreadable else "ready"
        pages = len(doc.pages) if doc.kind in _PAGED else None
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            kept = store.own_summaries(conn, self.uid, self.sid)    # an earlier run's
            store.replace_sections(conn, self.uid, self.sid, list(toc.sections))
            again = [(s.section_id, kept[h], h) for s in toc.sections
                     if (h := text_hash(s.text)) in kept]
            if again:
                store.set_summaries(conn, self.uid, self.sid, again)
            store.set_status(conn, self.uid, self.sid, status=status, error=None, pages=pages,
                             unreadable_pages=unreadable, toc_mode=toc.mode)
            store.set_text_first(conn, self.uid, self.sid, text_first=imprecise[0],
                                 imprecise_pages=imprecise[1], image_pages=imprecise[2])
        n = len(toc.sections)
        self.report("toc", n, n, f"目录建好了，共 {n} 节")
        long = needs_summaries(toc.sections)
        full = self.job.mode == "full"
        return {"sections": n, "note": toc.note, "status": status, "pages": pages,
                "unreadable": unreadable, "rejected": rejected, "budget": budget,
                "summaries": full and long,       # decided here: replay-stable
                "embed": full or long,            # an ask attachment that is read whole: no
                "diagnostics": list(toc.diagnostics)}

    def keep(self, doc: ParsedDoc, layer: ParsedDoc, *, imprecise: tuple[bool, list[int], list[int]],
             quick: bool = False) -> dict[str, Any] | None:
        """The sections there are stay (module doc); None when there are none yet, or text-first
        ones that nothing holds on to and a parsed document can replace. Returns what `toc`
        returns, plus `turn` (as the vision step's), `reads`: the stored page reads that are in
        the sections already (items as the vision step's), so later steps do not swap them in
        again, and `parsed`: a parsed document's imprecise pages to swap into text-first sections
        (items likewise). `layer`: the text layer `doc` was merged into, if it was.
        `imprecise`: as `toc`'s."""
        secs = self._sections()
        if not secs:
            return None
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            row = store.get_source(conn, self.uid, self.sid)
            stored = store.page_reads(conn, self.uid, self.sid)
            linked = store.has_links(conn, self.uid, self.sid)
        from_layout = row is not None and row.text_first and not imprecise[0]
        if from_layout and not row.toc_edited and not linked:
            return None
        texts = {p.page: p.text for p in doc.pages}
        parsed = [[p, texts[p], "local", False] for p in _imprecise(layer)] if from_layout else []
        whole = _squash("".join(s.text for s in secs))
        reads = [[p, r.text, r.by, r.figure] for p, r in sorted(stored.items())
                 if r.text and _squash(r.text) in whole]
        turn = ([p.page for p in doc.pages if p.read_by == "none"]
                if self.job.mode == "ask" and doc.kind == "image" and not self._local_ok(doc)
                else imprecise[2] if quick else [])
        have = {int(x[0]) for x in reads}
        unreadable = [p.page for p in doc.pages
                      if p.read_by == "none" and p.page not in have and p.page not in turn]
        status: SourceStatus = "partial" if unreadable else "ready"
        pages = len(doc.pages) if doc.kind in _PAGED else None
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            store.set_status(conn, self.uid, self.sid, status=status, error=None, pages=pages,
                             unreadable_pages=unreadable)
            store.set_text_first(conn, self.uid, self.sid, text_first=imprecise[0],
                                 imprecise_pages=imprecise[1], image_pages=imprecise[2])
        n = len(secs)
        self.report("toc", n, n, f"目录沿用原来的，共 {n} 节")
        long = needs_summaries(secs)
        full = self.job.mode == "full"
        return {"sections": n, "note": None, "status": status, "pages": pages,
                "unreadable": unreadable, "rejected": False, "budget": None,
                "summaries": full and long, "embed": full or long, "diagnostics": [],
                "turn": turn, "reads": reads, "parsed": parsed}

    def _sections(self) -> list[SectionRow]:
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            return store.sections(conn, self.uid, self.sid)

    def glossary(self) -> dict[str, Any]:
        """One fast-tier call over the sections; any failure (or no model) leaves the glossary
        empty and is logged, never raised and never shown to the user."""
        llm = self.deps.llm_for(self.uid)
        if llm is None:
            log.warning("source %s/%s: no glossary: no model", self.uid, self.sid)
            return {"failed": True, "terms": 0, "dropped": 0}
        self.report("toc", 1, 1, "正在整理符号和术语")
        res = extract_glossary(self._sections(), llm=llm)
        if res.failed:
            log.warning("source %s/%s: no glossary: %s", self.uid, self.sid, res.reason)
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            doc = store.get_document(conn, self.uid, self.sid)
            entries = with_locators(res.entries, doc, self.sid)
            store.set_glossary(conn, self.uid, self.sid, entries, res.coverage)
        return {"failed": res.failed, "terms": len(res.entries), "dropped": res.dropped}

    def _chunks(self, secs: list[SectionRow]) -> list[ChunkRow]:
        return section_chunks(secs, self.deps.new_id)

    def chunk(self, *, missing: bool = False) -> int:
        """Chunks without vectors: the source can be searched (BM25) and used from here.
        `missing`: only for the sections that have none (kept sections keep their chunks)."""
        secs = self._sections()
        if missing:
            with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
                have = {c.section_id for c in store.chunks_of(conn, self.uid, [self.sid])}
            secs = [s for s in secs if s.section_id not in have]
        chunks = self._chunks(secs)
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            store.put_chunks(conn, self.uid, self.sid, chunks,
                             section_ids=[s.section_id for s in secs] if missing else None)
        return len(chunks)

    def embed(self, stage: Stage = "embed") -> int:
        """Vectors for the chunks and sections that have none (`embed_pending`)."""
        return embed_pending(
            self.deps.pool, self.uid, self.sid, self.deps.embedder,
            lambda n, total: self.report(stage, n, total, f"正在整理 {n}/{total} 段"),
            gen=self.job.gen)

    def patch(self, doc: ParsedDoc, reads: list[list[Any]], *, embedded: bool,
              turn: list[int]) -> dict[str, Any]:
        """Pages read after the toc go into the section that holds them; the changed sections
        are re-chunked and re-embedded (when the source was embedded). `doc` has the reads
        applied."""
        changed = swap_reads(self.deps.pool, self.uid, self.sid,
                             [(int(p), str(text)) for p, text, _by, _figure in reads],
                             gen=self.job.gen)
        if changed and self.deps.on_text_changed is not None:
            self.deps.on_text_changed(self.uid, self.sid, changed)
        if embedded and changed:      # new chunks for these sections, then their vectors
            rechunk(self.deps.pool, self.uid, self.sid, changed, self.deps.new_id,
                    gen=self.job.gen)
            self.embed("ocr")
        unreadable = [p.page for p in doc.pages if p.read_by == "none" and p.page not in turn]
        status: SourceStatus = "partial" if unreadable else "ready"
        read = {int(x[0]) for x in reads}
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            row = store.get_source(conn, self.uid, self.sid)
            store.set_status(conn, self.uid, self.sid, status=status, error=None,
                             unreadable_pages=unreadable)
            if row is not None and (row.imprecise_pages or row.image_pages):
                store.set_text_first(conn, self.uid, self.sid, text_first=row.text_first,
                                     imprecise_pages=[p for p in row.imprecise_pages
                                                      if p not in read],
                                     image_pages=[p for p in row.image_pages if p not in read])
        self.report("ocr", len(reads), len(reads), f"补读了 {len(reads)} 页")
        long = needs_summaries(self._sections())
        return {"status": status, "unreadable": unreadable,
                "summaries": self.job.mode == "full" and long}

    def summary(self) -> dict[str, Any]:
        """Cache hits first, then one call per section, each written as soon as it is back
        (a 402 halfway keeps what was paid for). A provider rejection propagates."""
        llm = self.deps.llm_for(self.uid)
        if llm is None:
            return {"skipped": True, "missing": 0}
        secs = self._sections()
        todo = [s for s in secs if s.read_by != "none" and s.text.strip()]
        hashes = {s.section_id: text_hash(s.text) for s in todo}
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            cached = store.summaries_by_hash(conn, self.uid, sorted(set(hashes.values())))
        total, done, missing = len(todo), 0, 0
        hits = [(s.section_id, cached[hashes[s.section_id]], hashes[s.section_id])
                for s in todo if hashes[s.section_id] in cached]
        hit_ids = {sid for sid, _, _ in hits}
        if hits:
            with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
                store.set_summaries(conn, self.uid, self.sid, hits)
            done = len(hits)
            self.report("summary", done, total, f"正在读 {done}/{total} 节")
        for s in todo:
            h = hashes[s.section_id]
            if s.section_id in hit_ids:
                continue
            if h in cached:            # same text as a section summarized earlier in this run
                row = (s.section_id, cached[h], h)
            else:
                diagnostics: list[str] = []
                got = summarize([s], llm=llm, cached=cached, on_done=lambda _i, _n: None,
                                diagnostics=diagnostics)
                row = (got[0][0], got[0][2], got[0][1]) if got else None
            if row is None:
                missing += 1
            else:
                with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
                    store.set_summaries(conn, self.uid, self.sid, [row])
                cached[h] = row[1]
            done += 1
            self.report("summary", done, total, f"正在读 {done}/{total} 节")
        return {"skipped": False, "missing": missing}

    def finish(self, *, failed: bool, error: str | None, end: dict[str, Any]) -> bool:
        end = end | {"mode": self.job.mode}   # a learning use reruns until a full run is done
        with _generation_connection(self.deps.pool, self.uid, self.sid, self.job.gen) as conn:
            if failed:
                store.set_status(conn, self.uid, self.sid, status="failed", error=error)
            store.set_progress(conn, self.uid, self.sid, end)
        return True

    def fail(self, detail: str, usable: SourceStatus | None, stage: Stage, *,
             code: str | None = None, params: dict[str, Any] | None = None) -> SourceStatus:
        end = SourceProgress(stage=stage, done=0, total=1, message=detail).model_dump()
        end |= {"end": "error", "detail": detail, "error_code": code, "error_params": params or {}}
        try:
            self.step("run.finish", lambda: self.finish(failed=usable is None, error=detail,
                                                        end=end))
        except _Deleted:
            return usable or "reading"
        except Exception:     # the follower is still told; the row keeps what it had
            log.exception("source %s/%s: the failure could not be recorded", self.uid, self.sid)
        with self.deps.pool.connection() as conn:
            user = conn.execute("select lang from app_user where user_id=%s", (self.uid,)).fetchone()
        language = user["lang"] if user else "en"
        self.emit("error", SourceFailed(detail=failure_detail(detail, code, params or {}, language=language), code=code))
        return usable or "failed"


def _ready(deps: ImportDeps, job: ImportJob) -> bool:
    """`deps.on_ready` inside its step; a failing hook is logged and the import ends `done`."""
    try:
        deps.on_ready(job.user_id, job.source_id)
    except Exception:
        log.exception("source %s/%s: the on_ready hook failed", job.user_id, job.source_id)
    return True


def _budget_stop(exc: BudgetExceededError) -> dict[str, Any]:
    return {"code": "budget_exceeded", "detail": exc.line(BUDGET_OUTCOME), "params": exc.params}


def failure_detail(detail: str, code: str | None, params: dict[str, Any], *, language: str | None = None) -> str:
    """Render only typed system facts; old text remains original without guessing its language."""
    from evowiki.i18n import current_language, tr
    if (language or current_language()) != "en":
        return detail
    known = {
        "provider_rejected": "The model service rejected the request. Check its credentials, permissions or balance.",
        "vision_failed": "Some image pages could not be read. The parts already read remain usable.",
        "source_import_failed": "The material could not be imported. Delete it and upload it again.",
        "source_processing_failed": "Processing the material failed. The parts already read remain usable.",
        "source_parse_failed": "This material could not be read. Check the file or URL and upload it again.",
        "source_not_found": "This material was not found.",
        "source_deleted": "This material was deleted.",
        "source_interrupted": "Reading this material was interrupted. Try again.",
    }
    if code != "budget_exceeded":
        return tr(known[code], lang="en") if code in known else detail
    message = {
        "day": "Today's spending reached the ${limit} limit. This material was not fully read. You can raise the limit in settings.",
        "month": "This month's spending reached the ${limit} limit. This material was not fully read. You can raise the limit in settings.",
        "page": "Spending on this page reached the ${limit} limit. This material was not fully read. You can raise the limit in settings.",
    }.get(params.get("period"))
    return tr(message, lang="en", **params) if message else detail


@traced_entry("source_import", subject=lambda a: a["job"].source_id,
              user=lambda a: a["job"].user_id)
def execute(job: ImportJob, emit: Callable[[str, BaseModel], None],
            deps: ImportDeps) -> SourceStatus:
    """Imports the source; returns its status. Emits `usable` once the source can be used and
    ends the item stream with `done` or `error`, except when the source was deleted (or a newer
    run took over) meanwhile: then it just stops."""
    r = _Run(job, emit, deps)
    usable: SourceStatus | None = None
    stage: Stage = "parse"
    code, params = None, {}
    try:
        parsed = layer = _doc_from(r.step("source.parse", r.parse))
        laid: dict[str, Any] = {}
        if parsed.kind == "pdf":       # whatever is configured: replay-stable
            laid = r.step("source.layout", lambda: r.layout(parsed))
            if "doc" in laid:
                parsed = _doc_from(laid["doc"])
        # module doc: the text layer as it is; `quick`: a chat attachment that stays so
        text_first = parsed.kind == "pdf" and "doc" not in laid
        quick = text_first and job.mode == "ask" and (
            not scanned(layer) or reads_on_demand(job, layer, deps.claude))
        # a slide's pictures are read as a turn asks about them, in any mode: the text is all a run has
        on_demand = quick or parsed.kind == "pptx"
        imprecise = (text_first, _imprecise(layer) if text_first else [],
                     _image_pages(layer) if on_demand or (parsed.kind == "pdf" and not scanned(layer)) else [])
        kept = (r.step("source.keep", lambda: r.keep(parsed, layer, imprecise=imprecise, quick=on_demand))
                if job.keep_sections else None)
        if kept is None:
            stage = "detect"
            pre = r.step("source.vision", lambda: r.vision(parsed, read=not on_demand))
            doc = _with_reads(parsed, pre["reads"])
            stage = "toc"
            # pages the chat turn looks at itself
            turn = sorted({*pre.get("turn", []), *(imprecise[2] if on_demand else [])})
            toc = r.step("source.toc", lambda: r.toc(doc, turn, quick=quick, imprecise=imprecise))
        else:                                      # the sections stay; every page is read later
            pre = {"reads": kept["reads"], "attempted": [], "stop": None}
            doc, turn, toc = _with_reads(parsed, pre["reads"]), kept["turn"], kept
        usable, stage = toc["status"], "embed"     # `stage`: the step a failure is in
        embedded = toc.get("embed", True)          # absent in step values recorded before it
        if embedded:                               # text first: usable without vectors
            r.step("source.chunk", lambda: r.chunk(missing=kept is not None))
        if not toc["rejected"] and job.mode == "full":   # 402 / budget at the toc: no more calls
            r.step("source.glossary", r.glossary)
        n = toc["sections"]
        r.emit("usable", SourceDone(status=usable, pages=toc["pages"], sections=n,
                                    unreadable_pages=toc["unreadable"], note=toc["note"],
                                    title=r.title()))
        if embedded:                               # vectors after usable (minutes on a CPU)
            r.step("source.embed", r.embed)
        notes = [toc["note"]] if toc["note"] else []
        if toc["rejected"]:            # 402 / budget at the toc: usable by pages, no summaries
            stage = "toc"
            if toc.get("budget"):      # absent in step values recorded before this key
                raise _TocBudget(toc["budget"])
            raise ProviderRejectedError("toc model rejected the request")

        stage = "ocr"
        stop: dict[str, Any] | None = pre.get("stop")
        applied = {int(x[0]): x[1] for x in pre["reads"]}
        attempted = set(pre.get("attempted", []))
        # a chat attachment's text stays what a running turn read: no hinted page is read after
        # usable, and a text-first one's unreadable pages wait for the turn that asks for them
        unread = [] if on_demand else [p.page for p in parsed.pages
                                   if p.read_by == "none" and p.page not in attempted]
        hinted = [] if job.mode == "ask" else [p for p in _hinted(parsed) if p not in applied]
        unreadable, summaries = toc["unreadable"], toc["summaries"]
        later = {"reads": [], "stop": None}
        if unread or hinted:
            later = r.step("source.ocr", lambda: r.ocr(doc, unread, hinted, claude_ok=stop is None))
            stop = stop or later["stop"]
        fresh = [*toc.get("parsed", []),
                 *(x for x in later["reads"] if applied.get(int(x[0])) != x[1])]
        doc = _with_reads(doc, fresh)
        if fresh:
            patched = r.step("source.patch", lambda: r.patch(
                doc, fresh, embedded=embedded, turn=turn))
            usable, unreadable = patched["status"], patched["unreadable"]
            summaries = patched["summaries"]
        if stop:
            raise _VisionStop(stop)

        if summaries:
            stage = "summary"
            res = r.step("source.summary", r.summary)
            if res["skipped"]:
                notes.append(NOTE_NO_LLM)
            elif res["missing"]:      # its readers use the section's first paragraph
                log.info("source %s/%s: %d sections without a summary", job.user_id, job.source_id,
                         res["missing"])
        note = "；".join(notes) or None
        end = SourceProgress(stage=stage, done=n, total=n,
                             message=f"读完了，共 {n} 节").model_dump()
        end |= {"end": "done", "note": note}
        if toc.get("diagnostics"):     # absent in step values recorded before this key
            end["diagnostics"] = toc["diagnostics"]
        r.step("run.finish", lambda: r.finish(failed=False, error=None, end=end))
        if deps.on_ready is not None:
            durable.step("source.match_start", lambda: _ready(deps, job))
        r.emit("done", SourceDone(status=usable, pages=toc["pages"], sections=n,
                                  unreadable_pages=unreadable, note=note, title=r.title()))
        return usable
    except _Deleted:
        log.info("source %s/%s was deleted or re-run: import (gen %d) stopped", job.user_id,
                 job.source_id, job.gen)
        return usable or "reading"
    except ParseError as exc:
        detail, code = str(exc), "source_parse_failed"
    except ProviderRejectedError:
        detail, code = PROVIDER_REJECTED_DETAIL, "provider_rejected"
    except _TocBudget as exc:          # the user's spending cap: keep what was written
        detail, code, params = exc.args[0]["detail"], exc.args[0]["code"], exc.args[0]["params"]
    except BudgetExceededError as exc:
        stop = _budget_stop(exc)
        detail, code, params = stop["detail"], stop["code"], stop["params"]
    except _VisionStop as exc:
        detail, code, params = exc.stop["detail"], exc.stop["code"], exc.stop["params"]
    except Exception:          # fail closed: the row and the follower are told
        log.exception("source import %s/%s crashed", job.user_id, job.source_id)
        detail = MSG_CRASHED if usable is None else MSG_LATE_CRASH
        code = "source_import_failed" if usable is None else "source_processing_failed"
    return r.fail(detail, usable, stage, code=code, params=params)
