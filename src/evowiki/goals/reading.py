"""Reading mode (读懂这一篇, spec the product specification §2):
a goal to understand one material reads it unit by unit. A unit is cut by content (a chapter, a
section, a step of the argument, an experiment; short neighbours merged, a long section split by
paragraphs, §2.2) and located in the material by section + paragraph range + the first words of
its first paragraph; pages only locate and cite (one unit may span pages, one page may hold
several units). A unit is NOT a knowledge node: it never enters ConceptName, GraphSaved, K or a
goal map; the points it teaches / uses are ordinary nodes (§2.3).

Stored as a whole snapshot (`wikiapp.events.ReadingSaved`, Q1) so reading progress changes in
the same commit as the page it starts. Ids, order and locations are stable (§2.1): a unit id is
made once (`new_unit_id`) and never changed or reused (a merge keeps the first unit's id); order
changes only by a merge of two adjacent units nobody started; a started unit (`page_id`) is frozen
(no merge, no skip, no new spans). When a section's text changes (re-OCR) `reanchor` finds each
split span again by its anchor, else reads the whole section and marks the span `uncertain`
(「原文位置可能不准」) — never a quiet move.

Code checks only the structure (`check_units`, §2.2); how to cut is the model's (S1's understand
mode, goals/blocks.py). Paragraphs come from `sources.labels.paragraphs` (one split for planning,
excerpt cutting and re-anchoring); the helpers here take its `Para`s, so goals/ stays free of the
sources layer. Pure: no IO, no LLM."""

from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from evowiki.goals.intake import TocSection

ANCHOR_CHARS = 15       # the anchor: the first words of a span's first paragraph (§2.1)


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Para(_M):
    """One paragraph of a section's text (`sources.labels.paragraphs`: split on blank lines).
    `index` from 0 within the section; `text` as stored (its 〔p.N〕 marks kept: the writer cites
    by them); `page` / `page_to`: the PDF pages it starts and ends on (None: no page numbers);
    `head`: its opening without page marks, whitespace collapsed (the toc's paragraph line and
    the anchor come from it)."""

    index: int = Field(ge=0)
    text: str
    page: int | None = None
    page_to: int | None = None
    head: str = ""


class UnitSpan(_M):
    """A contiguous stretch of the material: paragraphs `para_from`–`para_to` (both included,
    from 0) of one section; both None = the whole section. `anchor`: the first words of its first
    paragraph (re-anchoring after the text changes); `page_from` / `page_to`: the PDF pages its
    paragraphs are on (None: no page numbers). `uncertain`: re-anchoring could not find it and it
    reads the whole section now (「原文位置可能不准」 on the unit's row)."""

    section_id: str
    para_from: int | None = None
    para_to: int | None = None
    anchor: str = ""
    page_from: int | None = None
    page_to: int | None = None
    uncertain: bool = False

    @property
    def whole(self) -> bool:
        return self.para_from is None


class ReadingUnit(_M):
    """One unit to read (one page of guided reading, Q4). `unit_id` made once, never changed or
    reused; `order` from 1, in the material's order (only for sorting: identity is the id);
    `title` / `about`: the model's short title and one line of what it is about (never built from
    section numbers). Filled when the unit is entered (the 「看这一段」 call, §3.5), empty from the
    initial plan: `analyzed`, `teaches` (the points this unit explains: shared-graph concept ids),
    `uses` (the prerequisites it uses without explaining them), `focus` (what the page should
    explain closely). `page_id`: its reading page, once started (its location is frozen from
    then on); `skipped`: the learner will not read it."""

    unit_id: str
    order: int = Field(ge=1)
    title: str
    about: str = ""
    spans: tuple[UnitSpan, ...] = Field(min_length=1)
    analyzed: bool = False
    teaches: tuple[str, ...] = ()
    uses: tuple[str, ...] = ()
    focus: tuple[str, ...] = ()
    page_id: str | None = None
    skipped: bool = False


