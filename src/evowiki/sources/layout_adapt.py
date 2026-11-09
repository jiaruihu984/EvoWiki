"""Adapters from a layout parser's raw output (MinerU content list, DoclingDocument dict) to
EvoWiki's `SourceDocument`. Pure functions over recorded JSON; neither backend is imported."""

import logging
from collections.abc import Sequence
from html.parser import HTMLParser
from typing import Any

from evowiki.sources.document import BBox, Block, BlockKind, DocPage, Heading, SourceDocument

log = logging.getLogger(__name__)

_MINERU_SIMPLE: dict[str, BlockKind] = {
    "header": "header", "aside_text": "header", "footer": "footer", "page_number": "footer",
    "page_footnote": "footnote",
}
_DOCLING_KIND: dict[str, BlockKind] = {
    "text": "paragraph", "paragraph": "paragraph", "list_item": "list_item",
    "caption": "caption", "footnote": "footnote", "page_header": "header",
    "page_footer": "footer", "code": "code",
}
_warned: set[str] = set()


def _warn_once(backend: str, kind: str) -> None:
    key = f"{backend}:{kind}"
    if key not in _warned:
        _warned.add(key)
        log.info("%s: unknown item type %r, kept as a paragraph", backend, kind)


def _unit(v: float) -> float:
    return min(1.0, max(0.0, v))


