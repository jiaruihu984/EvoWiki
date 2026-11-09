"""Generate the sample PDFs used by tests/sources/test_parse_pdf.py.

Run once by hand (`uv run python tests/fixtures/sources/make_pdf_fixtures.py`); the
generated files are committed and the tests only read them.

- outline.pdf     12 pages, 3 chapters x 2 sections of bookmarks, mixed Chinese/English
- mixed_scan.pdf  20 pages; pages 5-14 are "scans" (text drawn into an image, no text layer)
- no_outline.pdf  6 pages, no bookmarks, each page starts with a heading-styled line
- paper.pdf       4 pages, no bookmarks, shaped like a paper: a running header on every page,
                  numbered headings mid-page (page 2 starts 2, 2.1, 2.2 and 3), References
- encrypted.pdf   needs an open password
- broken.pdf      truncated bytes
- no_tounicode.pdf  1 page of text in a font whose glyphs have no Unicode mapping
                    (hand-written PDF: made-up /Differences glyph names, no ToUnicode)
- figure_page.pdf   2 pages of text; page 1 has a small picture (10% of the page),
                    page 2 a chart-like picture covering the lower half (50%)
- form_image.pdf    2 pages of text with a picture inside a Form XObject: page 1 the form
                    scaled x4 (~32% of the page), page 2 nested forms scaled x2 twice (~32%)

`main()` rewrites every file; to add one fixture without touching the others, call its
`make_*` function alone.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
CJK = "STSong-Light"
pdfmetrics.registerFont(UnicodeCIDFont(CJK))

_W, _H = A4

_BODY_ZH = (
    "这一节讲的是学习材料怎样切成小节。每一页都有足够多的文字，",
    "这样判定程序会把它当成有文字层的正常页面，而不是扫描件。",
)
_BODY_EN = (
    "This paragraph is plain English text so that the page carries a real text layer.",
    "Spaced repetition, retrieval practice and interleaving are covered in later pages.",
)


def _body(c: canvas.Canvas, y: float, page_no: int) -> None:
    lines = (*_BODY_ZH, *_BODY_EN, f"第 {page_no} 页 / page {page_no}")
    for line in lines:
        c.setFont(CJK, 11)
        c.drawString(60, y, line)
        y -= 18


def make_outline(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("学习方法 Learning Methods")
    # 12 pages; chapter k starts on page 4k-3, its two sections on 4k-3 and 4k-1.
    for page_no in range(1, 13):
        chapter = (page_no - 1) // 4 + 1
        offset = (page_no - 1) % 4
        y = _H - 72
        if offset == 0:
            ch_key = f"ch{chapter}"
            c.bookmarkPage(ch_key)
            c.addOutlineEntry(f"第{chapter}章 Chapter {chapter}", ch_key, level=0)
        if offset in (0, 2):
            sec = 1 if offset == 0 else 2
            sec_key = f"ch{chapter}s{sec}"
            c.bookmarkPage(sec_key)
            c.addOutlineEntry(f"{chapter}.{sec} 小节 Section {chapter}.{sec}", sec_key, level=1)
            c.setFont(CJK, 16)
            c.drawString(60, y, f"{chapter}.{sec} 小节 Section {chapter}.{sec}")
            y -= 30
        _body(c, y, page_no)
        c.showPage()
    c.save()


def _scan_image(page_no: int) -> bytes:
    img = Image.new("L", (620, 877), 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=18)
    lines = [
        f"Scanned page {page_no}",
        "This text was drawn into a picture,",
        "so the PDF has no text layer here.",
        "An OCR or vision reader is needed.",
    ]
    y = 60
    for line in lines:
        draw.text((50, y), line, fill=0, font=font)
        y += 32
    buf = io.BytesIO()
    img.convert("1").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def make_mixed_scan(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("混合扫描 Mixed Scan")
    for page_no in range(1, 21):
        if 5 <= page_no <= 14:
            img = ImageReader(io.BytesIO(_scan_image(page_no)))
            c.drawImage(img, 0, 0, width=_W, height=_H)
        else:
            _body(c, _H - 72, page_no)
        c.showPage()
    c.save()


def make_no_outline(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)  # no setTitle: title falls back to the hint
    for page_no in range(1, 7):
        c.setFont(CJK, 20)
        c.drawString(60, _H - 72, f"第{page_no}部分 Part {page_no}")
        _body(c, _H - 110, page_no)
        c.showPage()
    c.save()


PAPER_HEADER = "Spaced Practice in Small Groups - Preprint"
# per page: ("h", heading) or ("p", body line); every page starts with PAPER_HEADER and ends with
# its page number
PAPER_PAGES = (
    (("p", "Spaced Practice in Small Groups"), ("p", "A. Author and B. Author"),
     ("h", "Abstract"),
     ("p", "We compare spaced and massed practice in small study groups over four weeks."),
     ("p", "Spaced practice raised delayed recall in every group we measured."),
     ("h", "1 Introduction"),
     ("p", "Learners often mass their practice right before an exam."),
     ("p", "2 groups were compared in a pilot that is not reported here.")),
    (("p", "Earlier work suggests that spacing helps long-term retention."),
     ("h", "2 Related Work"),
     ("p", "Two lines of work bear on this question."),
     ("h", "2.1 Retrieval Practice"),
     ("p", "Testing yourself strengthens memory more than rereading does."),
     ("h", "2.2 Interleaving"),
     ("p", "Mixing problem types helps learners tell them apart."),
     ("h", "3 Methods"),
     ("p", "Forty students took part in eight groups of five.")),
    (("p", "Each group met twice a week for four weeks."),
     ("h", "3.1 Materials"),
     ("p", "The material was a short course on basic statistics."),
     ("h", "4 Results"),
     ("p", "Delayed recall was higher after spaced practice in all groups."),
     ("h", "5 Discussion"),
     ("p", "The effect held for weaker and stronger students alike.")),
    (("p", "Small groups seem to keep the spacing schedule well."),
     ("h", "6 Conclusion"),
     ("p", "Spread practice out, even when studying together."),
     ("h", "References"),
     ("p", "[1] Cepeda, N. J. et al. Distributed practice in verbal recall tasks. 2006."),
     ("p", "[2] Roediger, H. L. and Karpicke, J. D. Test-enhanced learning. 2006.")),
)


def make_paper(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("Spaced Practice in Small Groups")
    for page_no, lines in enumerate(PAPER_PAGES, start=1):
        c.setFont("Helvetica", 9)
        c.drawString(60, _H - 40, PAPER_HEADER)
        y = _H - 80
        for kind, text in lines:
            if kind == "h":
                y -= 8
                c.setFont("Helvetica-Bold", 13)
            else:
                c.setFont("Helvetica", 11)
            c.drawString(60, y, text)
            y -= 22 if kind == "h" else 18
        c.setFont("Helvetica", 9)
        c.drawString(_W / 2, 40, str(page_no))
        c.showPage()
    c.save()


def make_encrypted(path: Path) -> None:
    enc = StandardEncryption("open-sesame", ownerPassword="owner-pw", strength=128)
    c = canvas.Canvas(str(path), pagesize=A4, encrypt=enc)
    _body(c, _H - 72, 1)
    c.showPage()
    c.save()


def make_broken(path: Path) -> None:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _body(c, _H - 72, 1)
    c.showPage()
    c.save()
    data = buf.getvalue()
    path.write_bytes(data[: len(data) // 3])


def _raw_pdf(content: bytes, font: bytes) -> bytes:
    """A one-page PDF with font /F1, written by hand (reportlab always adds a ToUnicode)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
         b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        font,
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def make_no_tounicode(path: Path) -> None:
    # Codes 0x21-0x7e are drawn with glyph names /g33../g126 that mean nothing to a reader:
    # the page shows text, but no character maps back to Unicode.
    names = b" ".join(b"/g%d" % code for code in range(0x21, 0x7F))
    font = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
            b" /Encoding << /Type /Encoding /Differences [33 " + names + b"] >> >>")
    lines = []
    y = 780
    for row in range(8):
        codes = bytes(0x41 + (row * 7 + k) % 26 for k in range(40))
        lines.append(b"BT /F1 12 Tf 50 %d Td (%s) Tj ET" % (y, codes))
        y -= 20
    path.write_bytes(_raw_pdf(b"\n".join(lines), font))


