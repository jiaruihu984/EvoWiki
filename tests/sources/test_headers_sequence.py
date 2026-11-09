"""A page-number-looking edge line is stripped only when it behaves like a page number."""

from evowiki.sources.headers import strip_running_lines
from evowiki.sources.parsed import PageText


def _page(n: int, top: str | None = None, bottom: str | None = None) -> PageText:
    tag = chr(0x4E00 + n)
    lines = [f"{tag}首句", f"{tag}次句", f"正文{n}", f"{tag}倒数句", f"{tag}末句"]
    parts = ([top] if top else []) + lines + ([bottom] if bottom else [])
    return PageText(page=n, text="\n".join(parts), read_by="text")


def _bare(p: PageText) -> str:
    return _page(p.page).text


def test_lone_chapter_number_on_one_page_is_kept():
    pages = [_page(i, bottom=str(i + 10)) for i in range(1, 9)]
    pages[3] = _page(4, top="3")
    out = strip_running_lines(pages)
    assert out[3].text.split("\n")[0] == "3"
    assert out[0].text == _bare(pages[0])


def test_page_numbers_with_gaps_are_stripped():
    nums = [12, 13, 15, 16, 17, 19]
    pages = [_page(i, bottom=str(n)) for i, n in enumerate(nums, 1)]
    assert [p.text for p in strip_running_lines(pages)] == [_bare(p) for p in pages]


def test_words_and_off_sequence_numerals_are_kept():
    pages = [_page(i, bottom=f) for i, f in enumerate(["Mix", "CV", "IV", "MD", "DC", "13"], 1)]
    assert [p.text for p in strip_running_lines(pages)] == [p.text for p in pages]


def test_roman_front_matter_numbers_are_stripped():
    pages = [_page(i, bottom=f) for i, f in enumerate(["i", "ii", "iii", "iv", "v"], 1)]
    assert [p.text for p in strip_running_lines(pages)] == [_bare(p) for p in pages]
