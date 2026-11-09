"""A material's title: the title the user gave; else, for a PDF, its own title (metadata Title, else
a first top-level bookmark that looks like a title: `parse_pdf.pdf_title`, follow-up B
2026-10-22); else an arXiv url's title from the API (`sources.arxiv`); else the first heading the
layout parse read (`importer`); else the uploaded file's name or the url, where a PDF's metadata
Title still stands in when that name is generic (empty, a hash, 「untitled」, 「document」 …). Every
title is cleaned (control characters out, whitespace collapsed, at most TITLE_MAX characters),
and a title that reaches a prompt outside the data fence goes through the fence's marker
defusing (`prompt_title`): a title is the material's own words, untrusted like its text."""

import re
import unicodedata

from evowiki.sources.config import defuse

TITLE_MAX = 80

_GENERIC = frozenset({
    "", "untitled", "document", "doc", "file", "scan", "download", "paper", "new document",
    "无标题", "未命名", "未命名资料", "新建文档", "文档"})
_HASHLIKE = re.compile(r"^(?:[0-9a-f]{12,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|\d+)$",
                       re.IGNORECASE)
_DECORATION = re.compile(r"[\s_\-.()\[\]（）]+")


def clean_title(text: str | None) -> str:
    """Control / format characters dropped, whitespace runs collapsed to one space, trimmed, cut
    to TITLE_MAX characters."""
    s = "".join(" " if c.isspace() else c for c in (text or "")
                if c.isspace() or unicodedata.category(c) not in ("Cc", "Cf"))
    return re.sub(r" {2,}", " ", s).strip()[:TITLE_MAX].strip()


def generic_title(text: str | None) -> bool:
    """A name that says nothing about the material: empty, a placeholder word (any case, with or
    without a trailing number such as 「document (3)」), or a hash / uuid / bare number."""
    s = clean_title(text).lower()
    core = _DECORATION.sub(" ", s).strip()
    base = re.sub(r"\s*\d+$", "", core).strip()
    return core in _GENERIC or base in _GENERIC or bool(_HASHLIKE.match(core.replace(" ", "")))


# a first bookmark or heading that names a part of the document, not the document: a section number,
# 第…章 / Chapter / Part / §, or front matter (contents, cover, preface, …)
_PART_HEAD = re.compile(
    r"^(?:\d+(?:\.\d+)*\b|[ivxlc]+\.\s|第\s*[0-9一二三四五六七八九十百零〇]+\s*[章节部分篇讲课]"
    r"|(?:chapter|section|part|lecture|unit|appendix)\s*(?:\d+|[ivxlc]+\b)|§|arxiv:)", re.IGNORECASE)
_FRONT_MATTER = frozenset({
    "contents", "table of contents", "toc", "cover", "front matter", "title page", "preface",
    "foreword", "introduction", "abstract", "copyright", "index", "acknowledgements",
    "acknowledgments", "目录", "封面", "前言", "序", "序言", "引言", "摘要", "版权", "索引", "致谢"})


def looks_like_title(text: str) -> bool:
    """A first top-level bookmark or heading that can stand for the whole document's title."""
    t = clean_title(text)
    return (len(t) >= 2 and not generic_title(t) and not _PART_HEAD.match(t)
            and t.lower().rstrip(":：. ") not in _FRONT_MATTER)


def prompt_title(text: str) -> str:
    """The title as a prompt may show it outside the fence: cleaned and marker-defused."""
    return defuse(clean_title(text))
