"""Page updates when K moves (plan rev 3 Phase 1 「局部修改」 + rev 3.1 Phase 3 page layer), with no
model call of their own. Deterministic conflict check: a one-line section now holds a shaky / gap
cell; an assumed cell is no longer known; a shaky / gap cell is covered nowhere. The conflicts are
recorded on the page (`Page.unresolved_conflicts` / `conflicts`) and nothing is rewritten: an answer,
right or wrong, never changes the text he reads without his word (user rulings 2026-11-04; the
assistant offers to re-explain and he confirms the proposal). A conflict stays in the banner:
「按当前状态重新组织」 is a writer reorganize.

A cell he marked 「不会」 (`declared == "dont"`) is no conflict while the mark stands. The one case
that hands sections to the page writer, the one owner of a page's outline and text (skill design A7
item 5), is page feedback that names a missing concept (`declared_too`): that asks for the change.
The sections that can fix it (`writer_sections`: a non-pinned one-line section that holds a weak cell,
a non-pinned prose section where an untaught weak cell appears) wait for a scoped run that may also
insert sections (`wikiapp.stale.patch_pending`, `service.rsi_runs.patch_write`, the conflicts as its
instruction).

`apply_ops` / `_keep_untouched` stay for the assistant's accepted page edits (`assistant.wiki`)."""

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from evowiki.goals.contracts import (
    FOLDABLE_ROLES,
    Band,
    KView,
    Observation,
    PageOutline,
    SectionRole,
    SectionState,
)
from evowiki.goals.fold import (
    ConflictCode,
    ConflictRef,
    FoldResult,
    Page,
    fold_pass,
    k_snapshot,
    know_this,
    moved_cells,
    since_last_visit,
)
from evowiki.goals.guardrails import (
    BAND_ZH,
    WEAK,
    DraftOutline,
    DraftSection,
    PageSpec,
    cell_label,
    draft_of,
    page_bands,
    section_of,
)

_REWRITABLE = ("role", "heading", "cells", "notes", "why", "analogy_sources")


