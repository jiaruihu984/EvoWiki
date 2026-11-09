"""Reading pages that have no text layer from their images (design §5 step 3). `NoVision` reads
nothing (those pages stay read_by="none" and the source `partial`); `sources.layout_client.LayoutWorker`
reads them with the local layout worker (MinerU or Docling) and says which pages need a model that
can see figures."""

import re
from dataclasses import dataclass
from typing import Literal, Protocol

# A page needs a model that looks at the image (Q4) when the layout has a chart / image / figure
# block covering at least FIGURE_MIN_AREA of the page, or the OCR text is shorter than
# MIN_TEXT_CHARS non-space characters.
FIGURE_LABELS = frozenset({"chart", "image", "figure"})
FIGURE_MIN_AREA = 0.15
MIN_TEXT_CHARS = 20

# Markup an OCR / layout model puts in its Markdown for figure blocks (`<div><img src=...></div>`,
# `![](...)`): not text the page says.
# Only the HTML tags it emits, so formula text like `$x<y$ 且 $y>z$` still counts.
_MARKUP = re.compile(
    r"</?(?:div|img|p|span|br|center|table|thead|tbody|tr|td|th)\b[^<>]*>"
    r"|!\[[^\]]*\]\([^)]*\)", re.IGNORECASE)


@dataclass(frozen=True)
class PageImageRead:
    text: str | None       # None: the page could not be read
    figure: bool           # a chart / geometry figure: needs a model that can look at images
    reason: str | None = None   # why text is None, for the log: "repeat", "too_long",
    #                             "error:<msg>", "timeout", "crashed", "unavailable", "no_reader"…
    thin_only: bool = False     # `figure` only because little text was read: no figure block


class VisionReader(Protocol):
    name: Literal["local", "claude", "none"]

    def available(self) -> bool: ...

    def read(self, png_pages: list[bytes]) -> list[PageImageRead]:
        """One result per page image, in order; text None for a page it could not read."""
        ...


class NoVision:
    """No reader: every page stays unread."""

    name: Literal["none"] = "none"

    def available(self) -> bool:
        return False

    def read(self, png_pages: list[bytes]) -> list[PageImageRead]:
        return [PageImageRead(None, False, "no_reader") for _ in png_pages]


def strip_markup(markdown: str) -> str:
    """OCR Markdown without the reader's figure markup (tags only; the text stays)."""
    return _MARKUP.sub("", markdown or "")


def has_figure_block(blocks: list[dict]) -> bool:
    """A large figure block: `blocks` items are {"label", "area"} with area the block's share of
    the page (0..1)."""
    return any(b.get("label") in FIGURE_LABELS and float(b.get("area") or 0) >= FIGURE_MIN_AREA
               for b in blocks)


def needs_eyes(markdown: str, blocks: list[dict]) -> bool:
    """Q4: a large figure block, or too little text read."""
    if has_figure_block(blocks):
        return True
    text = _MARKUP.sub("", markdown or "")
    return sum(1 for ch in text if not ch.isspace()) < MIN_TEXT_CHARS
