import sys
import types
from pathlib import Path

import pytest

from evowiki.assistant.tools.fetch_url import Fetched, FetchError, UrlBlocked, UrlFetcher
from evowiki.sources import parse as parse_mod
from evowiki.sources.parse import (
    detect_kind,
    fetch_and_parse,
    parse_bytes,
    parse_text,
)
from evowiki.sources.parsed import ParsedDoc, ParseError

FIX = Path(__file__).parent.parent / "fixtures" / "sources"
UNREACHABLE = "打不开这个网址，可以把正文贴进来"


def load(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_pptx_one_page_per_slide_and_image_only_slide_is_unreadable():
    doc = parse_bytes("pptx", load("slides.pptx"), title_hint="slides")
    assert doc.kind == "pptx" and not doc.headings_inline
    assert [p.page for p in doc.pages] == [1, 2, 3, 4]
    assert "Queue" in doc.pages[1].text
    assert "Slide number" not in doc.pages[0].text
    assert [(o.title, o.depth, o.page) for o in doc.outline][:2] == [
        ("Intro to Graphs", 0, 1), ("BFS", 0, 2)]
    assert doc.title == "Intro to Graphs"
    # slide 3 has a picture only: the parser itself marks it unreadable (no judge_all);
    # the short slide "BFS / Queue" stays text
    assert [(p.read_by, p.reason) for p in doc.pages] == [
        ("text", None), ("text", None), ("none", "no_text"), ("text", None)]
    assert doc.pages[2].text == "" and "BFS" in doc.pages[1].text


def test_docx_heading_levels_two_and_three():
    doc = parse_bytes("docx", load("notes.docx"), title_hint="notes")
    assert doc.kind == "docx" and doc.headings_inline and len(doc.pages) == 1
    levels = [o.depth for o in doc.outline]
    assert levels.count(0) == 2 and levels.count(1) == 3
    assert [o.title for o in doc.outline][:3] == ["Chapter One", "Section 1.1", "Section 1.2"]
    assert all(o.page is None for o in doc.outline)
    assert "# Chapter One" in doc.pages[0].text and doc.title == "Chapter One"


def test_xlsx_sheet_names_in_outline():
    doc = parse_bytes("xlsx", load("table.xlsx"), title_hint="table")
    assert [(o.title, o.depth, o.page) for o in doc.outline] == [
        ("Scores", 0, None), ("Notes", 0, None)]
    assert "Ann" in doc.pages[0].text and doc.title == "table"


def test_md_headings_and_code_fence_ignored():
    doc = parse_bytes("md", load("lecture.md"), title_hint="lecture")
    assert doc.headings_inline
    assert [(o.title, o.depth) for o in doc.outline] == [
        ("Lecture 1", 0), ("Merge sort", 1), ("Quick sort", 1), ("Lecture 2", 0)]
    fenced = parse_bytes("md", b"# A\n```\n# not a heading\n```\n", title_hint="x")
    assert [o.title for o in fenced.outline] == ["A"]


def test_plain_text_bytes_and_gbk():
    doc = parse_bytes("text", "你好，世界".encode("gb18030"), title_hint="note")
    assert doc.pages[0].text == "你好，世界" and doc.kind == "text"
    with pytest.raises(ParseError):
        parse_bytes("text", b"   \n", title_hint="empty")


def test_parse_text_pasted():
    doc = parse_text("第一行标题\n正文", title_hint=None)
    assert doc.kind == "text" and doc.title == "第一行标题" and len(doc.pages) == 1
    assert parse_text("abc", title_hint="我的笔记").title == "我的笔记"
    with pytest.raises(ParseError):
        parse_text("  ", title_hint=None)


def test_image_is_one_unread_page():
    doc = parse_bytes("image", load("photo.png"), title_hint="photo")
    (page,) = doc.pages
    assert doc.kind == "image" and page.text == "" and page.read_by == "none"
    assert page.reason == "image" and page.page == 1 and doc.outline == ()


@pytest.mark.parametrize("name,kind", [
    ("a.PDF", "pdf"), ("a.pptx", "pptx"), ("a.docx", "docx"), ("a.xlsx", "xlsx"),
    ("a.md", "md"), ("a.markdown", "md"), ("a.txt", "text"), ("a.jpeg", "image"),
    ("a.webp", "image")])
def test_detect_kind(name, kind):
    assert detect_kind(name, b"") == kind


def test_unknown_extension_lists_supported_formats():
    with pytest.raises(ParseError) as e:
        detect_kind("virus.exe", b"MZ")
    for ext in (".pdf", ".pptx", ".docx", ".xlsx", ".md", ".txt", ".png"):
        assert ext in str(e.value)


def test_corrupt_office_file_is_parse_error():
    with pytest.raises(ParseError):
        parse_bytes("pptx", b"not a zip at all", title_hint="x")


def test_pdf_dispatches_to_parse_pdf(monkeypatch):
    seen = {}
    fake = types.ModuleType("evowiki.sources.parse_pdf")

    def parse_pdf(data, *, title_hint):
        seen["args"] = (data, title_hint)
        return ParsedDoc("pdf", "T", (), (), False)

    fake.parse_pdf = parse_pdf
    monkeypatch.setitem(sys.modules, "evowiki.sources.parse_pdf", fake)
    assert parse_bytes("pdf", b"%PDF", title_hint="h").title == "T"
    assert seen["args"] == (b"%PDF", "h")


# --- URLs (fake fetcher, no network) -----------------------------------------------------------

class FakeFetcher:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.urls = result, error, []

    def fetch(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.result


def html_fetched(body=None, ctype="text/html; charset=utf-8", truncated=False):
    return Fetched(final_url="https://site.test/merge", content_type=ctype,
                   body=body if body is not None else load("page.html"), truncated=truncated)


def test_url_html_main_text_without_navigation():
    doc, raw, mime = fetch_and_parse("https://site.test/merge", FakeFetcher(html_fetched()))
    text = doc.pages[0].text
    assert doc.kind == "url" and doc.title == "Merge Sort Explained"
    assert "two sorted halves" in text
    assert "Pricing" not in text and "Copyright" not in text
    assert raw == load("page.html") and mime == "text/html; charset=utf-8"
    assert doc.headings_inline and [o.title for o in doc.outline] == ["Merge Sort Explained"]


def test_url_plain_text_and_markdown_used_directly():
    f = html_fetched(b"# Hi\n\nbody text here", ctype="text/markdown")
    doc, _raw, mime = fetch_and_parse("https://site.test/x.md", FakeFetcher(f))
    assert doc.pages[0].text.startswith("# Hi") and mime == "text/markdown"
    assert doc.outline[0].title == "Hi"


def test_url_pdf_goes_to_parse_pdf(monkeypatch):
    called = {}

    def fake(data, *, title_hint):
        called["a"] = (data, title_hint)
        return ParsedDoc("pdf", "P", (), (), False)

    monkeypatch.setattr(parse_mod, "_parse_pdf", fake)
    f = Fetched(final_url="https://site.test/p.pdf", content_type="application/pdf",
                body=b"%PDF-1.4")
    doc, raw, mime = fetch_and_parse("https://site.test/p.pdf", FakeFetcher(f))
    assert doc.kind == "pdf" and raw == b"%PDF-1.4" and mime == "application/pdf"
    assert called["a"] == (b"%PDF-1.4", "https://site.test/p.pdf")


@pytest.mark.parametrize("error", [UrlBlocked("内网"), FetchError("HTTP 404")])
def test_url_unreachable_message(error):
    with pytest.raises(ParseError, match=UNREACHABLE):
        fetch_and_parse("https://site.test/x", FakeFetcher(error=error))


def test_url_empty_body_message():
    f = html_fetched(b"<html><body>  </body></html>")
    with pytest.raises(ParseError, match=UNREACHABLE):
        fetch_and_parse("https://site.test/x", FakeFetcher(f))


def test_url_too_big_is_refused():
    f = html_fetched(truncated=True)
    with pytest.raises(ParseError, match="太大"):
        fetch_and_parse("https://site.test/x", FakeFetcher(f))


@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "http://10.0.0.5/", "http://localhost/",
                                 "file:///etc/passwd"])