class _Table(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            self.rows[-1].append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def table_text(html: str) -> str:
    """A table's rows as `cell | cell` lines."""
    p = _Table()
    p.feed(html)
    p.close()
    return "\n".join(" | ".join(r) for r in p.rows if r)


class _Builder:
    """Collects blocks with `p{page}-b{n}` ids, n counting per page, and the heading list."""

    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.headings: list[Heading] = []
        self._n: dict[int, int] = {}

    def add(self, page: int, kind: BlockKind, text: str, *, bbox: BBox | None = None,
            latex: str | None = None, html: str | None = None, level: int | None = None) -> None:
        n = self._n.get(page, 0)
        self._n[page] = n + 1
        block_id = f"p{page}-b{n}"
        self.blocks.append(Block(block_id, page, kind, text, bbox, latex, html, level))
        if kind == "heading":
            self.headings.append(Heading(block_id, " ".join(text.split()), (level or 1) - 1,
                                         page, None, bbox, "layout"))


def _texts(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [s for s in (v or ()) if isinstance(s, str) and s.strip()]


def _mineru_box(box: Any) -> BBox | None:
    if not box:
        return None
    return (_unit(box[0] / 1000), _unit(box[1] / 1000), _unit(box[2] / 1000), _unit(box[3] / 1000))


def from_mineru(raw: list[dict[str, Any]], *, first_page: int, version: str) -> SourceDocument:
    """`raw` is MinerU's content list (v1). `page_idx` is the 0-based index in the whole PDF, not
    in the parsed range (checked on a recording of pages 4-5), so `first_page` only sets where the
    page list starts: pages `first_page..` the last page with an item."""
    b = _Builder()
    last = first_page
    for item in raw:
        page = int(item.get("page_idx", 0)) + 1
        last = max(last, page)
        kind = item.get("type", "")
        bbox = _mineru_box(item.get("bbox"))
        text = item.get("text") or ""
        if kind == "text":
            lvl = item.get("text_level") or 0
            if lvl >= 1:
                b.add(page, "heading", text, bbox=bbox, level=lvl)
            else:
                b.add(page, "paragraph", text, bbox=bbox)
        elif kind == "list":
            b.add(page, "list_item", "\n".join(_texts(item.get("list_items"))), bbox=bbox)
        elif kind == "equation":
            latex = text.strip().removeprefix("$$").removesuffix("$$").strip()
            b.add(page, "formula", text, bbox=bbox, latex=latex)
        elif kind == "table":
            body = item.get("table_body") or ""
            b.add(page, "table", table_text(body), bbox=bbox, html=body)
            for c in _texts(item.get("table_caption")):
                b.add(page, "caption", c)
        elif kind in ("image", "chart"):
            caps = _texts(item.get(f"{kind}_caption"))
            b.add(page, "figure", " ".join(caps), bbox=bbox)
            for c in caps:
                b.add(page, "caption", c)
        elif kind == "code":
            b.add(page, "code", item.get("code_body") or "", bbox=bbox)
        elif kind in _MINERU_SIMPLE:
            b.add(page, _MINERU_SIMPLE[kind], text, bbox=bbox)
        else:
            _warn_once("mineru", kind)
            b.add(page, "paragraph", text, bbox=bbox)
    pages = tuple(DocPage(p) for p in range(first_page, last + 1))
    return SourceDocument("pdf", "mineru", version, pages, tuple(b.blocks), tuple(b.headings))


def _ref(raw: dict[str, Any], ref: str) -> dict[str, Any]:
    _, coll, idx = ref.split("/")
    return raw[coll][int(idx)]


def _docling_box(item: dict[str, Any],
                 sizes: dict[int, tuple[float, float]]) -> tuple[int, BBox | None]:
    prov = item.get("prov") or []
    if not prov:
        return 0, None
    page = prov[0]["page_no"]
    bb = prov[0].get("bbox")
    size = sizes.get(page)
    if not bb or not size or not size[0] or not size[1]:
        return page, None
    w, h = size
    top, bottom = bb["t"], bb["b"]
    if bb.get("coord_origin") == "BOTTOMLEFT":
        top, bottom = h - top, h - bottom
    return page, (_unit(bb["l"] / w), _unit(top / h), _unit(bb["r"] / w), _unit(bottom / h))


def _docling_table_text(item: dict[str, Any]) -> str:
    rows: dict[int, dict[int, str]] = {}
    for c in (item.get("data") or {}).get("table_cells", ()):
        rows.setdefault(c["start_row_offset_idx"], {})[c["start_col_offset_idx"]] = \
            " ".join((c.get("text") or "").split())
    return "\n".join(" | ".join(r[k] for k in sorted(r)) for _, r in sorted(rows.items()))


def from_docling(raw: dict[str, Any], *, version: str) -> SourceDocument:
    """`raw` is `DoclingDocument.export_to_dict()`; blocks follow the reading order of `body`."""
    sizes = {int(k): (v["size"]["width"], v["size"]["height"]) for k, v in raw["pages"].items()}
    b = _Builder()
    seen: set[str] = set()
    cur = [min(sizes, default=1)]       # page of the last item with a position

    def box(item: dict[str, Any]) -> tuple[int, BBox | None]:
        page, bbox = _docling_box(item, sizes)
        cur[0] = page or cur[0]
        return cur[0], bbox

    def captions(item: dict[str, Any], page: int) -> None:
        for c in item.get("captions", ()):
            t = _ref(raw, c["$ref"])
            seen.add(c["$ref"])
            b.add(box(t)[0] or page, "caption", t.get("text") or t.get("orig") or "")

    def walk(children: Sequence[dict[str, Any]]) -> None:
        for ch in children:
            ref = ch["$ref"]
            if ref in seen:
                continue
            seen.add(ref)
            item = _ref(raw, ref)
            if ref.startswith("#/groups/"):
                walk(item.get("children", ()))
                continue
            label = item.get("label", "")
            page, bbox = box(item)
            text = item.get("text") or ""
            if ref.startswith("#/tables/"):
                b.add(page, "table", _docling_table_text(item), bbox=bbox)
                captions(item, page)
                continue
            if ref.startswith("#/pictures/"):
                b.add(page, "figure", "", bbox=bbox)      # its inner text is figure noise
                captions(item, page)
                continue
            if label in ("section_header", "title"):
                b.add(page, "heading", text, bbox=bbox,
                      level=0 if label == "title" else item.get("level", 1))
            elif label == "formula":
                latex = text if "\\" in text else None
                b.add(page, "formula", text or item.get("orig") or "", bbox=bbox, latex=latex)
            elif label in _DOCLING_KIND:
                b.add(page, _DOCLING_KIND[label], text, bbox=bbox)
            else:
                _warn_once("docling", label)
                b.add(page, "paragraph", text, bbox=bbox)
            walk(item.get("children", ()))

    walk(raw["body"].get("children", ()))
    pages = tuple(DocPage(p, *sizes[p]) for p in sorted(sizes))
    return SourceDocument("pdf", "docling", version, pages, tuple(b.blocks), tuple(b.headings))


def concat(docs: Sequence[SourceDocument]) -> SourceDocument:
    """Batches of one PDF, in order: pages, blocks and headings appended (ids carry the page)."""
    if not docs:
        raise ValueError("concat needs at least one document")
    first = docs[0]
    return SourceDocument(
        first.kind, first.backend, first.version,
        tuple(p for d in docs for p in d.pages),
        tuple(x for d in docs for x in d.blocks),
        tuple(h for d in docs for h in d.headings))
