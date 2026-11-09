"""Section text -> retrieval chunks (~800 chars), each tagged with its page."""

import re
from collections.abc import Callable

from evowiki.sources.config import PAGE_MARK_RE
from evowiki.sources.models import ChunkRow, SectionRow

_SENTENCES = re.compile(r".*?(?:[。！？!?]+|\.(?=\s|$))\s*|.+$", re.DOTALL)
_BLANK = re.compile(r"\n\s*\n")


def _split_long(para: str, target: int) -> list[str]:
    """Pack sentences up to `target`; a single sentence longer than that stays whole."""
    pieces = [m.group(0) for m in _SENTENCES.finditer(para) if m.group(0)]
    out: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) > target:
            out.append(cur)
            cur = ""
        cur += p
    if cur:
        out.append(cur)
    return out


def _paragraphs(text: str, first_page: int | None) -> list[tuple[int | None, str]]:
    parts = PAGE_MARK_RE.split(text)       # [text0, page, text1, page, text2, ...]
    page = first_page
    out: list[tuple[int | None, str]] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            page = int(part)
            continue
        for para in _BLANK.split(part):
            if para.strip():
                out.append((page, para.strip()))
    return out


def chunk_section(sec: SectionRow, *, new_id: Callable[[], str],
                  target_chars: int = 800) -> list[ChunkRow]:
    """Split on blank lines; split over-long paragraphs at sentence ends; merge short ones up
    to about `target_chars`. A chunk never spans pages; its page is the nearest `〔p.N〕`
    before it (page markers are removed from the text)."""
    units: list[tuple[int | None, str]] = []
    for page, para in _paragraphs(sec.text, sec.page_from):
        if len(para) > target_chars:
            units.extend((page, s.strip()) for s in _split_long(para, target_chars) if s.strip())
        else:
            units.append((page, para))
    chunks: list[ChunkRow] = []
    cur_page: int | None = None
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur:
            chunks.append(ChunkRow(chunk_id=new_id(), section_id=sec.section_id,
                                   source_id=sec.source_id, ord=len(chunks),
                                   page=cur_page, text=cur))
        cur = ""

    for page, text in units:
        if cur and (page != cur_page or len(cur) + len(text) + 2 > target_chars):
            flush()
        if not cur:
            cur_page = page
            cur = text
        else:
            cur += "\n\n" + text
    flush()
    return chunks
