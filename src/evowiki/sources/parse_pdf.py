"""PDF text + bookmarks via pypdfium2 (Apache-2.0 / BSD; no AGPL PyMuPDF)."""

from __future__ import annotations

import functools
import io
import logging
import re
import threading

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from evowiki.sources.parsed import OutlineEntry, PageText, ParsedDoc, ParseError
from evowiki.sources.titles import clean_title, generic_title, looks_like_title

log = logging.getLogger(__name__)

MSG_PASSWORD = "这个文件打不开（加了密码？）"
MSG_BROKEN = "这个文件打不开（文件可能损坏）"
MSG_EMPTY = "这个 PDF 里没有任何页面"

# PDFium is inherently not thread-safe (pypdfium2 docs): one process-wide lock is held for the whole
# of every public entry point below (open, iterate, render, close), so no two threads (HTTP
# threadpool, DBOS import workflows, assistant turns) are ever inside pdfium together. Every
# PdfDocument is opened and closed within one locked call.
DRAWN_SPAN = 0.15          # of the page, either way: wider than this is a rule or a border
_PDFIUM_LOCK = threading.RLock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _PDFIUM_LOCK:
            return fn(*args, **kwargs)
    return wrapper


_PASSWORD_CODES = frozenset({pdfium_c.FPDF_ERR_PASSWORD, pdfium_c.FPDF_ERR_SECURITY})


def _open(data: bytes) -> pdfium.PdfDocument:
    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) in _PASSWORD_CODES:
            raise ParseError(MSG_PASSWORD) from exc
        raise ParseError(MSG_BROKEN) from exc
    if len(doc) == 0:
        doc.close()
        raise ParseError(MSG_EMPTY)
    return doc


# a soft hyphen (U+00AD; pdfium gives U+0002 for the PDF's own) marks where a word may break:
# at a line end it joins the word's halves, elsewhere it is dropped. A real hyphen stays.
_SOFT_BREAK = re.compile("[\x02\u00ad][ \t]*\n[ \t]*")
_SOFT = re.compile("[\x02\u00ad]")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return _SOFT.sub("", _SOFT_BREAK.sub("", text))


def _title(doc: pdfium.PdfDocument, title_hint: str) -> str:
    """The uploaded file's name (`title_hint`); the metadata Title only when that name is generic
    and the metadata is not (sources/titles, final review #5). Cleaned either way."""
    if not generic_title(title_hint):
        return clean_title(title_hint)
    try:
        meta = clean_title(doc.get_metadata_value("Title") or "")
    except pdfium.PdfiumError:
        meta = ""
    return meta if meta and not generic_title(meta) else clean_title(title_hint)


@_locked
def pdf_title(data: bytes) -> str | None:
    """The PDF's own title for an upload the user did not name (follow-up B, 2026-10-22): the
    metadata Title, else the first top-level bookmark when it looks like a title, else None (the
    caller falls back to the file name). Cleaned; a file that does not open → None (the import
    reports it)."""
    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        return None
    try:
        try:
            meta = clean_title(doc.get_metadata_value("Title") or "")
        except pdfium.PdfiumError:
            meta = ""
        if meta and not generic_title(meta):
            return meta
        try:
            first = next((bm for bm in doc.get_toc() if bm.level == 0), None)
        except pdfium.PdfiumError:
            return None
        if first is None:
            return None
        head = first.get_title().strip()
        return clean_title(head) if looks_like_title(head) else None
    finally:
        doc.close()


def _outline(doc: pdfium.PdfDocument) -> tuple[OutlineEntry, ...]:
    n = len(doc)
    entries: list[OutlineEntry] = []
    for bm in doc.get_toc():
        title = bm.get_title().strip()
        if not title:
            continue
        dest = bm.get_dest()
        index = dest.get_index() if dest is not None else None
        page = index + 1 if index is not None and 0 <= index < n else None
        entries.append(OutlineEntry(title=title, depth=bm.level, page=page))
    return tuple(entries)