class _Units(_M):
    """What the stability ops work on: a confirmed `Reading` or a draft's `ReadingPlan`."""

    units: tuple[ReadingUnit, ...]


class ReadingPlan(_Units):
    """S1's initial plan of a 读懂 goal (`BlocksResult.reading`, §3.2): the units (structure only,
    fresh ids) and `left_out` (section ids not read — references, acknowledgements; display only),
    for the one material `source_id` titled `title`. The draft card shows it; confirming makes it
    a `Reading`."""

    source_id: str
    title: str
    left_out: tuple[str, ...] = ()


class Reading(_Units):
    """The reading of goal `map_id` over the one material `source_id` (titled `title`), §2.1."""

    reading_id: str
    map_id: str
    source_id: str
    title: str
    left_out: tuple[str, ...] = ()
    created_at: AwareDatetime


RefusedReason = Literal["unknown_unit", "same_unit", "not_adjacent", "started", "skipped",
                        "not_skipped", "not_analyzed"]


class ReadingRefused(ValueError):
    """A stability op that cannot be done (§2.1); nothing changed. `reason`: unknown_unit,
    same_unit, not_adjacent, started (the unit has a page: its location is frozen), skipped (a
    skipped unit is not merged: restore it first), not_skipped (unskip of a unit read),
    not_analyzed (a page or a use before the unit's 「看这一段」: wikiapp/reading.py)."""

    def __init__(self, reason: RefusedReason, unit_id: str) -> None:
        super().__init__(f"{reason}: {unit_id}")
        self.reason: RefusedReason = reason
        self.unit_id = unit_id


REFUSED_TEXT = {
    "started": "这一段已经开始读了，位置不能再改",
    "not_adjacent": "只能合并相邻的两段",
    "same_unit": "要合并的是同一段",
    "skipped": "这一段设成了不读，先恢复它",
    "not_skipped": "这一段没有设成不读",
    "not_analyzed": "这一段还没看过讲什么，先点开它",
    "unknown_unit": "这份阅读里没有这一段",
}


def refused_text(exc: ReadingRefused) -> str:
    """What the learner is told for a refused stability op (the API's 409, the chat's edit_reading)."""
    return REFUSED_TEXT.get(exc.reason, "这一段现在不能这样改")


def new_unit_id(new_id: Callable[[str], str]) -> str:
    """A fresh unit id from the injected id source (prefix `ru`, the wikiapp id convention)."""
    return new_id("ru")


def anchor_of(para: Para) -> str:
    return para.head[:ANCHOR_CHARS]


def span_of(paras: Sequence[Para], section_id: str, para_from: int | None = None,
            para_to: int | None = None) -> UnitSpan:
    """The span over paragraphs `para_from`–`para_to` of a section (`paras`: its paragraphs;
    both None: the whole section), with its anchor and pages computed from the paragraphs —
    never written by the model (§3.2). Range errors are `check_units`' to report first;
    an index outside `paras` here is an IndexError."""
    if (para_from is None) != (para_to is None):
        raise ValueError("a span has both paragraph bounds or neither")
    if para_from is None or para_to is None:
        picked = list(paras)
    else:
        if not 0 <= para_from <= para_to < len(paras):
            raise IndexError(f"paragraphs {para_from}-{para_to} of {len(paras)}")
        picked = list(paras[para_from:para_to + 1])
    starts = [p.page for p in picked if p.page is not None]
    ends = [p.page_to if p.page_to is not None else p.page for p in picked
            if (p.page_to if p.page_to is not None else p.page) is not None]
    return UnitSpan(section_id=section_id, para_from=para_from, para_to=para_to,
                    anchor=anchor_of(picked[0]) if picked else "",
                    page_from=starts[0] if starts else None, page_to=ends[-1] if ends else None)


# --- structure check (§2.2) --------------------------------------------------------------------

_END = 1 << 30          # a whole section's last paragraph, for ordering


def _name(u: ReadingUnit) -> str:
    return f"第 {u.order} 个单元「{u.title}」"


