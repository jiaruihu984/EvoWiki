from evowiki.sources.chunk import chunk_section
from evowiki.sources.models import SectionRow
from evowiki.sources.toc import build_toc
from tests.sources._fakes import id_gen, pdf


def sec(text, page_from=None):
    return SectionRow("sec1", "src", None, 0, 0, "t", page_from, None, text, 0, None, "text")


def test_pages_recovered_and_markers_removed():
    (s,) = build_toc(pdf(3, outline=[("A", 0, 1)]), "src", llm=None, new_id=id_gen()).sections
    chunks = chunk_section(s, new_id=id_gen("c"), target_chars=200)
    assert [c.page for c in chunks] == [1, 2, 3]       # chunks never span pages
    assert all("〔p." not in c.text for c in chunks)
    assert [c.ord for c in chunks] == [0, 1, 2]
    assert "body text of page 2." in chunks[1].text
    assert all(c.section_id == s.section_id and c.source_id == "src" for c in chunks)


def test_short_paragraphs_merge_and_long_ones_split():
    short = chunk_section(sec("a\n\nb\n\nc"), new_id=id_gen("c"))
    assert len(short) == 1 and short[0].text == "a\n\nb\n\nc" and short[0].page is None
    long = "这是一句话。" * 100
    chunks = chunk_section(sec(long), new_id=id_gen("c"), target_chars=100)
    assert len(chunks) > 1 and "".join(c.text for c in chunks) == long
    assert all(len(c.text) <= 100 for c in chunks)


def test_overlong_sentence_is_kept_whole():
    word = "x" * 500
    (c,) = chunk_section(sec(word), new_id=id_gen("c"), target_chars=100)
    assert c.text == word


def test_first_page_comes_from_section_when_no_marker():
    chunks = chunk_section(sec("hello", page_from=7), new_id=id_gen("c"))
    assert chunks[0].page == 7
    assert chunk_section(sec(""), new_id=id_gen("c")) == []


def test_decimal_in_long_paragraph_still_splits():
    para = "Version 1.2 was released. " + "This sentence is filler text. " * 66
    assert len(para) > 1900
    chunks = chunk_section(sec(para), new_id=id_gen("c"))
    assert 2 <= len(chunks) <= 4 and all(len(c.text) <= 800 for c in chunks)
    assert "".join(c.text.replace("\n\n", "") for c in chunks).replace(" ", "") == \
        para.replace(" ", "")


def test_fixture_chunk_pages_match_sections():
    from tests.sources._fakes import fixture_doc
    toc = build_toc(fixture_doc("outline.pdf", "pdf"), "src", llm=None, new_id=id_gen())
    for s in toc.sections:
        for c in chunk_section(s, new_id=id_gen("c")):
            assert s.page_from <= c.page <= s.page_to
