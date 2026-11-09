import io

from pptx import Presentation
from pptx.util import Emu, Pt

from evowiki.sources.parse_pptx import parse_pptx
from evowiki.sources.toc import build_toc
from tests.sources._fakes import id_gen


def _save(prs):
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _deck():
    prs = Presentation()
    blank = prs.slide_layouts[6]
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Course"
    for label, title in [("01 · 研究意义", "研究意义"), ("01 · 研究意义", "为什么需要 AI"),
                         ("02 · 方法", "D-SCRIPT")]:
        s = prs.slides.add_slide(blank)
        grp = s.shapes.add_group_shape()
        box = grp.shapes.add_textbox(Emu(400000), Emu(200000), Emu(3000000), Emu(300000))
        box.text_frame.text = label
        box.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
        t = s.shapes.add_textbox(Emu(400000), Emu(600000), Emu(6000000), Emu(800000))
        t.text_frame.text = title
        t.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
        b = s.shapes.add_textbox(Emu(400000), Emu(3000000), Emu(6000000), Emu(800000))
        b.text_frame.text = "AUROC 0.81"
        b.text_frame.paragraphs[0].runs[0].font.size = Pt(18)
        s.notes_slide.notes_text_frame.text = f"讲稿：{title}"
    return _save(prs)


def test_titles_section_labels_and_notes():
    parsed, doc = parse_pptx(_deck(), title_hint="deck")
    assert [(e.title, e.depth, e.page) for e in parsed.outline] == [
        ("Course", 1, 1), ("研究意义", 0, 2), ("研究意义", 1, 2), ("为什么需要 AI", 1, 3),
        ("方法", 0, 4), ("D-SCRIPT", 1, 4)]
    assert "备注：讲稿：D-SCRIPT" in parsed.pages[3].text
    assert all(b.bbox is None or all(0 <= v <= 1 for v in b.bbox)
               for b in doc.blocks if b.kind != "notes")
    assert doc.backend == "pptx" and all(p.width and p.height for p in doc.pages)


def test_toc_groups_slides_under_section_labels():
    parsed, _ = parse_pptx(_deck(), title_hint="deck")
    toc = build_toc(parsed, "s", llm=None, new_id=id_gen())
    rows = [(r.title, r.depth) for r in toc.sections]
    assert rows == [("Course", 0), ("研究意义", 0), ("研究意义", 1), ("为什么需要 AI", 1),
                    ("方法", 0), ("D-SCRIPT", 1)]
    group = toc.sections[1]
    assert (group.page_from, group.page_to, group.text) == (2, 3, "")
    assert toc.sections[2].parent_id == group.section_id and toc.sections[0].parent_id is None


def test_picture_only_slide_is_unreadable_and_table_rows_are_text():
    prs = Presentation()
    blank = prs.slide_layouts[6]
    s = prs.slides.add_slide(blank)
    s.shapes.add_picture(io.BytesIO(_png()), Emu(100000), Emu(100000), Emu(500000), Emu(500000))
    s = prs.slides.add_slide(blank)
    tbl = s.shapes.add_table(2, 2, Emu(100000), Emu(100000), Emu(4000000), Emu(800000)).table
    for r, row in enumerate([["a", "b"], ["c", "d"]]):
        for c, v in enumerate(row):
            tbl.cell(r, c).text = v
    parsed, _ = parse_pptx(_save(prs), title_hint="t")
    assert (parsed.pages[0].read_by, parsed.pages[0].reason) == ("none", "no_text")
    assert "a | b\nc | d" in parsed.pages[1].text


def test_placeholder_title_beats_bigger_body_run():
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Real title"
    body = s.placeholders[1]
    body.text_frame.text = "huge body"
    body.text_frame.paragraphs[0].runs[0].font.size = Pt(60)
    parsed, _ = parse_pptx(_save(prs), title_hint="t")
    assert [(e.title, e.depth) for e in parsed.outline] == [("Real title", 0)]


def _png() -> bytes:
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


def test_numbered_text_is_the_title_when_nothing_larger_sits_beside_it():
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])
    t = s.shapes.add_textbox(Emu(400000), Emu(300000), Emu(6000000), Emu(800000))
    t.text_frame.text = "1. 引言"
    t.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
    b = s.shapes.add_textbox(Emu(400000), Emu(3000000), Emu(6000000), Emu(800000))
    b.text_frame.text = "正文内容"
    b.text_frame.paragraphs[0].runs[0].font.size = Pt(18)
    _, doc = parse_pptx(_save(prs), title_hint="deck")
    assert [(h.title, h.source) for h in doc.headings] == [("1. 引言", "slide_title")]
    assert all(h.level == 0 for h in doc.headings)


def _deck_with_a_picture() -> bytes:
    from io import BytesIO

    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches
    png = BytesIO()
    Image.new("RGB", (300, 80), "black").save(png, "PNG")
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Density"
    s.shapes.add_picture(BytesIO(png.getvalue()), Inches(1), Inches(2), Inches(3), Inches(1))
    prs.slides.add_slide(prs.slide_layouts[5]).shapes.title.text = "No picture"
    out = BytesIO()
    prs.save(out)
    return out.getvalue()


def test_a_slide_with_a_picture_says_so_and_gives_the_picture_for_reading():
    from io import BytesIO

    from PIL import Image

    from evowiki.sources.parse_pptx import PICTURE_UNREAD, parse_pptx, slide_pictures
    data = _deck_with_a_picture()
    doc = parse_pptx(data, title_hint="x")[0]
    assert PICTURE_UNREAD in doc.pages[0].text and doc.pages[0].image_area > 0.005
    assert doc.pages[1].image_area == 0.0
    first, second = slide_pictures(data, [1, 2], 0.005)
    assert Image.open(BytesIO(first)).size == (300, 80) and second is None
