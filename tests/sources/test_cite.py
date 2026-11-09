"""Program-verified citations (materials Task 6b): `sources.cite.verify` is pure; the page text
comes from a dict."""

import pytest

from evowiki.sources.cite import (
    MIN_QUOTE_CHARS,
    QUOTE_MATCH,
    cited_pages,
    normalize,
    quote_in,
    stored_marks,
    verify,
)
from evowiki.wikiapp.events import PageSource

BOOK = PageSource(source_id="s1", title="线性代数讲义")
PAPER = PageSource(source_id="s2", title="注意力论文", page_offset=10)
PAGES = {("s1", 2): "两个向量相加，就是把对应的分量分别相加。",
         ("s1", 3): "数乘把每个分量都乘上同一个数",
         ("s1", 4): "，结果仍是一个向量。",
         ("s2", 12): "注意力权重在大多数情况下近似为稀疏分布。",
         ("s2", 5): "摘要：我们提出一种新的注意力机制。"}


def page_text(source_id: str, page: int) -> str:
    return PAGES.get((source_id, page), "")


def check(body: str, sources=(BOOK, PAPER), mapped: str = ""):
    """The checked text and what the check did not keep (passed citations: `test_a_passed_...`)."""
    out, issues = verify(body, list(sources), page_text, mapped)
    return out, [i for i in issues if i.kind != "verified"]


def test_a_found_quote_becomes_the_stored_form():
    out, issues = check("加法按分量算〔线性代数讲义 p.2：「就是把对应的分量分别相加」〕。")
    assert out == "加法按分量算〔线性代数讲义 p.2｜2〕。" and issues == []


def test_a_close_quote_still_matches_but_a_wrong_one_is_dropped():
    assert QUOTE_MATCH == 0.85
    out, issues = check("〔线性代数讲义 p.2：「就是把对应分量分别相加」〕")      # one character missing
    assert out == "〔线性代数讲义 p.2｜2〕" and issues == []
    out, issues = check("向量相加 〔线性代数讲义 p.2：「向量的长度等于分量平方和的平方根」〕。")
    assert out == "向量相加。"
    assert [(i.kind, i.source_id, i.page, i.quote) for i in issues] == [
        ("quote_not_found", "s1", 2, "向量的长度等于分量平方和的平方根")]


def test_a_quote_on_another_page_is_not_moved_there():
    out, issues = check("〔线性代数讲义 p.2：「数乘把每个分量都乘上同一个数」〕")
    assert out == "" and issues[0].kind == "quote_not_found"     # it is on p.3, cited as p.2


def test_a_sentence_running_over_the_page_break_is_found_on_n_and_n_plus_1():
    out, issues = check("〔线性代数讲义 p.3：「都乘上同一个数，结果仍是一个向量」〕")
    assert out == "〔线性代数讲义 p.3｜3〕" and issues == []


def test_the_printed_page_comes_from_the_offset():
    out, _ = check("〔注意力论文 p.12：「在大多数情况下近似为稀疏分布」〕")
    assert out == "〔注意力论文 p.2｜12〕"


def test_front_matter_before_printed_page_1_shows_the_pdf_page():
    out, issues = check("〔注意力论文 p.5：「我们提出一种新的注意力机制」〕")
    assert out == "〔注意力论文 p.5｜5〕" and issues == []


def test_the_old_form_without_a_quote_is_dropped():
    out, issues = check("按分量相加〔线性代数讲义 p.2〕，范围也一样〔线性代数讲义 p.2–3〕。")
    assert out == "按分量相加，范围也一样。"
    assert [i.kind for i in issues] == ["no_quote", "no_quote"]
    assert issues[0].mark == "〔线性代数讲义 p.2〕"


def test_a_differs_mark_whose_quote_is_in_the_mapped_sections_keeps_only_the_note():
    mapped = PAGES[("s2", 12)]
    out, issues = check("⟦和资料不同：通行的说法是稠密分布，因为……；资料原话：「近似为稀疏分布」⟧",
                        mapped=mapped)
    assert out == "⟦和资料不同：通行的说法是稠密分布，因为……⟧"
    assert [(i.kind, i.quote) for i in issues] == [("differs_verified", "近似为稀疏分布")]


def test_a_differs_mark_whose_quote_is_not_found_becomes_to_be_checked():
    out, issues = check("⟦和资料不同：通行的说法是 A；资料原话：「资料里没有这句」⟧",
                        mapped=PAGES[("s2", 12)])
    assert out == "⟦待查证：通行的说法是 A⟧"
    assert [(i.kind, i.note) for i in issues] == [("differs_unverified", "通行的说法是 A")]
    out, issues = check("⟦和资料不同：没给原话⟧", mapped=PAGES[("s2", 12)])
    assert out == "⟦待查证：没给原话⟧" and issues[0].kind == "differs_unverified"


