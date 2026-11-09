"""Parser output types shared by the PDF / Office / web parsers and the section builder."""

from dataclasses import dataclass

from evowiki.sources.models import ReadBy, SourceKind


@dataclass(frozen=True)
class PageText:
    page: int                       # 1-based
    text: str
    read_by: ReadBy                 # "text" first; "none" once judged unreadable
    reason: str | None = None       # "no_text" | "garbled" | "sparse"
    # Set by the vision step only: OCR says this page needs a model that sees pictures.
    figure: bool = False
    # Every judge signal that fired (unreadable.judge_page), e.g. ("rare_hanzi",
    # "image_page"). On a read_by="text" page, "formula_or_table" / "image_page" mean
    # "try OCR, keep this text layer if OCR fails" (unreadable.wants_ocr), as does
    # "no_unicode_map".
    signals: tuple[str, ...] = ()
    # Filled by parse_pdf for the judge: a font on the page has characters pdfium cannot map
    # to Unicode (no ToUnicode), and the share of the page covered by image objects.
    fonts_without_tounicode: bool = False
    image_area: float = 0.0
    drawn_marks: int = 0     # small vector shapes: a fraction bar, a radical, a formula as outlines

    def __post_init__(self) -> None:
        # Step values come back from JSON with lists; keep the dataclass hashable and equal.
        if not isinstance(self.signals, tuple):
            object.__setattr__(self, "signals", tuple(self.signals))


@dataclass(frozen=True)
class OutlineEntry:
    title: str
    depth: int
    page: int | None                # Office and md headings have no page number


@dataclass(frozen=True)
class LayoutHead:
    """A heading block of a layout parse, as a line of its page's text (`line`: 0-based index in
    `PageText.text.splitlines()`); `level` as the backend gave it (often all the same)."""
    page: int
    line: int
    title: str
    level: int | None


@dataclass(frozen=True)
class ParsedDoc:
    kind: SourceKind
    title: str
    pages: tuple[PageText, ...]     # pdf: one per page; pptx: one per slide; md/txt/web: one
    outline: tuple[OutlineEntry, ...]
    headings_inline: bool           # md / docx: headings live in the text ("# title")
    # Headings found by a layout parser (none for the text-layer parsers).
    layout_heads: tuple[LayoutHead, ...] = ()

    def __post_init__(self) -> None:
        # Step values come back from JSON with lists of dicts; keep the dataclass hashable and equal.
        if not isinstance(self.layout_heads, tuple) or any(
                not isinstance(h, LayoutHead) for h in self.layout_heads):
            object.__setattr__(self, "layout_heads", tuple(
                h if isinstance(h, LayoutHead) else LayoutHead(**h) for h in self.layout_heads))


class ParseError(Exception):
    """Encrypted, corrupt or unsupported file; the message is user-facing Chinese."""
