import pytest

from evowiki.llm.providers import ProviderRejectedError
from evowiki.sources.parsed import OutlineEntry, PageText, ParsedDoc
from evowiki.sources.toc import TocEntry, TocPlan, build_toc
from evowiki.wikiapp.llm import LLMUnavailable
from tests.sources._fakes import FakeLLM, fixture_doc, id_gen, pdf

OUTLINE = [("A", 0, 1), ("A.1", 1, 3), ("B", 0, 5), ("C", 0, 8), ("D", 0, 10), ("E", 0, 12)]


def test_outline_mode_six_sections_no_llm():
    llm = FakeLLM()
    toc = build_toc(pdf(12, outline=OUTLINE), "src", llm=llm, new_id=id_gen())
    assert toc.mode == "outline" and toc.note is None and llm.calls == []
    secs = toc.sections
    assert [s.title for s in secs] == ["A", "A.1", "B", "C", "D", "E"]
    assert [(s.page_from, s.page_to) for s in secs] == [(1, 2), (3, 4), (5, 7), (8, 9),
                                                        (10, 11), (12, 12)]
    assert [s.ord for s in secs] == list(range(6))
    assert secs[1].parent_id == secs[0].section_id and secs[2].parent_id is None
    assert "〔p.3〕" in secs[1].text and "body text of page 4" in secs[1].text
    assert secs[0].tokens > 0 and all(s.read_by == "text" for s in secs)


def test_outline_leading_pages_section():
    toc = build_toc(pdf(6, outline=[("Ch1", 0, 3), ("Ch2", 0, 5)]), "src", llm=None,
                    new_id=id_gen())
    assert [s.title for s in toc.sections] == ["前面的页", "Ch1", "Ch2"]
    assert (toc.sections[0].page_from, toc.sections[0].page_to) == (1, 2)


def test_outline_same_page_entries_do_not_duplicate_text():
    toc = build_toc(pdf(4, outline=[("A", 0, 1), ("A.1", 1, 1), ("B", 0, 3)]), "src",
                    llm=None, new_id=id_gen())
    a, a1, b = toc.sections
    assert a.text == "" and "page 1" in a1.text and "page 2" in a1.text and "page 3" in b.text


def test_outline_titles_without_the_number_on_the_page_split_the_page():
    text = "1 Introduction\nintro body\n2 Background\nbackground body\n3.1 Model Stacks\nstack body"
    doc = ParsedDoc("pdf", "x", (PageText(page=1, text=text, read_by="text"),),
                    (OutlineEntry("Introduction", 0, 1), OutlineEntry("Background", 0, 1),
                     OutlineEntry("Model Stacks", 1, 1)), False)
    intro, background, stacks = build_toc(doc, "src", llm=None, new_id=id_gen()).sections
    assert "intro body" in intro.text and "background" not in intro.text
    assert "background body" in background.text and "stack" not in background.text
    assert "stack body" in stacks.text


def test_outline_title_prefers_the_heading_over_a_running_header():
    text = "1.9. Exercises\nglossary entry\n1.8 Glossary\nterm\n1.9 Exercises\nexercise body"
    doc = ParsedDoc("pdf", "x", (PageText(page=1, text=text, read_by="text"),),
                    (OutlineEntry("Glossary", 0, 1), OutlineEntry("Exercises", 0, 1)), False)
    secs = build_toc(doc, "src", llm=None, new_id=id_gen()).sections
    exercises = secs[-1]
    assert exercises.title == "Exercises" and exercises.text.endswith("1.9 Exercises\nexercise body")
    assert "term" not in exercises.text