def test_a_name_that_is_no_material_of_the_page_is_dropped():
    out, issues = check("见〔别的书 p.2：「就是把对应的分量分别相加」〕。")
    assert out == "见。" and issues[0].kind == "unknown_source" and issues[0].source_id is None


def test_several_citations_in_one_section_are_each_checked():
    body = ("相加〔线性代数讲义 p.2：「把对应的分量分别相加」〕；数乘〔线性代数讲义 p.3：「乘上同一个数」〕；"
            "稀疏〔注意力论文 p.12：「完全不在这页」〕。")
    out, issues = check(body)
    assert out == "相加〔线性代数讲义 p.2｜2〕；数乘〔线性代数讲义 p.3｜3〕；稀疏。"
    assert [i.kind for i in issues] == ["quote_not_found"]


def test_other_marks_pass_through_untouched():
    body = "已核对的〔线性代数讲义 p.2｜2〕，你补充的〔你补充的〕，页码标记〔p.4〕，⟦待查证：X⟧。"
    out, issues = verify(body, [BOOK, PAPER], page_text, kept=stored_marks(body))
    assert out == body and issues == []


def test_a_forged_stored_mark_is_removed():
    body = "长度公式〔线性代数讲义 p.37｜37〕。"
    out, issues = check(body)                                  # nothing kept: a fresh write
    assert out == "长度公式。"
    assert [(i.kind, i.source_id, i.page, i.mark) for i in issues] == [
        ("unverified_stored", "s1", 37, "〔线性代数讲义 p.37｜37〕")]
    # kept only when exactly that mark was in the section's stored text
    out, issues = verify(body, [BOOK], page_text, kept={"〔线性代数讲义 p.36｜36〕"})
    assert out == "长度公式。" and issues[0].kind == "unverified_stored"


def test_a_quote_shorter_than_the_minimum_verifies_nothing():
    assert MIN_QUOTE_CHARS == 6
    out, issues = check("〔线性代数讲义 p.2：「向量相加」〕")             # 4 characters, on the page
    assert out == "" and issues[0].kind == "quote_not_found"
    out, _ = check("〔线性代数讲义 p.2：「两个向量相加」〕")                # 6 characters
    assert out == "〔线性代数讲义 p.2｜2〕"
    out, issues = check("⟦和资料不同：说明；资料原话：「稀疏」⟧", mapped=PAGES[("s2", 12)])
    assert out == "⟦待查证：说明⟧" and issues[0].kind == "differs_unverified"


def test_a_title_written_with_book_marks_still_matches():
    out, _ = check("〔《线性代数讲义》 p.2：「就是把对应的分量分别相加」〕")
    assert out == "〔线性代数讲义 p.2｜2〕"


def test_page_text_errors_propagate():
    def broken(source_id, page):
        raise RuntimeError("db down")
    with pytest.raises(RuntimeError, match="db down"):
        verify("〔线性代数讲义 p.2：「相加」〕", [BOOK], broken)


def test_normalize_and_cited_pages():
    assert normalize("A, b 〔p.3〕。\n「C」") == "abc"
    assert not quote_in("", "任何文字")
    assert cited_pages("〔线性代数讲义 p.2：「x」〕〔线性代数讲义 p.7〕〔别的 p.1：「y」〕", [BOOK]) == {
        "s1": {2, 3}}


def test_a_passed_citation_is_recorded_with_its_quote_under_its_stored_mark():
    _, issues = verify("〔注意力论文 p.5：「我们提出一种新的注意力机制」〕", [BOOK, PAPER], page_text)
    (i,) = issues
    assert (i.kind, i.mark, i.source_id, i.page, i.quote, i.locator) == (
        "verified", "〔注意力论文 p.5｜5〕", "s2", 5, "我们提出一种新的注意力机制", None)


def test_a_fuzzy_match_never_certifies_a_changed_number():
    page = "在显著性水平为0.05时，我们拒绝原假设，并认为两组均值存在显著差异，样本量为120。"
    assert quote_in("在显著性水平为0.05时，我们拒绝原假设，并认为两组均值存在显著差异，样本量为120", page)
    assert quote_in("在显著性水平为0.05时，我们拒绝原假设并认为两组均值存在显著差，样本量为120", page)  # noise
    assert not quote_in("在显著性水平为0.01时，我们拒绝原假设，并认为两组均值存在显著差异，样本量为120", page)
    assert not quote_in("我们拒绝原假设，并认为两组均值存在显著差异，样本量为320", page)