def check_units(units: Sequence[ReadingUnit], sections: "Sequence[TocSection]",
                paragraph_counts: Mapping[str, int]) -> list[str]:
    """The problems of a unit structure (empty: usable), worded for the model's retry (§2.2):
    every section is in the material's toc (`sections`, in reading order) and every paragraph
    index within its section (`paragraph_counts`: section id → paragraphs; a section without a
    count can only be read whole); units in the material's order, `order` 1, 2, 3 …; a unit's
    spans contiguous in the text (across sections: the first ends its section, the next starts
    its own, only heading-only sections between); no paragraph in two units, no section both read
    whole and split; a heading-only section (「只有标题」) never a unit on its own; at least one
    unit. Code checks the structure only, never how the model cut by content."""
    if not units:
        return ["一个单元都没有：至少要排一个单元"]
    pos = {s.id: i for i, s in enumerate(sections)}
    label = {s.id: s.label for s in sections}
    heading_only = {s.id for s in sections if s.chars == 0}
    problems: list[str] = []
    whole: dict[str, str] = {}                    # section id → the unit reading it whole
    split: dict[str, list[tuple[int, int, str]]] = {}
    last_end: tuple[int, int] | None = None
    for n, u in enumerate(units, start=1):
        if u.order != n:
            problems.append(f"{_name(u)}的序号应该是 {n}：单元按原文顺序从 1 连续编号")
        bounds: list[tuple[int, int, int]] = []    # (section pos, first para, last para)
        for sp in u.spans:
            if sp.section_id not in pos:
                problems.append(f"{_name(u)}写了目录里没有的节（{sp.section_id}）")
                bounds = []
                break
            lab, i = label[sp.section_id], pos[sp.section_id]
            if (sp.para_from is None) != (sp.para_to is None):
                problems.append(f"{_name(u)}在{lab}的段落范围不完整")
                bounds = []
                break
            if sp.para_from is None or sp.para_to is None:
                if sp.section_id in whole or sp.section_id in split:
                    problems.append(f"{lab}已经排进别的单元，又在{_name(u)}里整节出现")
                whole.setdefault(sp.section_id, u.unit_id)
                bounds.append((i, 0, _END))
                continue
            count = paragraph_counts.get(sp.section_id)
            if count is None:
                problems.append(f"{lab}没有段落列表，只能整节读（{_name(u)}）")
                bounds = []
                break
            if not 0 <= sp.para_from <= sp.para_to < count:
                problems.append(f"{_name(u)}在{lab}的段落 {sp.para_from}-{sp.para_to} 超出范围："
                                f"这一节共 {count} 段，从 0 数到 {count - 1}")
                bounds = []
                break
            if sp.section_id in whole:
                problems.append(f"{lab}已经整节排进别的单元，又在{_name(u)}里拆开读")
            for a, b, other in split.get(sp.section_id, []):
                if sp.para_from <= b and a <= sp.para_to:
                    problems.append(f"{lab}的段落 {max(a, sp.para_from)}-{min(b, sp.para_to)} "
                                    f"同时在{other}和{_name(u)}里")
            split.setdefault(sp.section_id, []).append((sp.para_from, sp.para_to, _name(u)))
            bounds.append((i, sp.para_from, sp.para_to))
        if not bounds:
            continue
        if all(sp.section_id in heading_only for sp in u.spans):
            problems.append(f"{_name(u)}只有标题、没有正文：把它并进它下面的小节")
        for (i, _, b), (j, a2, _) in pairwise(bounds):
            if _contiguous(i, b, j, a2, sections, paragraph_counts, heading_only):
                continue
            problems.append(f"{_name(u)}的原文不连续：{label[sections[i].id]}和"
                            f"{label[sections[j].id]}之间隔着别的内容，跨节合并只能合相邻的内容")
        start, end = (bounds[0][0], bounds[0][1]), (bounds[-1][0], bounds[-1][2])
        if last_end is not None and start <= last_end:
            problems.append(f"{_name(u)}没有按原文顺序排在上一个单元后面")
        last_end = max(end, last_end) if last_end is not None else end
    return problems


