"""pptx reader (python-pptx): per slide the text shapes (groups walked), tables, pictures and
charts with their boxes, the slide title, the small "NN · label" section labels decks put above the
title, and the speaker notes. MarkItDown took section labels and corner tags for titles; this
reads the shapes. Output is the document model (`SourceDocument`) and its `ParsedDoc` view."""

import dataclasses
import re
from importlib.metadata import version as _dist_version
from io import BytesIO
from typing import Any

from PIL import Image, UnidentifiedImageError

from evowiki.sources.document import BBox, Block, BlockKind, DocPage, Heading, SourceDocument
from evowiki.sources.parse_other import MSG_BROKEN
from evowiki.sources.parsed import ParsedDoc, ParseError

SECTION = re.compile(r"^\s*(\d{1,2})\s*[·.、]\s*(\S.{0,30})$")
_FILE_ALT = re.compile(r"\.(png|jpe?g|gif|bmp|tiff?|emf|wmf|svg|webp)$", re.IGNORECASE)
_TITLE_MAX = 120
STACK_GAP = 24
# A deck's formula pasted as a picture is not in its text: its text says there is something it does not
# hold, so "the slide does not say" is never the answer. A turn reads the picture (`slide_pictures`).
PICTURE_UNREAD = "（这里有一张图片，内容没有读出来）"
_TOP_ZONE = 0.4


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _walk(shapes: Any):
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    for sh in shapes:
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _walk(sh.shapes)
        else:
            yield sh


def _is_title_ph(sh: Any) -> bool:
    from pptx.enum.shapes import PP_PLACEHOLDER
    return bool(sh.is_placeholder
                and sh.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE))


def _font_size(sh: Any) -> float:
    sizes = [r.font.size.pt for p in sh.text_frame.paragraphs for r in p.runs if r.font.size]
    if not sizes and sh.is_placeholder:
        return 40.0 if _is_title_ph(sh) else 18.0
    return max(sizes, default=12.0)


def _clamp(v: float) -> float:
    return round(min(1.0, max(0.0, v)), 4)


def _box(sh: Any, w: float, h: float) -> BBox | None:
    if sh.left is None or sh.top is None or not w or not h:
        return None
    return (_clamp(sh.left / w), _clamp(sh.top / h),
            _clamp((sh.left + (sh.width or 0)) / w), _clamp((sh.top + (sh.height or 0)) / h))


class _Item:
    __slots__ = ("bbox", "kind", "size", "text", "title_ph")

    def __init__(self, kind: BlockKind, text: str, bbox: BBox | None, size: float = 0.0,
                 title_ph: bool = False):
        self.kind, self.text, self.bbox, self.size, self.title_ph = kind, text, bbox, size, title_ph


def _items(slide: Any, w: float, h: float) -> list[_Item]:
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    out: list[_Item] = []
    for sh in _walk(slide.shapes):
        box = _box(sh, w, h)
        if sh.has_text_frame and sh.text_frame.text.strip():
            out.append(_Item("paragraph", sh.text_frame.text.strip(), box, _font_size(sh),
                             _is_title_ph(sh)))
        elif getattr(sh, "has_table", False) and sh.has_table:
            rows = [" | ".join(c.text.strip() for c in r.cells) for r in sh.table.rows]
            out.append(_Item("table", "\n".join(rows), box))
        elif sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
            alt = sh._element.xpath("./p:nvPicPr/p:cNvPr/@descr")
            text = alt[0].strip() if alt else ""
            out.append(_Item("figure", "" if _FILE_ALT.search(text) else text, box))
        elif getattr(sh, "has_chart", False) and sh.has_chart:
            ch = sh.chart
            title = ch.chart_title.text_frame.text.strip() if ch.has_title else ""
            out.append(_Item("figure", title, box))
    out.sort(key=lambda i: (i.bbox[1], i.bbox[0]) if i.bbox else (9.0, 9.0))
    if any(i.text for i in out):       # a slide with only a picture is an unreadable page already
        out = [_Item(i.kind, PICTURE_UNREAD, i.bbox) if i.kind == "figure" and not i.text else i for i in out]
    return out


def _candidate(it: _Item) -> bool:
    """Could be the slide's title: a title placeholder, or a short text in the top of the slide."""
    if it.kind != "paragraph":
        return False
    return it.title_ph or bool(it.bbox and it.bbox[1] < _TOP_ZONE and len(it.text) <= _TITLE_MAX)


def _label(it: _Item, items: list[_Item]) -> str | None:
    """The section label a short numbered text is, only when the slide has a larger title
    candidate besides it; alone, "1. Intro" is the slide's title."""
    if it.kind != "paragraph" or it.title_ph:
        return None
    m = SECTION.match(" ".join(it.text.split("\n")))
    if not m:
        return None
    bigger = any(o is not it and _candidate(o) and o.size > it.size
                 and not SECTION.match(" ".join(o.text.split("\n"))) for o in items)
    return m.group(2).strip() if bigger else None