def test_no_outline_llm_plan_used():
    plan = TocPlan(sections=[TocEntry(title="Page 1 heading", depth=0, page=1),
                             TocEntry(title="Page 5 heading", depth=0, page=5)])
    llm = FakeLLM(plan)
    toc = build_toc(pdf(8), "src", llm=llm, new_id=id_gen())
    assert toc.mode == "llm" and len(llm.calls) == 1
    assert [(s.title, s.page_from, s.page_to) for s in toc.sections] == [
        ("Page 1 heading", 1, 4), ("Page 5 heading", 5, 8)]
    role, purpose, prompt = llm.calls[0]
    assert (role, purpose) == ("generator", "sources.toc")
    assert prompt.index("<<<资料内容开始>>>") < prompt.index("Page 3 heading") \
        < prompt.index("<<<资料内容结束>>>")
    assert "l4 of 3" not in prompt and "l3 of 3" in prompt   # first 3 lines only


def test_llm_bad_entries_dropped_but_rest_kept():
    plan = TocPlan(sections=[TocEntry(title="Page 1 heading", depth=0, page=1),
                             TocEntry(title="out", depth=0, page=99),
                             TocEntry(title="Page 4 heading", depth=0, page=4),
                             TocEntry(title="Page 2 heading", depth=0, page=2),     # back
                             TocEntry(title="Made up", depth=0, page=6)])           # not there
    toc = build_toc(pdf(8), "src", llm=FakeLLM(plan), new_id=id_gen())
    assert toc.mode == "llm" and [s.title for s in toc.sections] == ["Page 1 heading",
                                                                     "Page 4 heading"]


@pytest.mark.parametrize("result", [
    TocPlan(sections=[TocEntry(title="x", depth=0, page=6), TocEntry(title="y", depth=0, page=2)]),
    TocPlan(sections=[TocEntry(title="only", depth=0, page=1)]),
    LLMUnavailable("no"),
])
def test_llm_unusable_falls_back_to_pages(result):
    toc = build_toc(pdf(25), "src", llm=FakeLLM(result), new_id=id_gen())
    assert toc.mode == "pages" and toc.note == "没认出目录，按页分了"
    assert [(s.page_from, s.page_to) for s in toc.sections] == [(1, 10), (11, 20), (21, 25)]


def test_llm_rejection_propagates():
    with pytest.raises(ProviderRejectedError):
        build_toc(pdf(8), "src", llm=FakeLLM(ProviderRejectedError("402")), new_id=id_gen())


def test_short_pdf_or_no_llm_goes_straight_to_pages():
    llm = FakeLLM()
    assert build_toc(pdf(3), "src", llm=llm, new_id=id_gen()).mode == "pages"
    assert build_toc(pdf(30), "src", llm=None, new_id=id_gen()).mode == "pages"
    assert llm.calls == []


def test_scanned_pages_are_none_and_excluded():
    doc = pdf(6, outline=[("A", 0, 1), ("Scan", 0, 3), ("B", 0, 5)], none_pages=(3, 4))
    a, scan, b = build_toc(doc, "src", llm=None, new_id=id_gen()).sections
    assert scan.read_by == "none" and scan.text == "" and scan.tokens == 0
    assert a.read_by == "text" and b.read_by == "text"
    mixed = pdf(4, outline=[("A", 0, 1)], none_pages=(2,))
    (sec,) = build_toc(mixed, "src", llm=None, new_id=id_gen()).sections
    assert sec.read_by == "text" and "page 2" not in sec.text and "page 3" in sec.text


def test_markdown_headings():
    text = "intro\n\n# One\nbody 1\n\n## One.a\nbody a\n```\n# not a heading\n```\n# Two\nbody 2"
    doc = ParsedDoc("md", "t", (PageText(1, text, "text"),), (), True)
    toc = build_toc(doc, "src", llm=None, new_id=id_gen())
    assert toc.mode == "headings"
    assert [s.title for s in toc.sections] == ["前面的内容", "One", "One.a", "Two"]
    assert [s.depth for s in toc.sections] == [0, 0, 1, 0]
    assert toc.sections[2].parent_id == toc.sections[1].section_id
    assert "# not a heading" in toc.sections[2].text
    assert all(s.page_from is None and "〔p." not in s.text for s in toc.sections)


