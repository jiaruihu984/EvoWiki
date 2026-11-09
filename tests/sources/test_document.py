from evowiki.sources.document import Block, DocPage, Heading, SourceDocument, merge_text_layer
from evowiki.sources.parsed import LayoutHead, OutlineEntry, PageText, ParsedDoc


def _doc():
    return SourceDocument(
        kind="pdf", backend="mineru", version="4.0.12",
        pages=(DocPage(1), DocPage(2), DocPage(3)),
        blocks=(
            Block("p1-b0", 1, "header", "Journal X"),
            Block("p1-b1", 1, "heading", "1. Intro\nduction", bbox=(0.1, 0.1, 0.5, 0.12), level=1),
            Block("p1-b2", 1, "paragraph", "Some text.\nMore text."),
            Block("p1-b3", 1, "formula", "E = mc2", latex="E=mc^2"),
            Block("p2-b0", 2, "heading", "1.1 Scope", level=2),
            Block("p2-b1", 2, "table", "a | b\n1 | 2", html="<table>…</table>"),
        ),
        headings=(Heading("h0", "1. Introduction", 1, 1, None, (0.1, 0.1, 0.5, 0.12), "layout"),))


def test_page_texts_put_each_heading_on_one_line_and_drop_headers():
    texts = _doc().page_texts()
    text1, heads1 = texts[1]
    assert text1.splitlines() == ["1. Intro duction", "Some text.", "More text.", "$$E=mc^2$$"]
    assert heads1 == [LayoutHead(1, 0, "1. Intro duction", 1)]
    assert texts[2][1] == [LayoutHead(2, 0, "1.1 Scope", 2)]
    assert texts[3] == ("", [])


def test_to_parsed_marks_empty_pages_unreadable():
    p = _doc().to_parsed("T")
    assert [pg.read_by for pg in p.pages] == ["text", "text", "none"]
    assert p.pages[2].reason == "no_text"
    assert [h.title for h in p.layout_heads] == ["1. Intro duction", "1.1 Scope"]
    assert p.outline == (OutlineEntry("1. Introduction", 1, 1),)


def test_json_round_trip():
    d = _doc()
    assert SourceDocument.from_json(d.to_json()) == d


def test_merge_takes_layout_pages_with_text_and_text_layer_elsewhere():
    layout = _doc().to_parsed("T")
    bookmarks = (OutlineEntry("Bookmark", 1, 1),)
    text = ParsedDoc("pdf", "T", tuple(PageText(i, f"layer {i}", "text", image_area=0.1 * i)
                                       for i in (1, 2, 3)), bookmarks, False)
    m = merge_text_layer(layout, text)
    assert m.pages[0].text.startswith("1. Intro duction")
    assert m.pages[2].text == "layer 3"
    assert m.pages[0].image_area == 0.0          # layout page: no text-layer facts
    assert m.pages[2].image_area == 0.1 * 3        # text-layer page keeps them
    assert {h.page for h in m.layout_heads} == {1, 2}
    assert m.outline == bookmarks
