"""Parsers for everything that is not a PDF: Office files (docx / xlsx via MarkItDown, pptx via python-pptx), markdown / plain
text, web pages (trafilatura) and images. Office / md / txt / web pages are text taken straight
from the file, so a short page is simply short: the char-density judge (`unreadable`) is for PDF
pages only. These parsers mark EMPTY pages (picture-only slide, image-only docx, images) as
read_by="none" themselves."""

import re
from io import BytesIO

from evowiki.sources.models import SourceKind
from evowiki.sources.parsed import OutlineEntry, PageText, ParsedDoc, ParseError

MSG_BROKEN = "这个文件打不开或已损坏，换一个试试"
MSG_EMPTY = "文件里没有可读的文字"
MSG_URL_UNREADABLE = "打不开这个网址，可以把正文贴进来"

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_IMAGE_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_SHEET = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
_EXT = {"docx": ".docx", "xlsx": ".xlsx"}


def headings(text: str) -> tuple[OutlineEntry, ...]:
    """`#` headings outside code fences; depth = level - 1; no page number."""
    out: list[OutlineEntry] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and (m := _HEADING.match(line)):
            out.append(OutlineEntry(m.group(2).strip(), len(m.group(1)) - 1, None))
    return tuple(out)


def _title(text: str, title_hint: str | None) -> str:
    if title_hint:
        return title_hint
    for line in text.splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()[:40]
    return ""


MSG_BAD_TEXT = "这个文件打不开（文件可能损坏）"
_BAD_RATIO = 0.05


def decode_text(data: bytes) -> str:
    if b"\0" in data:
        raise ParseError(MSG_BAD_TEXT)
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ParseError(MSG_BAD_TEXT)
    chars = [c for c in text if not c.isspace()]
    bad = sum(1 for c in chars if c == chr(0xFFFD) or ord(c) < 32 or 0xE000 <= ord(c) <= 0xF8FF)
    if chars and bad / len(chars) > _BAD_RATIO:
        raise ParseError(MSG_BAD_TEXT)
    return text


_SITE_TAIL = r"\s+[-–—|·_]\s+\S"


def parse_plain(text: str, kind: SourceKind, *, title_hint: str | None,
                markdown: bool | None = None) -> ParsedDoc:
    """md / txt / pasted text: one page. Markdown keeps its headings inline; `markdown=False`
    makes `#` lines ordinary text (plain text served by a URL)."""
    text = text.strip()
    if not text:
        raise ParseError(MSG_EMPTY)
    md = kind in ("md", "url", "docx") if markdown is None else markdown
    outline = headings(text) if md else ()
    first = outline[0].title if outline else None
    title = title_hint or first or _title(text, None)
    return ParsedDoc(kind=kind, title=title, pages=(PageText(1, text, "text"),),
                     outline=outline, headings_inline=md)


def _markitdown(kind: str, data: bytes) -> str:
    from markitdown import MarkItDown
    try:
        return MarkItDown().convert_stream(BytesIO(data), file_extension=_EXT[kind]).markdown
    except Exception as e:                      # corrupt zip, bad XML, missing extra ...
        raise ParseError(MSG_BROKEN) from e


def _parse_xlsx(md: str, title_hint: str) -> ParsedDoc:
    md = md.strip()
    if not md:
        raise ParseError(MSG_EMPTY)
    outline = tuple(OutlineEntry(m.group(1), 0, None) for m in _SHEET.finditer(md))
    return ParsedDoc("xlsx", title_hint, (PageText(1, md, "text"),), outline, False)


def parse_office(kind: SourceKind, data: bytes, *, title_hint: str) -> ParsedDoc:
    if kind == "pptx":
        from evowiki.sources.parse_pptx import parse_pptx
        return parse_pptx(data, title_hint=title_hint)[0]
    md = _markitdown(kind, data)
    if kind == "xlsx":
        return _parse_xlsx(md, title_hint)
    clean = _IMAGE_MD.sub("", md).strip()
    if not clean:                                         # image-only or empty document
        reason = "image" if _IMAGE_MD.search(md) else "no_text"
        return ParsedDoc("docx", title_hint, (PageText(1, "", "none", reason),), (), True)
    doc = parse_plain(clean, "docx", title_hint=None)    # docx: headings in the text
    outline = doc.outline
    return ParsedDoc("docx", outline[0].title if outline else title_hint, doc.pages, outline,
                     True)


def parse_image(title_hint: str) -> ParsedDoc:
    page = PageText(1, "", "none", "image")
    return ParsedDoc("image", title_hint, (page,), (), False)


def parse_html(html: str, *, url: str) -> ParsedDoc:
    """Main text of a web page as markdown; navigation and footers are dropped."""
    import trafilatura
    try:
        text = trafilatura.extract(html, output_format="markdown", include_tables=True) or ""
        meta = trafilatura.extract_metadata(html) if text.strip() else None
    except Exception as e:                      # lxml / trafilatura internals
        raise ParseError(MSG_URL_UNREADABLE) from e
    if not text.strip():
        raise ParseError(MSG_EMPTY)
    doc = parse_plain(text, "url", title_hint=None)
    title = (meta.title if meta is not None and meta.title else None) or doc.title or url
    if doc.outline and re.match(_SITE_TAIL, title.removeprefix(doc.outline[0].title)):
        title = doc.outline[0].title       # 「行列式 - 维基百科，自由的百科全书」: the page, not the site
    return ParsedDoc("url", title, doc.pages, doc.outline, True)