def test_markdown_without_headings_is_one_section():
    doc = ParsedDoc("md", "t", (PageText(1, "just text", "text"),), (), True)
    toc = build_toc(doc, "src", llm=FakeLLM(), new_id=id_gen())
    assert toc.mode == "pages" and len(toc.sections) == 1 and toc.note is None


def test_pptx_one_section_per_slide():
    pages = tuple(PageText(i, f"Slide title {i}\nbullet", "text") for i in (1, 2, 3))
    doc = ParsedDoc("pptx", "deck", pages, (OutlineEntry("Custom", 0, None),), False)
    toc = build_toc(doc, "src", llm=None, new_id=id_gen())
    assert toc.mode == "headings"
    assert [s.title for s in toc.sections] == ["Slide title 1", "Slide title 2", "Slide title 3"]
    assert [s.page_from for s in toc.sections] == [1, 2, 3]


# ---- recorded fixtures through the real parsers ----

def test_fixture_outline_pdf_nine_sections_contiguous_no_llm():
    llm = FakeLLM()
    toc = build_toc(fixture_doc("outline.pdf", "pdf"), "src", llm=llm, new_id=id_gen())
    assert toc.mode == "outline" and llm.calls == []
    assert len(toc.sections) == 9           # 3 chapters + 6 sections = 9 bookmarks
    assert toc.sections[0].page_from == 1 and toc.sections[-1].page_to == 12
    for a, b in zip(toc.sections, toc.sections[1:], strict=False):
        assert b.page_from in (a.page_to, a.page_to + 1)        # shared page or next page


