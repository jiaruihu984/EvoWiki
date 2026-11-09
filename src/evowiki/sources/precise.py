"""One PDF page read from its image on demand: what read_source gets for a chat attachment's page
with a picture (`SourceRow.image_pages`), and for `precise=true`.

The read comes from a model that sees (`ClaudePages`), never from the local layout reader, which
misreads these pages (t²/2 as t−1/2, live 2026-11-04). It is stored as a `PageRead` for later turns
and for a later full import (`importer.read_pages` reuses it), and goes nowhere near the sections: a
running turn's text does not change, and a stored read's `figure` stays False, so it never reroutes
a later turn. Nothing raises for a page that cannot be read: it is None and the caller says so to
the model."""

import contextvars
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from psycopg_pool import ConnectionPool

from evowiki.sources import store
from evowiki.sources.importer import _Deleted, _generation_connection, page_images
from evowiki.sources.models import PageRead
from evowiki.sources.parse_pptx import PICTURE_UNREAD, parse_pptx, slide_pictures
from evowiki.sources.unreadable import PICTURE_AREA
from evowiki.sources.vision import VisionReader, strip_markup

log = logging.getLogger("evowiki.sources.precise")


def read_precise(pool: ConnectionPool, reader: VisionReader | None, user_id: str, source_id: str,
                 pages: Sequence[int], *, now: Callable[[], datetime],
                 say: Callable[[Sequence[int]], None] | None = None) -> dict[int, str | None]:
    """page -> its text as read from its image; None when it could not be read. `say(pages)`
    before the reader looks at the pages that have to be read now, all at once (about 6 seconds a
    page, live 2026-11-04: one after another a question that needs five pages waits half a minute)."""
    empty: dict[int, str | None] = {p: None for p in pages}
    try:
        with _generation_connection(pool, user_id, source_id, None) as conn:
            row = store.get_source(conn, user_id, source_id)
            assert row is not None
            gen = row.run_gen
            reads = store.page_reads(conn, user_id, source_id)
    except _Deleted:
        return empty
    seen = {p: r for p, r in reads.items() if r.by == "claude"}     # a local read does not count
    out: dict[int, str | None] = {p: seen[p].text if p in seen else None for p in pages}
    # a read that came out empty stays empty: only pages never read go to the reader
    todo = [p for p in pages if p not in seen]
    if not todo:
        return out
    try:
        with _generation_connection(pool, user_id, source_id, gen) as conn:
            got = store.get_file(conn, user_id, source_id)
    except _Deleted:
        return empty
    if got is None:
        return out
    slide: dict[int, str] = {}
    if row.kind == "pptx":          # its text is good and comes with what the pictures say
        slide = {x.page: x.text for x in parse_pptx(got[0], title_hint="")[0].pages}
        out |= {p: slide.get(p) or None for p in todo}
    if reader is None or not reader.available():
        return out
    if say is not None:
        say(todo)

    def read_one(p: int, img: bytes) -> None:
        try:
            res = reader.read([img])
        except Exception:      # 402, the spending cap, a timeout: not stored, the next call tries
            log.warning("source %s/%s page %d: not read", user_id, source_id, p, exc_info=True)
            return
        if len(res) != 1 or res[0].text is None:
            log.warning("source %s/%s page %d: not read (%s)", user_id, source_id, p,
                        res[0].reason if res else "no reply")
            return
        text = strip_markup(res[0].text).strip() or None
        if p in slide and text:
            words = slide[p].replace(PICTURE_UNREAD, "").strip()
            text = f"{words}\n\n图片里的内容：\n{text}".strip()
        try:
            with _generation_connection(pool, user_id, source_id, gen) as conn:
                store.put_page_reads(conn, user_id, source_id, [PageRead(p, text, "claude", False)],
                                     now())
        except _Deleted:
            log.info("source %s/%s changed during precise page %d read; discarded", user_id, source_id, p)
            return
        out[p] = text

    images = (slide_pictures(got[0], todo, PICTURE_AREA) if row.kind == "pptx"
              else page_images("pdf", got[0], todo))
    with ThreadPoolExecutor(max_workers=len(todo)) as ex:
        for f in [ex.submit(contextvars.copy_context().run, read_one, p, img)
                  for p, img in zip(todo, images, strict=True) if img is not None]:
            f.result()
    try:
        with _generation_connection(pool, user_id, source_id, gen):
            return out
    except _Deleted:
        return empty