def _contiguous(i: int, last: int, j: int, first: int, sections: "Sequence[TocSection]",
                counts: Mapping[str, int], heading_only: set[str]) -> bool:
    """Does a span starting at paragraph `first` of section `j` follow one ending at paragraph
    `last` of section `i` directly (`_END`: the whole section)?"""
    if j == i:
        return last != _END and first == last + 1
    if j < i:
        return False
    count = counts.get(sections[i].id)
    ends = last == _END or (count is not None and last == count - 1)
    return ends and first == 0 and all(s.id in heading_only for s in sections[i + 1:j])


# --- stability ops (§2.1, §3.3) ----------------------------------------------------------------

def _at(units: Sequence[ReadingUnit], unit_id: str) -> int:
    i = next((k for k, u in enumerate(units) if u.unit_id == unit_id), None)
    if i is None:
        raise ReadingRefused("unknown_unit", unit_id)
    return i


def _joined(spans: Sequence[UnitSpan]) -> tuple[UnitSpan, ...]:
    """Spans in order, two neighbouring paragraph ranges of one section as one span."""
    out: list[UnitSpan] = []
    for sp in spans:
        prev = out[-1] if out else None
        if (prev is not None and prev.section_id == sp.section_id and not prev.whole
                and not sp.whole and prev.para_to is not None and sp.para_from == prev.para_to + 1):
            out[-1] = prev.model_copy(update={
                "para_to": sp.para_to, "page_to": sp.page_to if sp.page_to is not None
                else prev.page_to, "uncertain": prev.uncertain or sp.uncertain})
        else:
            out.append(sp)
    return tuple(out)


def _once(*parts: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x for p in parts for x in p))


def merge_units[R: _Units](reading: R, first_id: str, second_id: str, *, title: str = "") -> R:
    """Units `first_id` and the one right after it (`second_id`) as one unit (§2.1, §3.3): the
    first's id stays, the second's is gone for good; spans in order (a section's neighbouring
    ranges joined); `title` or both titles joined by 、, the abouts by ；; what the 「看这一段」
    call found is kept when both were analyzed; else the merged unit is not analyzed and keeps
    none of it (its 「看这一段」 reads the whole merged text again; the links its old teaches had
    go at that commit: wikiapp.reading.teaches_links). The units after it move up one.
    ReadingRefused: an unknown or the same unit, not adjacent (by `order`), either started (its
    location is frozen) or skipped."""
    units = sorted(reading.units, key=lambda u: u.order)
    i, j = _at(units, first_id), _at(units, second_id)
    if i == j:
        raise ReadingRefused("same_unit", first_id)
    a, b = units[i], units[j]
    for u in (a, b):
        if u.page_id is not None:
            raise ReadingRefused("started", u.unit_id)
    if j != i + 1:
        raise ReadingRefused("not_adjacent", second_id)
    for u in (a, b):
        if u.skipped:
            raise ReadingRefused("skipped", u.unit_id)
    both = a.analyzed and b.analyzed
    merged = a.model_copy(update={
        "title": title.strip() or "、".join(_once((a.title, b.title))),
        "about": "；".join(x for x in _once((a.about, b.about)) if x),
        "spans": _joined([*a.spans, *b.spans]),
        "analyzed": both, "teaches": _once(a.teaches, b.teaches) if both else (),
        "uses": tuple(x for x in _once(a.uses, b.uses) if x not in (*a.teaches, *b.teaches))
        if both else (),
        "focus": _once(a.focus, b.focus) if both else ()})
    out = [*units[:i], merged, *units[j + 1:]]
    return reading.model_copy(update={"units": tuple(
        u.model_copy(update={"order": n}) for n, u in enumerate(out, start=1))})


