from evowiki.sources.headers import strip_running_lines
from evowiki.sources.parsed import PageText

HEADER = "线性代数讲义 · 第 3 章"


def _lines(n: int, body: str) -> list[str]:
    # edge lines differ per page even after digit normalisation, so only header/footer repeat
    tag = chr(0x4E00 + n)
    return [f"{tag}首句", f"{tag}次句", body, f"{tag}倒数句", f"{tag}末句"]


def _page(n: int, body: str, header: str | None = HEADER, footer: str | None = None) -> PageText:
    lines = _lines(n, body)
    parts = ([header] if header else []) + lines + ([footer] if footer else [])
    return PageText(page=n, text="\n".join(parts), read_by="text")


def _body(n: int, body: str) -> str:
    return chr(10).join(_lines(n, body))


def test_ten_pages_header_and_page_numbers_removed():
    pages = [_page(i, f"正文{i}", footer=f"- {i} -") for i in range(1, 11)]
    out = strip_running_lines(pages)
    for i, p in enumerate(out, 1):
        assert p.text == _body(i, f"正文{i}")
        assert p.page == i


def test_page_number_styles_removed_without_repeat():
    footers = ["12", "第 13 页", "14 / 40", "Page 15", "xvi"]
    pages = [_page(i, f"内容{i}", header=None, footer=f) for i, f in enumerate(footers, 1)]
    out = strip_running_lines(pages)
    assert [p.text for p in out] == [_body(i, f"内容{i}") for i in range(1, 6)]


def test_line_repeated_on_only_two_pages_is_kept():
    pages = [_page(i, f"正文{i}", header=None) for i in range(1, 9)]
    pages[0] = _page(1, "正文1", header="巧合的开头")
    pages[4] = _page(5, "正文5", header="巧合的开头")
    out = strip_running_lines(pages)
    assert out[0].text.startswith("巧合的开头")
    assert out[4].text.startswith("巧合的开头")


def test_three_pages_untouched():
    pages = [_page(i, f"正文{i}", footer=str(i)) for i in range(1, 4)]
    assert strip_running_lines(pages) == tuple(pages)


def test_unchanged_pages_are_same_objects():
    pages = [_page(i, f"正文{i}", header=None) for i in range(1, 6)]
    out = strip_running_lines(pages)
    assert all(a is b for a, b in zip(out, pages, strict=True))
