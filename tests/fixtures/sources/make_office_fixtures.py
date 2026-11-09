"""Regenerates the small Office / web / image fixtures next to this file.
Run: uv run python tests/fixtures/sources/make_office_fixtures.py
(docx is written by hand as a zip because python-docx is not a dependency.)"""

import io
import zipfile
from pathlib import Path

from openpyxl import Workbook
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

HERE = Path(__file__).parent


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 40, 40)).save(buf, "PNG")
    return buf.getvalue()


def make_pptx() -> None:
    prs = Presentation()
    texts = [("Intro to Graphs", "Vertices and edges"), ("BFS", "Queue"), None,
             ("Summary", "Graphs model relations")]
    png = HERE / "photo.png"
    for item in texts:
        if item is None:                                 # image-only slide, no text at all
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            slide.shapes.add_picture(str(png), Inches(1), Inches(1))
        else:
            slide = prs.slides.add_slide(prs.slide_layouts[1])
            slide.shapes.title.text = item[0]
            slide.placeholders[1].text = item[1]
    prs.save(HERE / "slides.pptx")


def make_docx() -> None:
    def p(style: str | None, text: str) -> str:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"

    body = "".join([
        p("Heading1", "Chapter One"), p(None, "Intro paragraph."),
        p("Heading2", "Section 1.1"), p(None, "Some text."),
        p("Heading2", "Section 1.2"), p(None, "More text."),
        p("Heading1", "Chapter Two"), p(None, "Closing."),
        p("Heading2", "Section 2.1"), p(None, "End."),
    ])
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    styles = "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{i}"><w:name w:val="heading {i}"/></w:style>'
        for i in (1, 2))
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/_rels/document.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "word/styles.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:styles {ns}>{styles}</w:styles>',
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document {ns}><w:body>{body}</w:body></w:document>',
    }
    with zipfile.ZipFile(HERE / "notes.docx", "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)


def make_xlsx() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Scores"
    ws.append(["name", "score"])
    ws.append(["Ann", 90])
    ws.append(["Bob", 75])
    ws2 = wb.create_sheet("Notes")
    ws2.append(["topic", "comment"])
    ws2.append(["graphs", "review BFS"])
    wb.save(HERE / "table.xlsx")


def make_text() -> None:
    (HERE / "lecture.md").write_text(
        "# Lecture 1\n\nIntro text about sorting.\n\n## Merge sort\n\nDivide and conquer.\n\n"
        "## Quick sort\n\nPivot based.\n\n# Lecture 2\n\nHashing basics.\n", encoding="utf-8")
    (HERE / "page.html").write_text(
        "<html><head><title>Merge Sort Explained</title></head><body>"
        "<nav><ul><li>Home</li><li>Pricing</li><li>Login</li><li>Contact sales</li></ul></nav>"
        "<article><h1>Merge Sort Explained</h1>"
        "<p>Merge sort splits an array in half, sorts each half recursively and then merges the "
        "two sorted halves into one sorted array. It runs in O(n log n) time in every case and "
        "needs linear extra space for the merge step.</p>"
        "<p>The merge step walks both halves with two indices and always copies the smaller "
        "front element, which makes the sort stable. Because the recursion depth is logarithmic, "
        "the algorithm behaves predictably even on adversarial inputs.</p>"
        "<p>In practice, hybrid versions switch to insertion sort for tiny sub-arrays to cut the "
        "constant factor, and external variants apply the same idea to data on disk.</p>"
        "</article><footer>Copyright 2026 Example Corp. All rights reserved.</footer>"
        "</body></html>", encoding="utf-8")


if __name__ == "__main__":
    (HERE / "photo.png").write_bytes(_png())
    make_pptx()
    make_docx()
    make_xlsx()
    make_text()