def test_internal_address_refused_by_real_fetcher(url):
    fetcher = UrlFetcher(resolve=lambda h: ["93.184.216.34"])
    with pytest.raises(ParseError, match=UNREACHABLE):
        fetch_and_parse(url, fetcher)


# --- fix round 1 -------------------------------------------------------------------------------

def test_docx_image_only_is_none_page(monkeypatch):
    from evowiki.sources import parse_other
    monkeypatch.setattr(parse_other, "_markitdown",
                        lambda kind, data: "![a](data:image/png;base64,AAAA)\n")
    doc = parse_bytes("docx", b"x", title_hint="t")
    assert [(p.read_by, p.reason, p.text) for p in doc.pages] == [("none", "image", "")]
    monkeypatch.setattr(parse_other, "_markitdown", lambda kind, data: "  ")
    assert parse_bytes("docx", b"x", title_hint="t").pages[0].read_by == "none"


def test_txt_binary_garbage_refused():
    for data in (b"abc\x00\x01def", bytes(range(1, 31)) * 5, b"\xef\xbf\xbd" * 50):
        with pytest.raises(ParseError, match="文件可能损坏"):
            parse_bytes("text", data, title_hint="x")


def test_trafilatura_exception_wrapped(monkeypatch):
    import trafilatura
    monkeypatch.setattr(trafilatura, "extract", lambda *a, **k: 1 / 0)
    with pytest.raises(ParseError, match=UNREACHABLE):
        fetch_and_parse("https://site.test/m", FakeFetcher(html_fetched()))


def test_parse_fetched_by_mime():
    from evowiki.sources.parse import parse_fetched
    html = parse_fetched(load("page.html"), "text/html; charset=utf-8", url="https://s.test/")
    assert html.kind == "url" and html.title == "Merge Sort Explained"
    md = parse_fetched(b"# H\n\nbody", "text/markdown", url="https://s.test/")
    assert md.outline[0].title == "H" and md.headings_inline
    txt = parse_fetched(b"# not heading\nbody", "text/plain", url="https://s.test/",
                        title_hint="mine")
    assert txt.outline == () and not txt.headings_inline and txt.title == "mine"
    js = parse_fetched(b'{"a": 1}', "application/json", url="https://s.test/")
    assert js.outline == () and js.kind == "url"
    with pytest.raises(ParseError, match=UNREACHABLE):
        parse_fetched(b"   ", "text/plain", url="https://s.test/")