class Conflict(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["line_weak", "assumed_lost", "uncovered"]
    code: ConflictCode                   # fixed code for clients
    section: str | None = None           # the section to rewrite
    anchor: str | None = None            # the section a conflict is about when `section` is None
    cells: tuple[str, ...]
    message: str                         # the banner line and the writer's instruction


def find_conflicts(spec: PageSpec, outline: PageOutline, states: Mapping[str, SectionState],
                   bands: Mapping[str, Band], *, skip: str | None = None) -> list[Conflict]:
    """Mirrors guardrail coverage: every shaky / gap cell not taught in a recap / explain / compare
    section is a conflict, reported once. A non-pinned line section holding such cells is a
    `line_weak` (rewrite it); a pinned line, an organizer / check section, or no section at all
    is `uncovered` (insert a reminder). Assumed cells no longer known are `assumed_lost`. Cells of
    the `skip` section (the user just gave feedback on it) are not reported."""
    out: list[Conflict] = []
    covered = set(outline.assumed)
    taught = {c for s in outline.sections if s.role in FOLDABLE_ROLES for c in s.cells}
    reported: set[str] = set()
    for s in outline.sections:
        covered |= set(s.cells)
        if s.id == skip:
            reported |= set(s.cells)
            continue
        if s.role is not SectionRole.LINE:
            continue
        weak = tuple(c for c in s.cells
                     if bands.get(c) in WEAK and c not in taught and c not in reported)
        if not weak:
            continue
        reported |= set(weak)
        labels = "、".join(cell_label(spec, c) for c in weak)
        st = states.get(s.id)
        if st is not None and st.pinned:
            # The pinned line cannot change, yet the coverage guardrail needs its weak cells taught
            # somewhere: ask for a reminder section instead.
            out.append(Conflict(kind="uncovered", code="pinned_line_weak", anchor=s.id,
                                cells=weak, message=(
                f"「{s.heading}」已钉住（一行带过，不能改），但其中 {labels} 现在是模糊 / 不会："
                "需要插入一段提醒（recap）覆盖它们，钉住的段本身不能动")))
        else:
            out.append(Conflict(kind="line_weak", code="line_weak", section=s.id, cells=weak,
                                message=(
                f"「{s.heading}」只一行带过，但其中 {labels} 现在是模糊 / 不会")))
    for a in outline.assumed:
        if a in bands and bands[a] is not Band.KNOWN and a not in reported:
            reported.add(a)
            out.append(Conflict(kind="assumed_lost", code="assumed_lost", cells=(a,), message=(
                f"默认你会的 {cell_label(spec, a)} 现在是{BAND_ZH[bands[a]]}")))
    for c, b in bands.items():
        if b not in WEAK or c in taught or c in reported:
            continue
        where = ("只出现在开篇 / 确认题里，没有被提醒或讲解，需要插入一段提醒（recap）"
                 if c in covered else "页面上没有任何段覆盖它")
        out.append(Conflict(
            kind="uncovered", code="uncovered_untaught" if c in covered else "uncovered_absent",
            cells=(c,), message=f"{cell_label(spec, c)} 现在是{BAND_ZH[b]}，{where}"))
    return out


def conflict_refs(conflicts: Sequence[Conflict]) -> tuple[ConflictRef, ...]:
    """The structured twin of `tuple(c.message for c in conflicts)`: one entry per cell of every
    conflict; section_id = c.section or c.anchor."""
    return tuple(ConflictRef(kind=c.code, section_id=c.section or c.anchor, cell=cell)
                 for c in conflicts for cell in c.cells)


class DraftOp(BaseModel):
    op: str
    id: str | None = None
    after: str | None = None
    role: str | None = None
    heading: str | None = None
    cells: list[str] | None = None
    notes: str | None = None
    why: str | None = None
    analogy_sources: list[str] | None = None
    section: DraftSection | None = None


def _index(secs: list[DraftSection], sid: str | None) -> int | None:
    return next((i for i, s in enumerate(secs) if s.id == sid), None)


def apply_ops(outline: PageOutline,
              ops: Sequence[DraftOp]) -> tuple[DraftOutline, list[DraftOp]]:
    """Prototype `applyOps` over a draft copy. Unknown ops (and delete) are ignored; an insert
    gets a unique id; `after` that is missing or None means "at the top"."""
    d = draft_of(outline)
    secs = [s.model_copy(deep=True) for s in d.sections]
    assumed = list(d.assumed)
    applied: list[DraftOp] = []
    for op in ops:
        i = _index(secs, op.id)
        if op.op == "rewrite" and i is not None:
            secs[i] = secs[i].model_copy(update={f: getattr(op, f) for f in _REWRITABLE
                                                 if getattr(op, f) is not None})
        elif op.op == "insert" and op.section is not None:
            nid = op.section.id.strip() or "n1"
            while _index(secs, nid) is not None:
                nid += "x"
            new = op.section.model_copy(update={"id": nid})
            j = _index(secs, op.after)
            secs.insert(0 if j is None else j + 1, new)
            op = op.model_copy(update={"section": new})
        elif op.op == "move" and i is not None:
            moved = secs.pop(i)
            j = _index(secs, op.after)
            secs.insert(0 if j is None else j + 1, moved)
        elif op.op == "assume" and op.cells is not None:
            assumed = list(op.cells)
        else:
            continue
        applied.append(op)
    return DraftOutline(profile=d.profile, assumed=assumed, sections=secs), applied


def _keep_untouched(old: PageOutline, cand: DraftOutline, new: PageOutline) -> PageOutline:
    """Every section whose draft the patch left unchanged keeps its original OutlineSection: the
    draft round trip drops fields the LLM must not set (CheckItem.verified)."""
    old_by = {s.id: s for s in old.sections}
    old_draft = {s.id: s for s in draft_of(old).sections}
    secs = tuple(old_by[d.id] if d.id in old_draft and old_draft[d.id] == d
                 and n.id == d.id else n
                 for d, n in zip(cand.sections, new.sections, strict=True))
    return PageOutline(**{**dict(new), "sections": secs})


def writer_sections(outline: PageOutline, states: Mapping[str, SectionState],
                    conflicts: Sequence[Conflict]) -> tuple[str, ...]:
    """The sections the page writer rewrites for `conflicts`, page order: a `line_weak` section,
    and every non-pinned prose section (not a check) where an `uncovered_untaught` cell appears.
    Pinned sections never; a conflict with no such section is left to the banner."""
    pinned = {sid for sid, st in states.items() if st.pinned}
    lines = {c.section for c in conflicts if c.code == "line_weak"}
    untaught = {cell for c in conflicts if c.code == "uncovered_untaught" for cell in c.cells}
    return tuple(s.id for s in outline.sections
                 if s.id not in pinned and s.role is not SectionRole.CHECK
                 and (s.id in lines or untaught & set(s.cells)))


def writer_instruction(messages: Sequence[str]) -> str:
    """The page writer's instruction for the sections a K change handed to it."""
    return ("学习者的掌握情况变了，这一页有这些地方内容不够：\n- " + "\n- ".join(messages)
            + "\n只改写这次范围内的段；需要时可以在用到它之前插入一段提醒。")


def _conflict_bands(spec: PageSpec, k: KView, *, declared_too: bool = False) -> dict[str, Band]:
    return {c: b for c, b in page_bands(spec, k).items()
            if declared_too or k.cell(c).declared != "dont"}


def recheck(spec: PageSpec, page: Page, k: KView) -> Page:
    """`page` with its conflicts found again (after the writer wrote what a K change handed to
    it): () when it fixed them all."""
    conflicts = find_conflicts(spec, page.outline, page.states, _conflict_bands(spec, k))
    return page.model_copy(update={"unresolved_conflicts": tuple(c.message for c in conflicts),
                                   "conflicts": conflict_refs(conflicts)})


# ---- page flow: section feedback, K changed, page opened ----

FeedbackKind = Literal["know", "deeper", "other"]   # deeper / other: the page writer rewrites
FEEDBACK_STRENGTH: dict[str, tuple[Literal["+", "-"], float]] = {
    "know": ("+", 0.6), "deeper": ("-", 0.4)}


class UpdateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    page: Page
    note: str = ""
    folded: tuple[str, ...] = ()
    unfolded: tuple[str, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    to_write: tuple[str, ...] = ()       # `writer_sections` of the conflicts: wait for the writer
    observations: tuple[Observation, ...] = ()
    unresolved: tuple[str, ...] = ()
    banner: str = ""


def unresolved_banner(page: Page) -> str:
    n = len(page.unresolved_conflicts)
    return f"这页有 {n} 处内容可能不够，点『按当前状态重新组织』补上" if n else ""


def feedback_observations(spec: PageSpec, page: Page, sid: str,
                          kind: FeedbackKind) -> tuple[Observation, ...]:
    """The K observations a section feedback carries (one per cell of the section)."""
    s = section_of(page.outline, sid)
    if kind not in FEEDBACK_STRENGTH:
        return ()
    pol, strength = FEEDBACK_STRENGTH[kind]
    return tuple(Observation(cell=c, polarity=pol, strength=strength, kind="feedback",
                             evidence_ref=f"feedback:{spec.page_id}:{sid}:{kind}")
                 for c in s.cells)


def feedback_outline(page: Page, sid: str, kind: FeedbackKind) -> PageOutline:
    """The outline a 「再讲深一点 / 换个讲法」 rewrite writes against: 「再讲深一点」 makes the section
    an explain section; ValueError for a check section or one with no text yet."""
    s = section_of(page.outline, sid)
    if kind not in ("deeper", "other"):
        raise ValueError(f"unknown feedback kind {kind!r}")
    if s.role is SectionRole.CHECK:
        raise ValueError(f"section {sid} is a check question; it cannot be rewritten")
    if page.texts.get(sid) is None:
        raise ValueError(f"section {sid} has no text yet; write it before asking for {kind!r}")
    if kind == "deeper" and s.role is not SectionRole.EXPLAIN:
        new_s = s.model_copy(update={"role": SectionRole.EXPLAIN, "why": "你点了“再讲深一点”"})
        return page.outline.model_copy(update={"sections": tuple(
            new_s if x.id == sid else x for x in page.outline.sections)})
    return page.outline


def feedback(spec: PageSpec, page: Page, sid: str, kind: Literal["know"]) -> UpdateResult:
    """「这部分我懂」: folds and locks the section (0 calls) and carries its `+0.6` observations. No
    model call and K is never written: the caller applies `observations` in the K lane,
    then runs the K-change pass with `skip=sid`. 「再讲深一点 / 换个讲法」 are rewritten by the page
    writer (`wikiapp.kchange.feedback_rewrite_stream`, against `feedback_outline`)."""
    if kind != "know":
        raise ValueError(f"feedback {kind!r} is rewritten by the page writer, not here")
    return UpdateResult(page=page.model_copy(update={
        "states": know_this(page.outline, page.states, sid)}),
        observations=feedback_observations(spec, page, sid, kind),
        unresolved=page.unresolved_conflicts, banner=unresolved_banner(page))


def _bring_up_to_date(spec: PageSpec, page: Page, k: KView, *, skip: str | None,
                      declared_too: bool = False) -> tuple[Page, FoldResult, list[Conflict], tuple[str, ...]]:
    """Fold / unfold and a new K snapshot (0 calls); the conflicts are recorded on the page; with
    `declared_too` their `writer_sections` are returned (unfolded: the writer rewrites them; their
    old text stays until then). The `skip` section (the user just asked to deepen / rephrase it) keeps its fold state in
    this pass; it is not locked, so a later pass without `skip` folds it normally. With no
    conflicts the recorded ones are cleared, unless `skip` is set (the skipped section may hide
    them)."""
    fold = fold_pass(page.outline, page.states, k)
    if skip is not None and skip in fold.states:
        fold = FoldResult(states={**fold.states, skip: page.state(skip)},
                          folded=tuple(x for x in fold.folded if x != skip),
                          unfolded=tuple(x for x in fold.unfolded if x != skip))
    page = page.model_copy(update={"states": fold.states, "snapshot": k_snapshot(spec, k)})
    conflicts = find_conflicts(spec, page.outline, page.states,
                               _conflict_bands(spec, k, declared_too=declared_too), skip=skip)
    if not conflicts:
        if skip is None:
            page = page.model_copy(update={"unresolved_conflicts": (), "conflicts": ()})
        return page, fold, conflicts, ()
    to_write = writer_sections(page.outline, page.states, conflicts) if declared_too else ()
    states = {**page.states, **{sid: page.state(sid).model_copy(update={"folded": False})
                                for sid in to_write}}
    return (page.model_copy(update={"states": states,
                                    "unresolved_conflicts": tuple(c.message for c in conflicts),
                                    "conflicts": conflict_refs(conflicts)}),
            fold, conflicts, to_write)


def _summary(page: Page, to_write: Sequence[str]) -> str:
    heads = "".join(f"「{section_of(page.outline, sid).heading}」" for sid in to_write)
    return f"{heads}待重写，其余原样保留" if heads else ""


def after_k_change(spec: PageSpec, page: Page, k: KView, *, skip: str | None = None,
                   declared_too: bool = False) -> UpdateResult:
    """K changed while the page is open: fold / unfold, conflicts recorded, and handed to the
    writer only with `declared_too` (0 calls). `skip`: the section the user just gave feedback on."""
    new, fold, conflicts, to_write = _bring_up_to_date(spec, page, k, skip=skip,
                                                       declared_too=declared_too)
    return UpdateResult(page=new, note=_summary(new, to_write), folded=fold.folded,
                        unfolded=fold.unfolded, conflicts=tuple(conflicts), to_write=to_write,
                        unresolved=new.unresolved_conflicts, banner=unresolved_banner(new))


def open_page(spec: PageSpec, page: Page, k: KView) -> UpdateResult:
    """Opening a page: nothing to do unless K moved since its snapshot; then fold / unfold, a
    「自你上次来……」 note, and the conflicts recorded (0 calls either way)."""
    after = k_snapshot(spec, k)
    if not moved_cells(page.snapshot, after):
        return UpdateResult(page=page, unresolved=page.unresolved_conflicts,
                            banner=unresolved_banner(page))
    new, fold, conflicts, to_write = _bring_up_to_date(spec, page, k, skip=None)
    note = since_last_visit(spec, page.outline, page.snapshot, after, fold)
    summary = _summary(new, to_write)
    if summary:
        note = (note or "自你上次来：") + summary + "。"
    return UpdateResult(page=new, note=note, folded=fold.folded, unfolded=fold.unfolded,
                        conflicts=tuple(conflicts), to_write=to_write,
                        unresolved=new.unresolved_conflicts, banner=unresolved_banner(new))
