"""Pure unit tests for the unreadable-page judge."""

from evowiki.sources.parsed import PageText
from evowiki.sources.unreadable import MAX_GARBLE_RATIO, MIN_CHARS_PER_PAGE, judge, judge_all

ZH_PARAGRAPH = (
    "间隔重复是一种把复习安排在遗忘之前的方法。每次复习之后，下一次复习的间隔会变长，"
    "这样同样的时间可以记住更多的内容。它常常和提取练习一起使用。"
)


def _page(text: str, page: int = 1) -> PageText:
    return PageText(page=page, text=text, read_by="text")


def test_initial_thresholds() -> None:
    assert MIN_CHARS_PER_PAGE == 40
    assert MAX_GARBLE_RATIO == 0.3


def test_a_short_page_without_a_picture_is_read_all_of_it() -> None:
    short = "Practice\n14\nCoding practice with NetworkX"
    assert judge(_page(short)).read_by == "text"
    assert judge(PageText(page=1, text=short, read_by="text", image_area=0.2)).read_by == "none"
    assert judge(_page("14")).read_by == "none"


def test_replacement_chars_are_garbled() -> None:
    out = judge(_page("\ufffd" * 200))
    assert (out.read_by, out.reason) == ("none", "garbled")


def test_private_use_and_control_chars_are_garbled() -> None:
    text = ("\ue001\ue002\x07\x01" * 20) + "正常文字" * 5
    out = judge(_page(text))
    assert (out.read_by, out.reason) == ("none", "garbled")


def test_single_letter_fragments_are_sparse() -> None:
    text = " ".join("abcdefghijklmnopqrstuvwxyz" * 3)
    out = judge(_page(text))
    assert (out.read_by, out.reason) == ("none", "sparse")


def test_cid_fragments_are_sparse() -> None:
    text = " ".join(f"(cid:{n})" for n in range(30))
    out = judge(_page(text))
    assert (out.read_by, out.reason) == ("none", "sparse")


def test_chinese_paragraph_is_text() -> None:
    out = judge(_page(ZH_PARAGRAPH))
    assert (out.read_by, out.reason) == ("text", None)


def test_english_paragraph_is_text() -> None:
    text = (
        "Retrieval practice means pulling a fact out of memory instead of re-reading it. "
        "A small quiz after each section is a simple way to do it."
    )
    out = judge(_page(text))
    assert (out.read_by, out.reason) == ("text", None)


def test_empty_page_is_no_text() -> None:
    out = judge(_page("  \r\n  "))
    assert (out.read_by, out.reason) == ("none", "no_text")


def test_judge_keeps_page_and_text() -> None:
    page = _page(ZH_PARAGRAPH, page=7)
    out = judge(page)
    assert out.page == 7 and out.text == ZH_PARAGRAPH


def test_judge_all_preserves_order() -> None:
    pages = [_page(ZH_PARAGRAPH, 1), _page("", 2), _page(ZH_PARAGRAPH, 3)]
    out = judge_all(pages)
    assert isinstance(out, tuple)
    assert [(p.page, p.read_by) for p in out] == [(1, "text"), (2, "none"), (3, "text")]