def _has_unmapped_chars(textpage: pdfium.PdfTextPage) -> bool:
    """True when pdfium cannot map some character on the page to Unicode (a font with no
    ToUnicode and no usable encoding). pdfium then returns the raw codes, which can look
    like clean letters, so the text alone does not show it."""
    # 1 = unmapped, 0 = mapped, -1 = error (not evidence of a missing map)
    return any(pdfium_c.FPDFText_HasUnicodeMapError(textpage, i) == 1
               for i in range(textpage.count_chars()))


def _image_area(page: pdfium.PdfPage) -> float:
    """Share of the page covered by image objects (forms included), bounding boxes clipped
    to the page and summed, capped at 1. Overlapping images count twice and a rotated image
    counts its axis-aligned box; it is a hint.

    An image inside a Form XObject reports bounds in the form's space, so its box is mapped
    through each enclosing form's matrix (innermost first) up to page space."""
    left, bottom, right, top = page.get_bbox()
    page_area = (right - left) * (top - bottom)
    if page_area <= 0:
        return 0.0
    covered = 0.0
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=3):
        try:
            x0, y0, x1, y1 = obj.get_bounds()
            form = obj.container
            while form is not None:
                x0, y0, x1, y1 = form.get_matrix().on_rect(x0, y0, x1, y1)
                form = form.container
        except pdfium.PdfiumError:         # a hint only: one odd image must not fail the parse
            log.warning("pdf image object without bounds; left out of the image area")
            continue
        w = min(x1, right) - max(x0, left)
        h = min(y1, top) - max(y0, bottom)
        if w > 0 and h > 0:
            covered += w * h
    return min(1.0, covered / page_area)


def _drawn_marks(page: pdfium.PdfPage) -> int:
    """Vector shapes small in both directions (under DRAWN_SPAN of the page). A formula set by
    LaTeX or PowerPoint leaves its letters in the text layer in reading-order pieces and draws its
    fraction bars and radicals: the text alone does not show what is missing. A table's rules and a
    page's border span the page and do not count."""
    left, bottom, right, top = page.get_bbox()
    width, height = (right - left) * DRAWN_SPAN, (top - bottom) * DRAWN_SPAN
    marks = 0
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_PATH], max_depth=3):
        try:
            x0, y0, x1, y1 = obj.get_bounds()
        except pdfium.PdfiumError:
            continue
        if x1 - x0 < width and y1 - y0 < height:
            marks += 1
    return marks


@_locked
def check_pdf(data: bytes) -> None:
    """ParseError when the PDF cannot be opened (a password, a broken file, no pages)."""
    _open(data).close()


@_locked
def parse_pdf(data: bytes, *, title_hint: str) -> ParsedDoc:
    """One PageText per page (read_by="text"; run unreadable.judge_all afterwards)."""
    doc = _open(data)
    try:
        pages: list[PageText] = []
        for i in range(len(doc)):
            page = doc[i]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded()
                    unmapped = _has_unmapped_chars(textpage)
                finally:
                    textpage.close()
                area = _image_area(page)
                drawn = _drawn_marks(page)
            finally:
                page.close()
            pages.append(PageText(page=i + 1, text=_clean(text), read_by="text",
                                  fonts_without_tounicode=unmapped, image_area=area,
                                  drawn_marks=drawn))
        return ParsedDoc(
            kind="pdf",
            title=_title(doc, title_hint),
            pages=tuple(pages),
            outline=_outline(doc),
            headings_inline=False,
        )
    except pdfium.PdfiumError as exc:
        raise ParseError(MSG_BROKEN) from exc
    finally:
        doc.close()


@_locked
def render_pages(data: bytes, pages: list[int], dpi: int = 150) -> list[bytes]:
    """PNG bytes for the given 1-based pages, in the order asked (for a vision reader)."""
    doc = _open(data)
    try:
        n = len(doc)
        bad = [p for p in pages if not 1 <= p <= n]
        if bad:
            raise ValueError(f"page out of range 1..{n}: {bad}")
        out: list[bytes] = []
        for p in pages:
            page = doc[p - 1]
            try:
                bitmap = page.render(scale=dpi / 72)
                buf = io.BytesIO()
                bitmap.to_pil().save(buf, format="PNG")
                bitmap.close()
            finally:
                page.close()
            out.append(buf.getvalue())
        return out
    except pdfium.PdfiumError as exc:
        raise ParseError(MSG_BROKEN) from exc
    finally:
        doc.close()