def _title(items: list[_Item]) -> _Item | None:
    for it in items:
        if it.kind == "paragraph" and it.title_ph:
            return it
    top = [it for it in items if _candidate(it) and _label(it, items) is None]
    return max(top, key=lambda it: (it.size, -it.bbox[1]), default=None)   # type: ignore[index]


def read_pptx(data: bytes) -> SourceDocument:
    from pptx import Presentation
    try:
        prs = Presentation(BytesIO(data))
        w, h = float(prs.slide_width), float(prs.slide_height)
        slides = list(prs.slides)
    except Exception as e:                      # corrupt zip, bad XML ...
        raise ParseError(MSG_BROKEN) from e
    if not slides:
        raise ParseError(MSG_BROKEN)
    pages: list[DocPage] = []
    blocks: list[Block] = []
    raw_heads: list[tuple[str, int, int, BBox | None, str]] = []   # title, level, page, bbox, src
    section: str | None = None
    for n, slide in enumerate(slides, 1):
        items = _items(slide, w, h)
        pages.append(DocPage(n, w, h))
        title = _title(items)
        sec = next((it for it in items if _label(it, items) is not None and it.bbox), None)
        if sec is not None:
            label = _label(sec, items) or ""
            if label != section:
                section = label
                raw_heads.append((label, 0, n, sec.bbox, "section_label"))
        if title is not None:
            raw_heads.append((_one_line(title.text), 1, n, title.bbox, "slide_title"))
        for k, it in enumerate(items, 1):
            kind: BlockKind = "heading" if it is title else it.kind
            blocks.append(Block(f"p{n}-b{k}", n, kind, it.text, it.bbox,
                                level=1 if it is title else None))
        notes = slide.notes_slide.notes_text_frame.text.strip() if slide.has_notes_slide else ""
        if notes:
            blocks.append(Block(f"p{n}-b{len(items) + 1}", n, "notes", notes))
    flat = not any(level == 0 for _, level, *_ in raw_heads)
    headings = tuple(
        Heading(f"h{i}", t, 0 if flat else lv, pg, None, bb, src)          # type: ignore[arg-type]
        for i, (t, lv, pg, bb, src) in enumerate(raw_heads, 1))
    return SourceDocument("pptx", "pptx", _dist_version("python-pptx"), tuple(pages),
                          tuple(blocks), headings)


def parse_pptx(data: bytes, *, title_hint: str) -> tuple[ParsedDoc, SourceDocument]:
    doc = read_pptx(data)
    first = next((h.title for h in doc.headings if h.source == "slide_title"), None)
    parsed = doc.to_parsed(first or title_hint)
    shares = _picture_shares(data)
    return dataclasses.replace(parsed, pages=tuple(
        dataclasses.replace(p, image_area=shares.get(p.page, 0.0)) for p in parsed.pages)), doc


def _pictures(slide: Any) -> list[Any]:
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    return [sh for sh in _walk(slide.shapes) if sh.shape_type == MSO_SHAPE_TYPE.PICTURE]


def _picture_shares(data: bytes) -> dict[int, float]:
    """Slide -> the share of it covered by its pictures (`PageText.image_area`), capped at 1."""
    from pptx import Presentation
    prs = Presentation(BytesIO(data))
    area = float(prs.slide_width) * float(prs.slide_height)
    return {n: min(1.0, sum(float(sh.width) * float(sh.height) for sh in _pictures(s)) / area)
            for n, s in enumerate(prs.slides, 1)}


def _stacked(blobs: list[bytes]) -> bytes | None:
    """The pictures one above the other on white, at their own size: a pasted formula is small on its
    slide and has to stay readable. None when none of them opens (an EMF, say)."""
    images = []
    for blob in blobs:
        try:
            img = Image.open(BytesIO(blob))
            img.load()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            continue
        images.append(img.convert("RGBA"))
    if not images:
        return None
    canvas = Image.new("RGB", (max(i.width for i in images),
                               sum(i.height for i in images) + STACK_GAP * (len(images) - 1)), "white")
    y = 0
    for img in images:
        canvas.paste(img, (0, y), img)
        y += img.height + STACK_GAP
    out = BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


def slide_pictures(data: bytes, pages: list[int], min_share: float) -> list[bytes | None]:
    """One image per slide of `pages` for a seeing model: the pictures embedded in the file (a slide is
    not rendered, and they are what its text does not hold), those of at least `min_share` of the
    slide. None: no picture of it could be opened."""
    from pptx import Presentation
    prs = Presentation(BytesIO(data))
    area = float(prs.slide_width) * float(prs.slide_height)
    slides = list(prs.slides)
    return [_stacked([sh.image.blob for sh in _pictures(slides[p - 1])
                      if float(sh.width) * float(sh.height) / area >= min_share]) for p in pages]
