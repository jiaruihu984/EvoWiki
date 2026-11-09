from evowiki.sources.parsed import PageText
from evowiki.sources.printed import find_page_offset, printed_page


def _page(n: int, head: str = "", foot: str = "", body: str = "正文一行\n正文二行\n正文三行") -> PageText:
    return PageText(n, "\n".join(x for x in (head, body, foot) if x), "text")


def test_footer_numbers_give_the_offset():
    # PDF pages 1-4 are front matter; printed page 1 is PDF page 5
    pages = [_page(n) for n in range(1, 5)] + [_page(n, foot=str(n - 4)) for n in range(5, 12)]
    assert find_page_offset(pages) == 4


def test_number_forms_and_header_position():
    pages = [_page(3, head="- 1 -"), _page(4, foot="第 2 页"), _page(5, head="3"),
             _page(6, foot="  4  ")]
    assert find_page_offset(pages) == 2


def test_numbers_only_from_the_first_two_and_last_two_lines():
    body = "a\nb\n7\nc\nd"                  # "7" is in the middle: not a candidate
    pages = [_page(n, body=body) for n in range(1, 8)]
    assert find_page_offset(pages) is None


def test_fewer_than_three_agreeing_pages_is_none():
    pages = [_page(5, foot="1"), _page(6, foot="2")] + [_page(n) for n in range(7, 20)]
    assert find_page_offset(pages) is None


def test_the_offset_must_be_the_majority_of_pages_with_candidates():
    agree = [_page(n, foot=str(n - 2)) for n in range(3, 6)]        # 3 pages: offset 2
    noise = [_page(n, foot=str(n * 3)) for n in range(6, 10)]       # 4 pages, all different
    assert find_page_offset(agree + noise) is None
    assert find_page_offset(agree + noise[:2]) == 2                 # 3 of 5: majority


def test_a_page_with_two_numbers_counts_once_per_offset():
    pages = [_page(n, head="2026", foot=str(n + 10)) for n in range(1, 6)]
    assert find_page_offset(pages) == -10      # printed pages may run ahead of the PDF


def test_unreadable_and_empty_pages_are_skipped():
    pages = [PageText(1, "", "none", "no_text")] + [_page(n, foot=str(n)) for n in range(2, 6)]
    assert find_page_offset(pages) == 0
    assert find_page_offset([]) is None


def test_printed_page():
    assert printed_page(10, 4) == 6
    assert printed_page(3, 4) is None          # roman-numbered front matter: show the PDF page
    assert printed_page(4, 4) is None
    assert printed_page(10, None) is None
    assert printed_page(1, -100) == 101
