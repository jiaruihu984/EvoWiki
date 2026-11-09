from evowiki.sources.document import Block, DocPage, SourceDocument
from evowiki.sources.locate import locate, locate_json


def _doc():
    return SourceDocument(
        kind="pdf", backend="mineru", version="4.0.12",
        pages=(DocPage(1), DocPage(2), DocPage(3)),
        blocks=(
            Block("p1-b0", 1, "header", "Journal X"),
            Block("p1-b1", 1, "heading", "1. Intro\nduction", bbox=(0.1, 0.1, 0.5, 0.12), level=1),
            Block("p1-b2", 1, "paragraph", "Some text.\nMore text."),
            Block("p2-b0", 2, "heading", "1.1 Scope", level=2),
        ),
        headings=())


def test_locate_finds_the_block_and_span():
    loc = locate(_doc(), "src", 1, "More   text")
    assert loc.block_id == "p1-b2" and loc.bbox is None and loc.page == 1
    # "More text" is characters 11..20 of the block; the brief's 21 would take in the full stop
    assert (loc.char_start, loc.char_end) == (11, 20)
    assert "Some text.\nMore text."[loc.char_start:loc.char_end] == "More text"


def test_locate_looks_one_page_around_then_gives_up():
    assert locate(_doc(), "src", 2, "Some text").page == 1
    assert locate(_doc(), "src", 3, "not there") is None
    assert locate(_doc(), "src", 3, "Some text") is None      # two pages away
    assert locate(_doc(), "src", 1, "   ") is None


def test_locate_matches_across_a_broken_word():
    assert locate(_doc(), "src", 1, "1. Introduction").block_id == "p1-b1"


def test_locate_uses_only_the_first_40_characters_of_a_long_quote():
    text = "The quick brown fox jumps over the lazy dog near the river bank"
    doc = SourceDocument("pdf", "mineru", "1", (DocPage(1),),
                         (Block("p1-b0", 1, "paragraph", text),), ())
    quote = text[:50] + " but a tail that the stored text never had"
    loc = locate(doc, "src", 1, quote)
    assert loc is not None and loc.block_id == "p1-b0"
    assert (loc.char_start, loc.char_end) == (0, 50)   # the 40th non-space character ends at 50


def test_locate_ignores_math_delimiters_written_differently():
    doc = SourceDocument(kind="pdf", backend="mineru", version="4", pages=(DocPage(1),),
                         blocks=(Block("p1-b0", 1, "paragraph", "已知 $x+1=0$，求 x"),))
    assert locate(doc, "s", 1, r"已知 \( x+1=0 \)，求").block_id == "p1-b0"


def test_locate_json_is_the_stored_shape_and_none_without_a_document_page_or_match():
    assert locate_json(_doc(), "s", 2, "1.1 Scope") == {
        "source_id": "s", "page": 2, "bbox": None, "block_id": "p2-b0", "char_start": 0,
        "char_end": 9}
    assert locate_json(None, "s", 2, "1.1 Scope") is None
    assert locate_json(_doc(), "s", None, "1.1 Scope") is None
    assert locate_json(_doc(), "s", 2, "not there") is None
