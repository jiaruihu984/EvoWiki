"""Row types for materials (migration 0059). Named *Row to stay clear of the existing
`Source` classes elsewhere in the code base."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

SourceKind = Literal["pdf", "pptx", "docx", "xlsx", "image", "md", "text", "url"]
SourceStatus = Literal["reading", "ready", "partial", "failed"]
ReadBy = Literal["text", "vision", "none"]
Use = Literal["learn", "understand", "ask", "supplement", "exam"]
LinkRole = Literal["teaches", "exercises", "reads"]
ItemKind = Literal["single", "multi", "tf", "open", "group"]
ItemStatus = Literal["ok", "incomplete", "unreadable"]


@dataclass(frozen=True)
class SourceRow:
    source_id: str
    user_id: str
    kind: SourceKind
    title: str
    sha256: str
    bytes: int
    pages: int | None
    status: SourceStatus
    error: str | None
    unreadable_pages: tuple[int, ...]
    toc_mode: str | None              # outline | headings | llm | pages
    progress: dict                    # latest progress record, shown directly in the list
    origin: str | None                # url, or the in-vault path given by the plugin
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    page_offset: int | None = None    # PDF page − printed page (0060); None: show PDF pages
    # [{"term", "means", "page", "section_id"}] extracted at import (0060; section_id since 0101)
    glossary: tuple[dict, ...] = ()
    run_gen: int = 0                  # import run generation (0062)
    # which text the glossary is from (0101): {"whole", "head_chars", "sent_chars",
    # "total_chars", "cut_sections"}; None: imported before 0101 or no glossary step
    glossary_coverage: dict | None = None
    # the sections are a PDF's text layer, no layout parse went into them (0105); a learning use
    # parses it and builds the outline again (`sources.importer`)
    text_first: bool = False
    # text-first pages whose formulas, tables or pictures the text layer does not hold reliably
    imprecise_pages: tuple[int, ...] = ()
    image_pages: tuple[int, ...] = ()
    toc_edited: bool = False          # the user changed the outline: no rebuild drops that (0105)
    # where the title came from (0109): given | document (PDF metadata, arXiv) | fallback (file
    # name, url); only a fallback gives way to the title the layout parse reads
    title_source: str = "given"
    earlier_titles: tuple[str, ...] = ()   # names it went by: an old name still finds it (cite)


@dataclass(frozen=True)
class SectionRow:
    section_id: str
    source_id: str
    parent_id: str | None
    ord: int
    depth: int
    title: str
    page_from: int | None
    page_to: int | None
    text: str
    tokens: int
    summary: str | None
    read_by: ReadBy


@dataclass(frozen=True)
class ChunkRow:
    chunk_id: str
    section_id: str
    source_id: str
    ord: int
    page: int | None
    text: str


@dataclass(frozen=True)
class LinkRow:
    """Which wiki node came from which section of which material (table source_link)."""
    node_id: str
    section_id: str
    source_id: str
    role: LinkRole
    # who wrote it: the older intake / a reading block, the curator, the user, S1's blocks, the
    # one-level split of an entered block (stage 3, migration 0086), a reading unit's teaches
    # (reading-mode spec §2.3, migration 0088)
    by: Literal["intake", "curator", "user", "blocks", "enter", "reading"]


@dataclass(frozen=True)
class PageRead:
    """One page as read by one reader (migration 0062); text None = read but nothing came out."""
    page: int
    text: str | None
    by: Literal["local", "claude"]
    figure: bool


@dataclass(frozen=True)
class ExamItem:
    item_id: str
    source_id: str
    section_id: str | None
    page: int | None            # PDF page
    ord: int                    # order within the source (0..)
    number: str | None          # printed question number, verbatim
    parent_id: str | None       # item_id of the owning group
    kind: ItemKind
    stem: str
    options: tuple[str, ...]
    answer: str | None          # single "B"; multi "AC"; tf "对"/"错"; open: original answer text
    explanation: str | None
    has_key: bool
    status: ItemStatus
    problem: str | None         # diagnostic code (answer_not_in_text / parent_missing / model reason)
    node_id: str | None = None
    # where the stem sits in the stored document (`sources.locate`, 0103); None: no document,
    # or the stem not found there (the item is still checked against the section text)
    locator: dict | None = None
    cell: str | None = None
    attach: Literal["pending", "attached", "unattached"] = "pending"
    attach_note: str | None = None   # why it was not attached (short Chinese, shown in the ? hover)