def _set_skipped[R: _Units](reading: R, unit_id: str, skipped: bool) -> R:
    units = list(reading.units)
    i = _at(units, unit_id)
    u = units[i]
    if skipped and u.page_id is not None:
        raise ReadingRefused("started", unit_id)
    if skipped and u.skipped:
        raise ReadingRefused("skipped", unit_id)
    if not skipped and not u.skipped:
        raise ReadingRefused("not_skipped", unit_id)
    units[i] = u.model_copy(update={"skipped": skipped})
    return reading.model_copy(update={"units": tuple(units)})


def skip_unit[R: _Units](reading: R, unit_id: str) -> R:
    """The learner will not read this unit (「这一段不读」). ReadingRefused: unknown, started
    (it has a page), already skipped."""
    return _set_skipped(reading, unit_id, True)


def unskip_unit[R: _Units](reading: R, unit_id: str) -> R:
    """A skipped unit is read again. ReadingRefused: unknown, not skipped."""
    return _set_skipped(reading, unit_id, False)


def _find(paras: Sequence[Para], anchor: str) -> int | None:
    if not anchor:
        return None
    return next((p.index for p in paras if p.head.startswith(anchor)), None)


def reanchor[R: _Units](reading: R, section_id: str, paras: Sequence[Para]) -> R:
    """Section `section_id`'s text changed (re-OCR, `importer.swap_reads`); `paras`: its new
    paragraphs. Each split span of it is found again by its anchor (the first paragraph opening
    with it, in order): it ends where the next found span starts when the two were neighbours,
    else after its old length (never into the next found span). A span whose anchor is not found
    (or no longer in order) is marked `uncertain` (「原文位置可能不准」 — shown, never a quiet
    move) and reads the paragraphs between its neighbours, so no paragraph is in two units; when
    that is the whole section (no other span of it), it reads the section whole. A whole-section
    span gets its new pages and anchor. Applies to started units too: re-anchoring keeps them on
    the same text. When no paragraph is left between its neighbours the span reads the section
    whole and overlaps them: `check_units` reports it (wikiapp.reading.reanchor_readings fails
    closed on it)."""
    refs = [(k, n, sp) for k, u in enumerate(reading.units) for n, sp in enumerate(u.spans)
            if sp.section_id == section_id]
    split = sorted(((k, n, sp) for k, n, sp in refs if not sp.whole),
                   key=lambda t: t[2].para_from or 0)
    starts: list[int | None] = []
    last = -1
    for _, _, sp in split:
        f = _find(paras, sp.anchor)
        starts.append(f if f is not None and f > last else None)
        last = f if f is not None and f > last else last
    count = len(paras)
    new: dict[tuple[int, int], UnitSpan] = {}
    prev_end = -1
    for at, (k, n, sp) in enumerate(split):
        limit = next((x for x in starts[at + 1:] if x is not None), count) - 1
        old_len = (sp.para_to or 0) - (sp.para_from or 0)
        nxt = split[at + 1][2] if at + 1 < len(split) else None
        start = starts[at]
        if start is not None:
            joined = (nxt is not None and starts[at + 1] is not None
                      and nxt.para_from == (sp.para_to or 0) + 1)
            end = limit if joined else min(start + old_len, limit)
            new[(k, n)] = span_of(paras, section_id, start, end)
            prev_end = end
            continue
        start = prev_end + 1
        end = min(start + old_len, limit) if nxt is not None and starts[at + 1] is None else limit
        if start > end or (start == 0 and end == count - 1):
            new[(k, n)] = span_of(paras, section_id).model_copy(update={"uncertain": True})
        else:
            new[(k, n)] = span_of(paras, section_id, start, end).model_copy(
                update={"uncertain": True})
            prev_end = end
    for k, n, sp in refs:
        if sp.whole:
            new[(k, n)] = span_of(paras, section_id).model_copy(update={"uncertain": sp.uncertain})
    units = tuple(
        u.model_copy(update={"spans": tuple(new.get((k, n), sp) for n, sp in enumerate(u.spans))})
        if any(sp.section_id == section_id for sp in u.spans) else u
        for k, u in enumerate(reading.units))
    return reading.model_copy(update={"units": units})
