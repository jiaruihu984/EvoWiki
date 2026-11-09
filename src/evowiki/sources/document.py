"""EvoWiki's own document model: what a layout parser (MinerU, Docling, python-pptx) read from a
source, as typed blocks per page plus a heading tree, and its bridge to `ParsedDoc` (the shape the
section builder takes). Backends fill `SourceDocument`; nothing downstream sees a backend type."""

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal

from evowiki.sources.models import SourceKind
from evowiki.sources.parsed import LayoutHead, OutlineEntry, PageText, ParsedDoc

BlockKind = Literal["heading", "paragraph", "list_item", "formula", "table", "figure", "caption",
                    "code", "footnote", "notes", "header", "footer"]
HeadingSource = Literal["bookmark", "layout", "numbering", "slide_title", "section_label", "llm"]

BBox = tuple[float, float, float, float]

_HIDDEN = frozenset({"header", "footer"})   # running lines: not part of the page text


@dataclass(frozen=True)
class Block:
    block_id: str                   # unique in the document, e.g. "p3-b7"
    page: int                       # 1-based
    kind: BlockKind
    text: str
    bbox: BBox | None = None        # page-relative, 0..1
    latex: str | None = None        # formula
    html: str | None = None         # table
    level: int | None = None        # heading level as the backend gave it


@dataclass(frozen=True)
class Heading:
    heading_id: str
    title: str
    level: int
    page: int
    parent_id: str | None
    bbox: BBox | None
    source: HeadingSource


@dataclass(frozen=True)
class DocPage:
    page: int                       # 1-based
    width: float | None = None
    height: float | None = None


@dataclass(frozen=True)
class Locator:
    """Where a piece of a source sits: a page, optionally a block and a character range in it."""
    source_id: str
    page: int
    bbox: BBox | None
    block_id: str | None
    char_start: int | None = None
    char_end: int | None = None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _block_text(b: Block) -> str:
    if b.kind == "heading":
        return _one_line(b.text)
    if b.kind == "formula" and b.latex:
        return f"$${b.latex}$$"
    if b.kind == "notes":
        return f"备注：{b.text.strip()}" if b.text.strip() else ""
    return b.text.strip()


def _bbox(v: Any) -> BBox | None:
    return None if v is None else (v[0], v[1], v[2], v[3])


@dataclass(frozen=True)
class SourceDocument:
    kind: SourceKind
    backend: str                    # "mineru" | "docling" | "pptx" ...
    version: str
    pages: tuple[DocPage, ...]
    blocks: tuple[Block, ...]
    headings: tuple[Heading, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "SourceDocument":
        return cls(
            kind=d["kind"], backend=d["backend"], version=d["version"],
            pages=tuple(DocPage(**p) for p in d["pages"]),
            blocks=tuple(Block(**{**b, "bbox": _bbox(b.get("bbox"))}) for b in d["blocks"]),
            headings=tuple(Heading(**{**h, "bbox": _bbox(h.get("bbox"))})
                           for h in d.get("headings", ())))

    def page_texts(self) -> dict[int, tuple[str, list[LayoutHead]]]:
        """Per page: its text (one line per paragraph; header/footer blocks left out) and the
        heading lines found while joining."""
        out: dict[int, tuple[list[str], list[LayoutHead]]] = {p.page: ([], []) for p in self.pages}
        for b in self.blocks:
            if b.kind in _HIDDEN or b.page not in out:
                continue
            t = _block_text(b)
            if not t:
                continue
            lines, heads = out[b.page]
            if b.kind == "heading":
                heads.append(LayoutHead(b.page, len(lines), t, b.level))
            lines.extend(t.splitlines())
        return {p: ("\n".join(ls), hs) for p, (ls, hs) in out.items()}

    def to_parsed(self, title: str) -> ParsedDoc:
        texts = self.page_texts()
        pages = tuple(
            PageText(p.page, texts[p.page][0], "text" if texts[p.page][0] else "none",
                     None if texts[p.page][0] else "no_text")
            for p in self.pages)
        outline = tuple(OutlineEntry(h.title, h.level, h.page) for h in self.headings)
        heads = tuple(h for p in self.pages for h in texts[p.page][1])
        return ParsedDoc(self.kind, title, pages, outline, False, heads)


def merge_text_layer(layout: ParsedDoc, text_layer: ParsedDoc) -> ParsedDoc:
    """Page by page the layout page when it has text, else the text-layer page. A page taken
    from the layout carries no text-layer facts (`fonts_without_tounicode` False, `image_area`
    0): they describe the text layer, and as judge hints they would send a page the layout parser
    has already read from its image to OCR again. The outline is the text layer's (PDF bookmarks
    only); layout headings reach the table of contents through `layout_heads`, kept for the pages
    taken from the layout."""
    by_page = {p.page: p for p in layout.pages}
    pages: list[PageText] = []
    taken: set[int] = set()
    for tp in text_layer.pages:
        lp = by_page.get(tp.page)
        if lp is not None and lp.text:
            taken.add(tp.page)
            pages.append(dataclasses.replace(lp, fonts_without_tounicode=False, image_area=0.0))
        else:
            pages.append(tp)
    heads = tuple(h for h in layout.layout_heads if h.page in taken)
    return ParsedDoc(text_layer.kind, text_layer.title, tuple(pages), text_layer.outline,
                     text_layer.headings_inline, heads)
