from datetime import UTC, datetime

import pytest

from evowiki.content.models import LICENSE_TERMS, Source
from evowiki.pipeline.chunking import chunk_document, ingest_registration, passage_id
from evowiki.pipeline.sources import (
    IneligibleSourceError,
    SourceDocument,
    SourceRegistration,
)
from evowiki.pipeline.text import html_to_text

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def test_html_to_text_keeps_paragraphs_and_headings_drops_noise():
    html = """<html><head><style>p{}</style><script>var x=1;</script></head><body>
    <nav>menu</nav><h2>Principal  components</h2><p>PCA finds   directions.</p>
    <blockquote>Quoted abstract.</blockquote><p>Second <b>para</b>.</p>
    <footer>legal</footer></body></html>"""
    assert html_to_text(html) == "## Principal components\n\nPCA finds directions.\n\nSecond para."


def test_chunk_merges_short_paragraphs_and_splits_on_headings():
    text = "# A\n\none.\n\ntwo.\n\n# B\n\nthree."
    chunks = chunk_document("s", "u", text, max_chars=100)
    assert [c.text for c in chunks] == ["# A\n\none.\n\ntwo.", "# B\n\nthree."]
    assert [c.locator for c in chunks] == ["u#c0", "u#c1"]
    assert chunks[0].id == passage_id("s", "u#c0", chunks[0].text)


def test_chunk_splits_long_paragraph_at_sentences():
    para = " ".join(f"Sentence number {i} is here." for i in range(20))
    chunks = chunk_document("s", "u", para, max_chars=120)
    assert len(chunks) > 1 and all(len(c.text) <= 120 for c in chunks)
    assert " ".join(c.text for c in chunks) == para


def test_chunk_drops_quoted_lines_and_is_deterministic():
    text = "> quoted from arXiv\n\nOwn text."
    a = chunk_document("s", "u", text)
    assert [c.text for c in a] == ["Own text."]
    assert a == chunk_document("s", "u", text)


def _reg(verified: bool) -> SourceRegistration:
    src = Source(id="s", title="S", locator="https://s", license="CC-BY-4.0",
                 license_url="https://s/l", license_verified_at=NOW if verified else None,
                 **LICENSE_TERMS["CC-BY-4.0"].model_dump())
    return SourceRegistration(source=src, license_marker="cc-by",
                              documents=[SourceDocument(locator="https://s/doc", title="Doc")])


def test_ingest_requires_eligible_source():
    with pytest.raises(IneligibleSourceError):
        ingest_registration(_reg(False), lambda url: "<p>x</p>")


def test_ingest_fetches_converts_and_chunks():
    passages = ingest_registration(_reg(True), lambda url: "<p>Vectors add.</p><p>More.</p>")
    assert [p.text for p in passages] == ["Vectors add.\n\nMore."]
    assert passages[0].source_id == "s" and passages[0].locator == "https://s/doc#c0"


def test_chunk_drops_quoted_lines_inside_paragraph():
    text = "Own line.\n> quoted line\nAnother own line."
    assert [c.text for c in chunk_document("s", "u", text)] == ["Own line.\nAnother own line."]


def test_html_to_text_separates_table_cells_and_rows():
    html = "<table><tr><th>k</th><th>v</th></tr><tr><td>1</td><td>2</td></tr></table>"
    assert html_to_text(html) == "k | v\n\n1 | 2"


def test_chunk_drops_paragraphs_matching_excluded_text():
    text = "Own intro.\n\nThe abstract from the paper is:\n\nWe propose LoRA, which freezes.\n\nOwn end."
    chunks = chunk_document("s", "u", text, exclude=["abstract from the paper is:",
                                                     "We propose LoRA"])
    assert [c.text for c in chunks] == ["Own intro.\n\nOwn end."]


def test_ingest_applies_per_document_exclusions():
    reg = _reg(True)
    reg.documents[0] = SourceDocument(locator="https://s/doc", title="Doc",
                                      exclude=["We propose"])
    html = "<p>Own text.</p><p><em>We propose a quoted abstract.</em></p>"
    assert [p.text for p in ingest_registration(reg, lambda url: html)] == ["Own text."]
