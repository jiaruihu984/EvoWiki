import re
from html.parser import HTMLParser

_DROP = {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form",
         "button", "blockquote"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK = _HEADINGS | {"p", "div", "li", "ul", "ol", "pre", "table", "tr", "section", "article",
                      "br", "dd", "dt", "figcaption", "body"}
_CELLS = {"td", "th"}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._cells_in_row = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP:
            self._skip += 1
        elif not self._skip and tag in _CELLS:
            if self._cells_in_row:
                self.parts.append(" | ")
            self._cells_in_row += 1
        elif not self._skip and tag in _BLOCK:
            self.parts.append("\n\n")
            if tag == "tr":
                self._cells_in_row = 0
            if tag in _HEADINGS:
                self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP:
            self._skip = max(0, self._skip - 1)
        elif not self._skip and tag in _BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    paragraphs = re.split(r"\n\s*\n", "".join(parser.parts))
    cleaned = [" ".join(p.split()) for p in paragraphs]
    return "\n\n".join(p for p in cleaned if p and p.strip("# "))
