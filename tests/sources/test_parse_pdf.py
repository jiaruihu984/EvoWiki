"""PDF parsing over the committed fixtures in tests/fixtures/sources/."""

from pathlib import Path

import pytest

from evowiki.sources.parse_pdf import parse_pdf, render_pages
from evowiki.sources.parsed import ParseError
from evowiki.sources.unreadable import judge_all

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "sources"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_outline_pdf_pages_and_bookmarks() -> None:
    doc = parse_pdf(_read("outline.pdf"), title_hint="outline")
    assert doc.kind == "pdf"
    assert doc.title == "outline"            # the file name wins over the metadata (review #5)
    assert doc.headings_inline is False
    assert [p.page for p in doc.pages] == list(range(1, 13))
    assert all(p.read_by == "text" and p.reason is None for p in doc.pages)
    assert "这一节讲的是学习材料" in doc.pages[0].text
    assert "\r" not in doc.pages[0].text

    # 3 chapters (depth 0) with 2 sections each (depth 1): 9 bookmarks.
    chapters = [e for e in doc.outline if e.depth == 0]
    sections = [e for e in doc.outline if e.depth == 1]
    assert [(e.title, e.page) for e in chapters] == [
        ("第1章 Chapter 1", 1),
        ("第2章 Chapter 2", 5),
        ("第3章 Chapter 3", 9),
    ]
    assert len(sections) == 6
    assert [e.page for e in sections] == [1, 3, 5, 7, 9, 11]
    assert sections[3].title == "2.2 小节 Section 2.2"
    assert len(doc.outline) == 9


@pytest.mark.parametrize("hint", ["document", "", "Untitled (2)", "3f9a0c1d2e4b5a6c", "未命名资料"])
def test_a_generic_file_name_takes_the_metadata_title(hint: str) -> None:
    assert parse_pdf(_read("outline.pdf"), title_hint=hint).title == "学习方法 Learning Methods"


def test_no_outline_pdf_uses_title_hint() -> None:
    doc = parse_pdf(_read("no_outline.pdf"), title_hint="我的讲义")
    assert doc.title == "我的讲义"
    assert doc.outline == ()
    assert len(doc.pages) == 6
    assert doc.pages[2].text.startswith("第3部分 Part 3")


def test_mixed_scan_pages_5_to_14_are_unreadable() -> None:
    doc = parse_pdf(_read("mixed_scan.pdf"), title_hint="mixed_scan")
    assert len(doc.pages) == 20
    judged = judge_all(doc.pages)
    none_pages = [p.page for p in judged if p.read_by == "none"]
    assert none_pages == list(range(5, 15))
    assert all(p.reason == "no_text" for p in judged if p.read_by == "none")
    assert all(p.read_by == "text" for p in judged if not 5 <= p.page <= 14)


def test_encrypted_pdf_raises_user_facing_error() -> None:
    with pytest.raises(ParseError) as exc:
        parse_pdf(_read("encrypted.pdf"), title_hint="encrypted")
    assert str(exc.value) == "这个文件打不开（加了密码？）"


def test_broken_pdf_raises_user_facing_error() -> None:
    with pytest.raises(ParseError) as exc:
        parse_pdf(_read("broken.pdf"), title_hint="broken")
    assert str(exc.value) == "这个文件打不开（文件可能损坏）"


def test_not_a_pdf_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_pdf(b"hello, not a pdf", title_hint="x")


def test_render_pages_returns_png_per_requested_page() -> None:
    pngs = render_pages(_read("mixed_scan.pdf"), [5, 1], dpi=50)
    assert len(pngs) == 2
    assert all(b.startswith(b"\x89PNG\r\n\x1a\n") for b in pngs)


def test_render_pages_rejects_out_of_range_page() -> None:
    with pytest.raises(ValueError):
        render_pages(_read("no_outline.pdf"), [7])


def test_soft_hyphens_are_dropped_and_a_word_split_by_one_is_joined():
    """Live run 2026-10-21: the paper's text layer had 126 U+0002 (pdfium's soft hyphen) inside
    words such as 「exceeding」; U+00AD is the same mark in Unicode."""
    from evowiki.sources.parse_pdf import _clean
    assert _clean("ex\x02ceeding and X\x02ray") == "exceeding and Xray"
    assert _clean("the radiologists ex\x02\nceeding") == "the radiologists exceeding"
    assert _clean("ex\u00ad\r\n  ceeding") == "exceeding"
    assert _clean("chest X-\nray") == "chest X-\nray"          # a real hyphen stays, line too
    assert _clean("well-known") == "well-known"


# ---- follow-up B (2026-10-22): an unnamed upload takes the PDF's own title ----

def _bookmarked(first: str, title: str | None = None) -> bytes:
    """A 2-page PDF whose top-level bookmarks are `first` and 「第2章 后面」."""
    import io

    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    if title is not None:
        c.setTitle(title)
    for i, name in enumerate((first, "第2章 后面")):
        c.drawString(72, 720, f"page {i + 1}")
        c.bookmarkPage(f"b{i}")
        c.addOutlineEntry(name, f"b{i}", level=0)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_pdf_title_prefers_the_metadata_title() -> None:
    from evowiki.sources.parse_pdf import pdf_title
    assert pdf_title(_read("outline.pdf")) == "学习方法 Learning Methods"
    assert pdf_title(_bookmarked("Linear Algebra Notes", title="Eigenvalues (lecture notes)")) == (
        "Eigenvalues (lecture notes)")


def test_pdf_title_falls_back_to_a_first_bookmark_that_looks_like_a_title() -> None:
    from evowiki.sources.parse_pdf import pdf_title
    assert pdf_title(_bookmarked("Eigenvalues and diagonalization")) == (
        "Eigenvalues and diagonalization")
    assert pdf_title(_bookmarked("Linear Algebra", title="Untitled")) == "Linear Algebra"
    for part in ("4.1 What is an eigenvalue", "第1章 Chapter 1", "Chapter 3", "Contents", "目录",
                 "§2 Proofs", "Lecture 5"):
        assert pdf_title(_bookmarked(part)) is None, part


def test_pdf_title_is_none_without_metadata_or_bookmarks_or_for_a_broken_file() -> None:
    from evowiki.sources.parse_pdf import pdf_title
    assert pdf_title(_read("no_outline.pdf")) is None
    assert pdf_title(_read("broken.pdf")) is None
    assert pdf_title(b"not a pdf") is None


def test_a_formula_drawn_in_vector_shapes_counts_and_a_table_rule_does_not() -> None:
    import io

    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(600, 800))
    c.drawString(72, 720, "M x (t) = 1 + t m1")
    for i in range(3):                  # fraction bars
        c.line(100 + i * 40, 600, 130 + i * 40, 600)
    c.line(50, 500, 550, 500)           # a rule across the page
    c.save()
    assert parse_pdf(buf.getvalue(), title_hint="x").pages[0].drawn_marks == 3
