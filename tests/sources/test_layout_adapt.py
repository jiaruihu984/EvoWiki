import json
from pathlib import Path

from evowiki.sources.layout_adapt import concat, from_docling, from_mineru, table_text

DATA = Path(__file__).parent / "data" / "layout"


def _load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _shift(raw, pages):
    return [{**it, "page_idx": it["page_idx"] + pages} for it in raw]


def test_mineru_resnet_headings_pages_and_boxes():
    doc = from_mineru(_load("mineru_resnet_p1-3.json"), first_page=1, version="t")
    heads = [(b.page, " ".join(b.text.split())) for b in doc.blocks if b.kind == "heading"]
    assert (1, "1. Introduction") in heads
    assert (3, "3.1. Residual Learning") in heads
    assert all(b.bbox is None or all(0 <= v <= 1 for v in b.bbox) for b in doc.blocks)
    assert {p.page for p in doc.pages} == {1, 2, 3}
    assert {h.title for h in doc.headings} >= {"1. Introduction"}
    assert any(b.kind == "figure" for b in doc.blocks)


def test_mineru_formula_keeps_latex_and_tag():
    doc = from_mineru(_load("mineru_d2l_math_p1-2.json"), first_page=1, version="t")
    f = [b for b in doc.blocks if b.kind == "formula"]
    assert f and all(b.latex and not b.latex.startswith("$$") for b in f)
    assert any("\\tag" in b.latex for b in f)


def test_mineru_page_idx_is_absolute():
    # recorded from pages 4-5 of the PDF: page_idx was 3 and 4, not 0 and 1
    raw = _shift(_load("mineru_resnet_p1-3.json"), 10)
    doc = from_mineru(raw, first_page=11, version="t")
    assert {p.page for p in doc.pages} == {11, 12, 13}
    assert {b.page for b in doc.blocks} <= {11, 12, 13}


def test_mineru_table_and_caption():
    raw = [{"type": "table", "table_body": "<table><tr><td>a</td><td>b</td></tr></table>",
            "table_caption": ["Table 1. x"], "bbox": [0, 0, 1000, 500], "page_idx": 3}]
    doc = from_mineru(raw, first_page=4, version="t")
    assert [(b.kind, b.text) for b in doc.blocks] == [("table", "a | b"), ("caption", "Table 1. x")]
    assert doc.blocks[0].html.startswith("<table>")


def test_docling_resnet_headings_and_flipped_boxes():
    doc = from_docling(_load("docling_resnet_p1-3.json"), version="t")
    heads = [" ".join(b.text.split()) for b in doc.blocks if b.kind == "heading"]
    assert "1. Introduction" in heads
    top = next(b for b in doc.blocks if b.kind == "heading")
    assert top.bbox is not None and top.bbox[1] < 0.5        # a page-1 title sits in the upper half
    assert top.bbox[1] < top.bbox[3]
    assert [p.page for p in doc.pages] == [1, 2, 3]


def test_concat_and_table_text():
    a = from_mineru(_load("mineru_resnet_p1-3.json"), first_page=1, version="t")
    b = from_mineru(_shift(_load("mineru_resnet_p1-3.json"), 3), first_page=4, version="t")
    c = concat([a, b])
    assert [p.page for p in c.pages] == [1, 2, 3, 4, 5, 6]
    assert len({x.block_id for x in c.blocks}) == len(c.blocks)
    assert table_text("<table><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></table>") == "a | b\n1 | 2"
