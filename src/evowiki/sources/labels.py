"""Section labels, the table-of-contents excerpt for intake, paragraphs, and coverage. Pure
functions."""

import re
from collections.abc import Collection, Sequence

from evowiki.goals.intake import SourceToc, TocSection
from evowiki.goals.reading import Para
from evowiki.sources import config
from evowiki.sources.config import PAGE_MARK_RE
from evowiki.sources.models import LinkRow, SectionRow, SourceRow
from evowiki.sources.titles import prompt_title

LEAD_CHARS = 160            # table-of-contents excerpt length for the model, not a content guardrail
TITLE_LABEL_CHARS = 12
HEAD_CHARS = 60             # a paragraph's opening (`Para.head`): the toc's paragraph line, the anchor
# 「3.2 …」「第3章」, and an appendix's own number 「A Model」「A.1.2 Alignment」
_NUM = re.compile(r"^\s*(?:第\s*)?(\d+(?:\.\d+)*|[A-Z](?:\.\d+)*(?=\s))")
_BLANK = re.compile(r"\n\s*\n")


def _short(title: str) -> str:
    """The title's first TITLE_LABEL_CHARS characters, not ending in half a word."""
    cut = title[:TITLE_LABEL_CHARS]
    if len(title) > len(cut) and title[len(cut)].isascii() and title[len(cut)].isalnum() and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut


def section_labels(sections: Sequence[SectionRow]) -> dict[str, str]:
    """section_id -> 「§3.2」「§A.1」: the leading number of the title, else its first 12 characters;
    repeats within one material get 「（2）」 and so on."""
    out: dict[str, str] = {}
    seen: dict[str, int] = {}
    for s in sorted(sections, key=lambda s: (s.ord, s.section_id)):
        m = _NUM.match(s.title)
        base = "§" + (m.group(1) if m else _short(s.title.strip()).rstrip())
        seen[base] = seen.get(base, 0) + 1
        out[s.section_id] = base if seen[base] == 1 else f"{base}（{seen[base]}）"
    return out


def section_names(sections: Sequence[SectionRow]) -> dict[str, str]:
    """section_id -> what the learner reads for it: 「§3.2」 for a numbered section, else its title
    (the model's 「§Definitions（2）」 labels are for the prompt)."""
    return {s.section_id: (f"§{m.group(1)}" if (m := _NUM.match(s.title)) else s.title.strip())
            for s in sections}


def lead(text: str, limit: int = LEAD_CHARS) -> str:
    """The first paragraph (up to the first blank line) without page markers, cut at `limit`."""
    clean = PAGE_MARK_RE.sub("", text).strip()
    first = re.split(r"\n\s*\n", clean, maxsplit=1)[0].strip()
    return first if len(first) <= limit else first[:limit] + "…"


def pages_text(page_from: int | None, page_to: int | None) -> str:
    if page_from is None:
        return ""
    if page_to is None or page_to == page_from:
        return f"p.{page_from}"
    return f"p.{page_from}–{page_to}"


def body_chars(title: str, text: str) -> int:
    """The length of a section's text past its heading (page markers out, runs of whitespace
    counted once): 0 for a section that is only a heading."""
    body = " ".join(PAGE_MARK_RE.sub(" ", text).split())
    head = " ".join(title.split())
    if head and body.startswith(head):
        body = body[len(head):].strip()
    return len(body)


def paragraphs(text: str, *, page: int | None = None) -> list[Para]:
    """A section's paragraphs (reading-mode spec §2.2: the one split that planning, excerpt
    cutting and re-anchoring share): split on blank lines; a chunk that is only a 〔p.N〕 mark
    starts that page and is no paragraph. Each paragraph's `page` is where it starts — the mark
    it opens with, else the page the text before it ended on (`page`: the page the text starts
    on, the section's `page_from` as `excerpts.pages_of` reads it; None: a paragraph before any
    mark has none) — and `page_to` the last mark inside it (else its `page`)."""
    out: list[Para] = []
    for chunk in _BLANK.split(text):
        marks = list(PAGE_MARK_RE.finditer(chunk))
        plain = " ".join(PAGE_MARK_RE.sub(" ", chunk).split())
        if not plain:
            if marks:
                page = int(marks[-1].group(1))
            continue
        start = int(marks[0].group(1)) if marks and not chunk[:marks[0].start()].strip() else page
        end = int(marks[-1].group(1)) if marks else start
        out.append(Para(index=len(out), text=chunk.strip(), page=start, page_to=end,
                        head=plain[:HEAD_CHARS]))
        page = end
    return out


def source_toc(source: SourceRow, sections: Sequence[SectionRow], *, prefix: str = "") -> SourceToc:
    """A material's toc for S1 (`TocSection`), with each section's paragraphs; a section longer
    than the page writer's full-text budget is `long` (reading-mode spec §2.2: the same budget
    decides when a reading page cannot get the whole section, so no new threshold)."""
    labels = section_labels(sections)
    ordered = sorted(sections, key=lambda s: (s.ord, s.section_id))
    budget = config.full_text_tokens()
    return SourceToc(
        source_id=source.source_id, title=prompt_title(source.title),
        sections=tuple(TocSection(id=s.section_id, label=prefix + labels[s.section_id],
                                  title=s.title, depth=s.depth,
                                  pages=pages_text(s.page_from, s.page_to), lead=lead(s.text),
                                  chars=body_chars(s.title, s.text),
                                  paras=tuple(paragraphs(s.text, page=s.page_from)),
                                  long=s.tokens > budget)
                       for s in ordered))


def uncovered(sections: Sequence[SectionRow], links: Sequence[LinkRow],
              read: Collection[str] = ()) -> list[tuple[str, bool]]:
    """Topmost sections no node comes from, by `ord`: (section_id, is_exercise). A section whose
    own, ancestor or descendant link is `teaches`/`reads` is covered; one reached only by
    `exercises` links is listed with is_exercise=True (an exercise section has no page of its own).
    `read`: sections a 读懂 goal's units read (reading-mode spec §5: a unit not analyzed yet has no
    `teaches` link, so they are passed apart), covered the same way."""
    by_id = {s.section_id: s for s in sections}
    real = {k.section_id for k in links if k.role != "exercises"} | set(read)
    exer = {k.section_id for k in links if k.role == "exercises"}

    def ancestors(sid: str) -> list[str]:
        out: list[str] = []
        p = by_id[sid].parent_id
        while p is not None and p in by_id:
            out.append(p)
            p = by_id[p].parent_id
        return out

    family = {sid: {sid, *ancestors(sid)} for sid in by_id}
    for sid in by_id:                       # add descendants
        for a in ancestors(sid):
            family[a].add(sid)

    # covered: own/ancestor/descendant link, or an ancestor that is itself covered that way
    bad = {sid for sid in by_id
           if not any(family[x] & real for x in (sid, *ancestors(sid)))}
    return [(s.section_id, bool(family[s.section_id] & exer))
            for s in sorted(sections, key=lambda s: (s.ord, s.section_id))
            if s.section_id in bad and (s.parent_id is None or s.parent_id not in bad)]
