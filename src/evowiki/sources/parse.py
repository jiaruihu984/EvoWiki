"""Parse dispatcher: file name / bytes / pasted text / URL -> ParsedDoc."""

from evowiki.assistant.tools.fetch_url import (
    TEXT_TYPES,
    Fetched,
    Fetcher,
    FetchError,
    UrlBlocked,
    UrlFetcher,
)
from evowiki.sources import config
from evowiki.sources.models import SourceKind
from evowiki.sources.parse_other import (
    MSG_EMPTY,
    MSG_URL_UNREADABLE,
    decode_text,
    parse_html,
    parse_image,
    parse_office,
    parse_plain,
)
from evowiki.sources.parsed import ParsedDoc, ParseError

SUPPORTED = {".pdf": "pdf", ".pptx": "pptx", ".docx": "docx", ".xlsx": "xlsx",
             ".md": "md", ".markdown": "md", ".txt": "text",
             ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image"}
MSG_URL = "打不开这个网址，可以把正文贴进来"
_OFFICE = ("pptx", "docx", "xlsx")
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"RIFF")


def detect_kind(filename: str, data: bytes) -> SourceKind:
    dot = filename.rfind(".")
    ext = filename[dot:].lower() if dot >= 0 else ""
    kind = SUPPORTED.get(ext)
    if kind is None:
        raise ParseError("不支持这种文件，目前支持：" + "、".join(SUPPORTED))
    return kind  # type: ignore[return-value]


def _parse_pdf(data: bytes, *, title_hint: str) -> ParsedDoc:
    from evowiki.sources.parse_pdf import parse_pdf  # lazy: pdfium only when needed
    return parse_pdf(data, title_hint=title_hint)


def parse_bytes(kind: SourceKind, data: bytes, *, title_hint: str) -> ParsedDoc:
    if kind == "pdf":
        return _parse_pdf(data, title_hint=title_hint)
    if kind in _OFFICE:
        return parse_office(kind, data, title_hint=title_hint)
    if kind in ("md", "text"):
        return parse_plain(decode_text(data), kind, title_hint=title_hint)
    if kind == "image":
        if not data.startswith(_IMAGE_MAGIC):
            raise ParseError("这个图片文件打不开或已损坏，换一个试试")
        return parse_image(title_hint)
    raise ParseError("不支持这种资料类型")


def parse_text(text: str, *, title_hint: str | None) -> ParsedDoc:
    """Pasted text."""
    return parse_plain(text, "text", title_hint=title_hint)


def default_fetcher() -> UrlFetcher:
    return UrlFetcher(accept=TEXT_TYPES + ("application/pdf", "application/atom+xml"), max_bytes=config.max_bytes())


def _fetch(url: str, fetcher: Fetcher) -> Fetched:
    try:
        f = fetcher.fetch(url)
    except (UrlBlocked, FetchError) as e:
        raise ParseError(MSG_URL) from e
    if f.truncated:
        raise ParseError(f"这个网址的内容太大了（超过 {config.max_bytes() // 1024 // 1024} MB），"
                         "可以把需要的部分贴进来")
    return f


def _decode(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for piece in content_type.split(";")[1:]:
        k, _, v = piece.strip().partition("=")
        if k.strip().lower() == "charset" and v.strip():
            charset = v.strip().strip("\"'")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def parse_fetched(data: bytes, mime: str, *, url: str, title_hint: str | None = None) -> ParsedDoc:
    """Re-parse bytes fetched from a URL, by mime. html -> "url" (main text via trafilatura),
    pdf -> "pdf", markdown -> "url" with headings, plain text / json -> "url" without
    treating `#` lines as headings."""
    ctype, mime = mime, mime.split(";")[0].strip().lower()
    try:
        if mime == "application/pdf":
            return _parse_pdf(data, title_hint=title_hint or url)
        text = _decode(data, ctype)
        if "html" in mime:
            doc = parse_html(text, url=url)
        else:
            doc = parse_plain(text, "url", title_hint=title_hint,
                              markdown=(mime == "text/markdown"))
            doc = _as_url(doc, url)
        if title_hint:
            doc = ParsedDoc(doc.kind, title_hint, doc.pages, doc.outline, doc.headings_inline)
        return doc
    except ParseError as e:
        if str(e) in (MSG_EMPTY, MSG_URL_UNREADABLE):    # nothing readable on the page
            raise ParseError(MSG_URL) from e
        raise


def fetch_and_parse(url: str, fetcher: Fetcher) -> tuple[ParsedDoc, bytes, str]:
    """(doc, raw bytes, content type). The content type keeps its parameters (charset), so the
    import re-parses the stored bytes with the same charset. Unreachable / blocked / empty
    pages all say the same thing."""
    f = _fetch(url, fetcher)
    return parse_fetched(f.body, f.content_type, url=f.final_url), f.body, f.content_type.strip()


def _as_url(doc: ParsedDoc, final_url: str) -> ParsedDoc:
    return ParsedDoc("url", doc.title or final_url, doc.pages, doc.outline, doc.headings_inline)