def _chart_image(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.line([(20, h - 20), (w - 10, h - 20)], fill=(0, 0, 0), width=2)
    draw.line([(20, 10), (20, h - 20)], fill=(0, 0, 0), width=2)
    pts = [(20 + i * (w - 40) // 9, h - 30 - (i * 37) % (h - 60)) for i in range(10)]
    draw.line(pts, fill=(200, 30, 30), width=3)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def make_figure_page(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("图页 Figure Page")
    # page 1: text plus a small picture, 0.4 x 0.25 of the page = 10%
    _body(c, _H - 72, 1)
    c.drawImage(ImageReader(io.BytesIO(_chart_image(300, 200))), 60, 120,
                width=_W * 0.4, height=_H * 0.25)
    c.showPage()
    # page 2: text plus a chart covering the lower half of the page = 50%
    _body(c, _H - 72, 2)
    c.drawImage(ImageReader(io.BytesIO(_chart_image(600, 420))), 0, 0,
                width=_W, height=_H * 0.5)
    c.showPage()
    c.save()


def make_form_image(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("表单图片 Form Image")
    # A 100 x 100 picture inside a Form XObject, the way PPT / Word / InDesign exports place
    # pictures: its bounds are in the form's space, not the page's.
    c.beginForm("pic")
    c.drawImage(ImageReader(io.BytesIO(_chart_image(120, 120))), 0, 0, width=100, height=100)
    c.endForm()
    # page 1: the form scaled x4 at (100, 300): 400 x 400 on the page, ~32%
    _body(c, _H - 72, 1)
    c.saveState()
    c.translate(100, 300)
    c.scale(4, 4)
    c.doForm("pic")
    c.restoreState()
    c.showPage()
    # page 2: a form scaled x2 inside another form scaled x2 at (50, 50): also 400 x 400
    c.beginForm("outer")
    c.saveState()
    c.scale(2, 2)
    c.doForm("pic")
    c.restoreState()
    c.endForm()
    _body(c, _H - 72, 2)
    c.saveState()
    c.translate(50, 50)
    c.scale(2, 2)
    c.doForm("outer")
    c.restoreState()
    c.showPage()
    c.save()


def main() -> None:
    make_outline(HERE / "outline.pdf")
    make_mixed_scan(HERE / "mixed_scan.pdf")
    make_no_outline(HERE / "no_outline.pdf")
    make_paper(HERE / "paper.pdf")
    make_encrypted(HERE / "encrypted.pdf")
    make_broken(HERE / "broken.pdf")
    make_no_tounicode(HERE / "no_tounicode.pdf")
    make_figure_page(HERE / "figure_page.pdf")
    make_form_image(HERE / "form_image.pdf")
    for name in ("outline", "mixed_scan", "no_outline", "paper", "encrypted", "broken",
                 "no_tounicode", "figure_page", "form_image"):
        p = HERE / f"{name}.pdf"
        print(f"{p.name}: {p.stat().st_size} bytes")


if __name__ == "__main__":
    main()