def test_fixture_no_outline_pdf_one_llm_call():
    doc = fixture_doc("no_outline.pdf", "pdf")
    n = len(doc.pages)
    plan = TocPlan(sections=[TocEntry(title="第1部分 Part 1", depth=0, page=1),
                             TocEntry(title=f"第{n // 2 + 1}部分 Part {n // 2 + 1}", depth=0,
                                      page=n // 2 + 1)])
    llm = FakeLLM(plan)
    toc = build_toc(doc, "src", llm=llm, new_id=id_gen())
    assert toc.mode == "llm" and len(llm.calls) == 1
    bad = FakeLLM(TocPlan(sections=[TocEntry(title="x", depth=0, page=1)]))
    fb = build_toc(doc, "src", llm=bad, new_id=id_gen())
    assert fb.mode == "pages" and len(bad.calls) == 1


def test_fixture_mixed_scan_pages_not_in_text():
    doc = fixture_doc("mixed_scan.pdf", "pdf")
    none_pages = [p.page for p in doc.pages if p.read_by == "none"]
    assert none_pages
    toc = build_toc(doc, "src", llm=None, new_id=id_gen())
    text = "\n".join(s.text for s in toc.sections)
    for n in none_pages:
        assert f"〔p.{n}〕" not in text
    assert any(f"〔p.{p.page}〕" in text for p in doc.pages if p.read_by == "text")


def test_fixture_pptx_four_sections_even_with_outline():
    doc = fixture_doc("slides.pptx", "pptx")
    toc = build_toc(doc, "src", llm=FakeLLM(), new_id=id_gen())
    assert toc.mode == "headings" and len(toc.sections) == 4
    assert [s.page_from for s in toc.sections] == [1, 2, 3, 4]


def test_pptx_with_paged_outline_still_one_section_per_slide():
    pages = tuple(PageText(i, f"S{i}\nx", "text") for i in (1, 2, 3))
    doc = ParsedDoc("pptx", "d", pages, (OutlineEntry("S1", 0, 1), OutlineEntry("S3", 0, 3)), False)
    assert len(build_toc(doc, "src", llm=None, new_id=id_gen()).sections) == 3


def test_leading_section_is_not_a_parent():
    toc = build_toc(pdf(6, outline=[("Sub", 1, 3), ("Sub2", 1, 5)]), "src", llm=None,
                    new_id=id_gen())
    assert [s.parent_id for s in toc.sections] == [None, None, None]


def test_heading_keeps_hash_in_title():
    doc = ParsedDoc("md", "t", (PageText(1, "# C#\nx\n## Closing ##\ny", "text"),), (), True)
    assert [s.title for s in build_toc(doc, "src", llm=None, new_id=id_gen()).sections] == [
        "C#", "Closing"]


# ---- bookmark depths checked against section numbering (materials 3+4, Task 14) ----

def test_flat_bookmarks_with_multilevel_numbers_use_the_numbers():
    outline = [("第1章 绪论", 0, 1), ("1.1 背景", 0, 2), ("1.1.1 历史", 0, 3), ("1.2 目标", 0, 4),
               ("2 方法", 0, 5), ("2.1 数据", 0, 6)]
    toc = build_toc(pdf(8, outline=outline), "src", llm=None, new_id=id_gen())
    assert toc.mode == "outline"
    assert [s.depth for s in toc.sections] == [0, 1, 2, 1, 0, 1]
    by = {s.section_id: s for s in toc.sections}
    assert by[toc.sections[2].parent_id].title == "1.1 背景"
    assert toc.sections[5].parent_id == toc.sections[4].section_id
    assert any("编号" in d for d in toc.diagnostics)


def test_bookmark_depths_mostly_off_from_the_numbers_use_the_numbers():
    outline = [("书名", 0, 1), ("1 引言", 1, 2), ("1.1 问题", 2, 3), ("2 理论", 1, 4),
               ("2.1 定义", 2, 5), ("附录", 1, 7)]
    toc = build_toc(pdf(8, outline=outline), "src", llm=None, new_id=id_gen())
    # unnumbered entries move with the chapters (bookmarked one level down here)
    assert [s.depth for s in toc.sections] == [0, 0, 1, 0, 1, 0]
    assert toc.diagnostics


def test_bookmark_depths_that_agree_with_the_numbers_are_kept():
    outline = [("1 引言", 0, 1), ("1.1 问题", 1, 2), ("2 理论", 0, 4), ("2.1 定义", 2, 5)]
    toc = build_toc(pdf(8, outline=outline), "src", llm=None, new_id=id_gen())
    assert [s.depth for s in toc.sections] == [0, 1, 0, 2]          # 1 of 4 off: not > half
    assert toc.diagnostics == ()


def test_flat_unnumbered_bookmarks_go_to_the_llm_toc():
    outline = [("Page 1 heading", 0, 1), ("Page 4 heading", 0, 4), ("Page 6 heading", 0, 6)]
    plan = TocPlan(sections=[TocEntry(title="Page 1 heading", depth=0, page=1),
                             TocEntry(title="Page 3 heading", depth=1, page=3),
                             TocEntry(title="Page 4 heading", depth=0, page=4)])
    llm = FakeLLM(plan)
    toc = build_toc(pdf(8, outline=outline), "src", llm=llm, new_id=id_gen())
    assert toc.mode == "llm" and len(llm.calls) == 1
    assert [s.depth for s in toc.sections] == [0, 1, 0]
    assert toc.diagnostics


def test_flat_unnumbered_bookmarks_kept_when_the_llm_cannot_help():
    outline = [("Intro", 0, 1), ("Body", 0, 4), ("More", 0, 6)]
    for llm in (None, FakeLLM(LLMUnavailable("no"))):
        toc = build_toc(pdf(8, outline=outline), "src", llm=llm, new_id=id_gen())
        assert toc.mode == "outline" and [s.title for s in toc.sections] == ["Intro", "Body",
                                                                             "More"]


# ---- PDFs without bookmarks: sections start at a line (materials 3+4, Task 17) ----

PAPER_TOC = [("前面的内容", 0, 1, 1), ("Abstract", 0, 1, 1), ("1 Introduction", 0, 1, 2),
             ("2 Related Work", 0, 2, 2), ("2.1 Retrieval Practice", 1, 2, 2),
             ("2.2 Interleaving", 1, 2, 2), ("3 Methods", 0, 2, 3), ("3.1 Materials", 1, 3, 3),
             ("4 Results", 0, 3, 3), ("5 Discussion", 0, 3, 4), ("6 Conclusion", 0, 4, 4),
             ("References", 0, 4, 4)]


def test_fixture_paper_pdf_headings_inside_pages_no_llm():
    llm = FakeLLM()
    toc = build_toc(fixture_doc("paper.pdf", "pdf"), "src", llm=llm, new_id=id_gen())
    assert toc.mode == "headings" and toc.note is None and llm.calls == []
    secs = toc.sections
    assert [(s.title, s.depth, s.page_from, s.page_to) for s in secs] == PAPER_TOC
    by = {s.title: s for s in secs}
    assert by["2.1 Retrieval Practice"].parent_id == by["2 Related Work"].section_id
    assert by["3.1 Materials"].parent_id == by["3 Methods"].section_id
    assert by["4 Results"].parent_id is None
    # the text is split at the heading lines and keeps the page marks
    assert by["2.1 Retrieval Practice"].text == (
        "〔p.2〕\n2.1 Retrieval Practice\nTesting yourself strengthens memory more than "
        "rereading does.")
    intro = by["1 Introduction"].text
    assert intro.startswith("〔p.1〕\n1 Introduction\n") and "〔p.2〕" in intro
    assert "Earlier work suggests" in intro and "Related Work" not in intro
    assert "2 groups were compared" in intro            # a sentence, not a heading
    methods = by["3 Methods"].text
    assert "〔p.2〕\n3 Methods" in methods and "〔p.3〕" in methods
    assert "Each group met twice" in methods and "3.1 Materials" not in methods
    lead = secs[0].text
    assert "A. Author and B. Author" in lead and "Abstract" not in lead


def test_paper_sections_give_back_each_page_and_chunk_by_page():
    from evowiki.sources.chunk import chunk_section
    from evowiki.sources.excerpts import pages_of
    doc = fixture_doc("paper.pdf", "pdf")
    secs = build_toc(doc, "src", llm=None, new_id=id_gen()).sections
    page2 = pages_of(secs, [2])[2]
    assert page2.split("\n") == [ln for ln in doc.pages[1].text.splitlines() if ln.strip()]
    methods = next(s for s in secs if s.title == "3 Methods")
    assert {c.page for c in chunk_section(methods, new_id=id_gen("c"))} == {2, 3}


def _doc(*pages: str, outline=()) -> ParsedDoc:
    return ParsedDoc("pdf", "d", tuple(PageText(i, t, "text") for i, t in enumerate(pages, 1)),
                     tuple(OutlineEntry(*o) for o in outline), False)


def test_heading_lines_parse_the_layout():
    from evowiki.sources.toc import heading_lines
    header = "Journal of Things 12 (2020)"
    doc = _doc(
        f"{header}\nABSTRACT\nWe study things.\n第3章 绪论\n1.2. Scope of the study\n"
        "IV. Results\n摘 要\nAppendix B",
        f"{header}\n2 groups were compared.\n1 Introduction ........ 3\n"
        "3 This heading line is far too long to be a heading because it runs past eighty chars\n"
        "5 Results:\nReferences",
        f"{header}\nplain text")
    got = [(h.page, h.line, h.text, h.number) for h in heading_lines(doc)]
    assert got == [(1, 1, "ABSTRACT", None), (1, 3, "第3章 绪论", (3,)),
                   (1, 4, "1.2. Scope of the study", (1, 2)), (1, 5, "IV. Results", (4,)),
                   (1, 6, "摘 要", None), (1, 7, "Appendix B", None),
                   (2, 5, "References", None)]


AMBIGUOUS = ("Notes on a Method\nPreface text here.",
             "1 Setup\nfirst we set up.\n3 Results\nthen results.",
             "2 Errata\nsee above.",
             "more text\nand more.")


def test_ambiguous_headings_go_to_the_llm_with_their_lines():
    plan = TocPlan(sections=[TocEntry(title="Notes on a Method", depth=0, page=1, line=1),
                             TocEntry(title="1 Setup", depth=0, page=2, line=1),
                             TocEntry(title="3 Results", depth=0, page=2, line=3),
                             TocEntry(title="bad line", depth=0, page=3, line=99),
                             TocEntry(title="2 Errata", depth=1, page=3, line=1)])
    llm = FakeLLM(plan)
    toc = build_toc(_doc(*AMBIGUOUS), "src", llm=llm, new_id=id_gen())
    ((_, purpose, prompt),) = llm.calls
    assert purpose == "sources.toc"
    assert "第2页：\n  第1行：1 Setup\n  第2行：first we set up.\n  第3行：3 Results" in prompt
    assert '"line": 行号' in prompt and "一页里可以开始好几个章节" in prompt
    assert prompt.index("<<<资料内容开始>>>") < prompt.index("3 Results") \
        < prompt.index("<<<资料内容结束>>>")
    assert toc.mode == "llm"
    assert [(s.title, s.page_from, s.page_to) for s in toc.sections] == [
        ("Notes on a Method", 1, 1), ("1 Setup", 2, 2), ("3 Results", 2, 2),
        ("2 Errata", 3, 4)]
    setup, results = toc.sections[1], toc.sections[2]
    assert setup.text == "〔p.2〕\n1 Setup\nfirst we set up."
    assert results.text == "〔p.2〕\n3 Results\nthen results."


def test_llm_entries_without_a_line_are_found_on_their_page_and_order_is_kept():
    plan = TocPlan(sections=[TocEntry(title="1 Setup", depth=0, page=2),
                             TocEntry(title="3 results", depth=0, page=2),
                             TocEntry(title="back", depth=0, page=2, line=1),
                             TocEntry(title="Errata", depth=0, page=3),     # not the line's text
                             TocEntry(title="2 Errata", depth=0, page=3)])
    toc = build_toc(_doc(*AMBIGUOUS), "src", llm=FakeLLM(plan), new_id=id_gen())
    assert [(s.title, s.page_from) for s in toc.sections] == [
        ("前面的页", 1), ("1 Setup", 2), ("3 results", 2), ("2 Errata", 3)]
    assert toc.sections[2].text.startswith("〔p.2〕\n3 Results")


def test_ambiguous_headings_and_an_unusable_answer_fall_back_to_pages():
    llm = FakeLLM(TocPlan(sections=[TocEntry(title="x", depth=0, page=9, line=1)]))
    toc = build_toc(_doc(*AMBIGUOUS), "src", llm=llm, new_id=id_gen())
    assert toc.mode == "pages" and len(llm.calls) == 1


def test_bookmarks_start_at_the_line_their_title_is_on():
    doc = _doc("Title page\nA\nintro of a\nA.1  details\nbody of a1", "more of a1\nB\nbody b",
               outline=[("A", 0, 1), ("A.1 Details", 1, 1), ("B", 0, 2)])
    toc = build_toc(doc, "src", llm=None, new_id=id_gen())
    assert toc.mode == "outline"
    assert [(s.title, s.page_from, s.page_to) for s in toc.sections] == [
        ("前面的内容", 1, 1), ("A", 1, 1), ("A.1 Details", 1, 2), ("B", 2, 2)]
    lead, a, a1, b = toc.sections
    assert lead.text == "〔p.1〕\nTitle page" and a.text == "〔p.1〕\nA\nintro of a"
    assert a1.text == "〔p.1〕\nA.1  details\nbody of a1\n\n〔p.2〕\nmore of a1"
    assert b.text == "〔p.2〕\nB\nbody b"


# ---- sources-toc@v3 (spec 2026-10-29-sources-verifiable-extraction §P0-2) ----

def _body(n: int, seed: str) -> list[str]:
    """n full-width text lines of a paragraph (the last one short and ending it)."""
    full = [f"{seed} words fill this justified line of the body text so that it runs as wide as "
            f"the page w{i}" for i in range(n - 1)]
    return [*full, f"{seed} the paragraph ends here."]


def _paper() -> ParsedDoc:
    """A 13-page paper laid out like the 2026-10-23 live one (llm_8dc9…, recorded in
    tests/fixtures/sources/recorded/toc_v2_paper.json): an unnumbered title wrapped over two lines,
    unnumbered section titles in the middle of pages 1, 2 and 4, a running footer with the page
    number, Acknowledgements / References late on page 6, boxes and figures at the end."""
    foot = ["Author and Author Page {n}", "Trends Journal. Author manuscript; available in 2025."]
    pages = {
        1: ["Human-Gut Bacterial Protein-Protein Interactions: Understudied",
            "but Impactful to Human Health", "Ann Author1, Ben Author1", "1School, City, USA.",
            "Abstract", *_body(8, "abs"), "Keywords", "Gut microbiome; protein; disease",
            "A paradigm shift for studying human-bacterial interactions", *_body(10, "par")],
        2: [*_body(12, "p2a"),
            "Gut bacterial proteins directly interact with immune system components",
            *_body(12, "imm")],
        3: _body(20, "p3"),
        4: [*_body(2, "p4a"), "Gut bacterial proteins regulate the gut barrier",
            *_body(15, "bar")],
        5: ["Concluding remarks and future perspectives", *_body(18, "con")],
        6: [*_body(18, "p6"), "Acknowledgements", "We thank the funders.", "References",
            *(f"{n}. Author A et al. (2020) A long reference title that runs past the heading "
              f"length. Journal {n}, 2–3" for n in (1, 2))],
        7: [f"{n}. Author A et al. (2012) A long reference title that runs past the heading "
            f"length. Journal {n}, 3–4" for n in (11, 12)],
        8: [f"{n}. Author A et al. (2017) A long reference title that runs past the heading "
            f"length. Journal {n}, 6–7" for n in (33, 34)],
        9: ["Outstanding Questions Box", *_body(4, "oqb")],
        10: ["Highlights", *_body(4, "hl")],
        11: ["Figure 1:", *_body(3, "f1")], 12: ["Figure 2:", *_body(3, "f2")],
        13: ["Figure 3:", *_body(3, "f3")]}
    return ParsedDoc("pdf", "Human-Gut Bacterial Protein-Protein Interactions: Understudied but",
                     tuple(PageText(n, "\n".join([*ls, *(f.format(n=n) for f in foot)]), "text")
                           for n, ls in pages.items()), (), False)


def _recorded(name: str) -> dict:
    import json
    from pathlib import Path
    path = Path(__file__).parents[1] / "fixtures" / "sources" / "recorded" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_candidate_lines_find_unnumbered_titles_in_the_middle_of_pages():
    from evowiki.sources.toc import candidate_lines, heading_lines
    doc = _paper()
    shown = candidate_lines(doc, heading_lines(doc))
    text = {p.page: p.text.splitlines() for p in doc.pages}
    picked = {(n, text[n][i]) for n, ls in shown.items() for i in ls}
    beyond_top = {(n, text[n][i]) for n, ls in shown.items() for i in ls if i >= 3}
    for page, title in ((1, "Abstract"), (1, "Keywords"),
                        (1, "A paradigm shift for studying human-bacterial interactions"),
                        (2, "Gut bacterial proteins directly interact with immune system components"),
                        (4, "Gut bacterial proteins regulate the gut barrier"),
                        (6, "Acknowledgements"), (6, "References")):
        assert (page, title) in picked
    assert not any(t.startswith(("Author and Author", "Trends")) for _, t in beyond_top)
    assert not any(t.endswith("ends here.") for _, t in beyond_top)   # a paragraph's last line
    prompt = FakeLLM(TocPlan(sections=[]))
    build_toc(doc, "src", llm=prompt, new_id=id_gen())
    sent = prompt.calls[0][2]
    assert "第1页：" in sent and "第16行：A paradigm shift" in sent
    assert "只能从上面给出的行里选" in sent and "不要补没给出的章节" in sent
    assert "不做后面章节的父章节" in sent and "资料自己的标题" in sent


def test_recorded_v2_paper_reply_is_checked_and_its_depths_fixed():
    """The live sources-toc@v2 reply on the paper: the document title as a section at depth 0,
    Abstract at depth 0 and every later section at depth 1 (so under Abstract). Checked now: the
    title goes into the leading part, and every section is a top level, none under Abstract."""
    rec = _recorded("toc_v2_paper")
    toc = build_toc(_paper(), "src", llm=FakeLLM(TocPlan.model_validate(rec["reply"])),
                    new_id=id_gen())
    assert toc.mode == "llm"
    assert [(s.title, s.depth, s.page_from) for s in toc.sections] == [
        ("前面的内容", 0, 1), ("Abstract", 0, 1),
        ("Gut bacterial proteins regulate the gut barrier", 0, 4),
        ("Concluding remarks and future perspectives", 0, 5), ("Acknowledgements", 0, 6),
        ("References", 0, 6), ("Outstanding Questions Box", 0, 9), ("Highlights", 0, 10)]
    assert all(s.parent_id is None for s in toc.sections)
    assert toc.sections[0].text.startswith("〔p.1〕\nHuman-Gut Bacterial")


def test_a_title_that_is_not_its_line_moves_to_the_candidate_line_holding_it_or_is_dropped():
    plan = TocPlan(sections=[
        TocEntry(title="Abstract", depth=0, page=1, line=5),
        # the right title with a wrong line: found on its candidate line
        TocEntry(title="A paradigm shift for studying human-bacterial interactions", depth=0,
                 page=1, line=3),
        # the right line with a made-up title: dropped
        TocEntry(title="Immune interactions", depth=0, page=2, line=13),
        # a body line's text (not a candidate line): dropped
        TocEntry(title="p3 the paragraph ends here.", depth=0, page=3, line=20),
        TocEntry(title="Gut bacterial proteins regulate the gut barrier", depth=0, page=4)])
    toc = build_toc(_paper(), "src", llm=FakeLLM(plan), new_id=id_gen())
    assert [(s.title, s.page_from) for s in toc.sections] == [
        ("前面的内容", 1), ("Abstract", 1),
        ("A paradigm shift for studying human-bacterial interactions", 1),
        ("Gut bacterial proteins regulate the gut barrier", 4)]


def test_a_wrapped_title_matches_its_lines():
    doc = _paper()
    title = "Human-Gut Bacterial Protein-Protein Interactions: Understudied but Impactful to " \
            "Human Health"
    plan = TocPlan(sections=[TocEntry(title="Cover", depth=0, page=1, line=1),
                             TocEntry(title=title, depth=0, page=1, line=1),
                             TocEntry(title="Abstract", depth=1, page=1, line=5),
                             TocEntry(title="Keywords", depth=1, page=1, line=14)])
    toc = build_toc(doc, "src", llm=FakeLLM(plan), new_id=id_gen())
    # 「Cover」 is not the line; the wrapped title is the document's own title: dropped too
    assert [(s.title, s.depth) for s in toc.sections] == [
        ("前面的内容", 0), ("Abstract", 0), ("Keywords", 0)]


@pytest.mark.parametrize(("entries", "depths"), [
    ([("Abstract", 0), ("1 Intro", 1), ("1.1 Aim", 2)], [0, 0, 1]),     # no parent: lifted
    ([("Intro", 0), ("Aim", 2), ("Scope", 2), ("Methods", 0)], [0, 1, 1, 0]),   # skipped level
    ([("Intro", 0), ("References", 1), ("Appendix", 1)], [0, 0, 0]),  # nothing under References
    ([("Ch 1", 1), ("Sec", 2), ("Ch 2", 1), ("Sec", 3), ("Sub", 4)], [0, 1, 0, 1, 2]),
    ([("参考文献", 2), ("附录 A", 0)], [0, 0])])
def test_fix_depths(entries, depths):
    from evowiki.sources.toc import fix_depths
    assert fix_depths(entries) == depths
