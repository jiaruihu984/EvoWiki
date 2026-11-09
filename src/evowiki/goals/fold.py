"""Page layer, lazy and fold-never-cut (plan rev 3.1 Phase 3; Phase 1 「这部分我懂」). Every
function here is 0 LLM calls. K up → a recap / explain / compare section whose cells are all known
folds (only its recall cue shows; the text stays). A folded section with a shaky / gap cell
unfolds. Sections the user folded or unfolded by hand (`fold_locked`) and locked ones (`pinned`) are
left alone. Collapsing a page to a reference card folds every unlocked foldable section without a
shaky / gap cell.
Follows the prototype's `foldPass` / `openPage` note."""

from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evowiki.goals.contracts import (
    FOLDABLE_ROLES,
    Band,
    KView,
    OutlineSection,
    PageOutline,
    SectionState,
    SectionText,
)
from evowiki.goals.guardrails import BAND_ZH, WEAK, PageSpec, cell_label, page_bands, section_of

States = dict[str, SectionState]
NOTE_MAX_MOVES = 4


ConflictCode = Literal["line_weak", "pinned_line_weak", "assumed_lost",
                       "uncovered_untaught", "uncovered_absent"]


class ConflictRef(BaseModel):
    """One unresolved conflict, structured: one per conflict cell. `section_id`: the section it is
    about (a pinned one-line section too), None when there is none. Kept here, not in patch.py,
    so `Page` can hold it without a patch -> fold import cycle."""

    model_config = ConfigDict(frozen=True)

    kind: ConflictCode
    section_id: str | None = None
    cell: str | None = None


class Page(BaseModel):
    """One page as kept in V: outline, per-section display state, written text, and the K bands
    the page was last brought up to date against. `unresolved_conflicts`: the conflict messages a
    rejected patch left behind (shown as a banner until a patch or a reorganize fixes them).
    `conflicts`: the same conflicts, structured (one per cell); always set together with
    `unresolved_conflicts`. Pages stored before it existed read as () until their next update."""

    model_config = ConfigDict(frozen=True)

    outline: PageOutline
    states: dict[str, SectionState]
    texts: dict[str, SectionText] = Field(default_factory=dict)
    snapshot: dict[str, Band] = Field(default_factory=dict)
    unresolved_conflicts: tuple[str, ...] = ()
    conflicts: tuple[ConflictRef, ...] = ()

    def state(self, sid: str) -> SectionState:
        return self.states.get(sid, SectionState(section_id=sid))


class FoldResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    states: dict[str, SectionState]
    folded: tuple[str, ...] = ()
    unfolded: tuple[str, ...] = ()


def states_for(outline: PageOutline, states: Mapping[str, SectionState] | None = None) -> States:
    old = states or {}
    return {s.id: old.get(s.id, SectionState(section_id=s.id)) for s in outline.sections}


def _foldable(s: OutlineSection) -> bool:
    return s.role in FOLDABLE_ROLES


def fold_pass(outline: PageOutline, states: Mapping[str, SectionState], k: KView) -> FoldResult:
    out = states_for(outline, states)
    folded: list[str] = []
    unfolded: list[str] = []
    for s in outline.sections:
        st = out[s.id]
        if not _foldable(s) or not s.cells or st.fold_locked or st.pinned:
            continue
        bands = [k.cell(c).band for c in s.cells]
        if not st.folded and all(b is Band.KNOWN for b in bands):
            out[s.id] = st.model_copy(update={"folded": True})
            folded.append(s.id)
        elif st.folded and any(b in WEAK for b in bands):
            out[s.id] = st.model_copy(update={"folded": False})
            unfolded.append(s.id)
    return FoldResult(states=out, folded=tuple(folded), unfolded=tuple(unfolded))


def know_this(outline: PageOutline, states: Mapping[str, SectionState], sid: str) -> States:
    """「这部分我懂」: fold this section and lock it (a short section just stays as it is)."""
    out = states_for(outline, states)
    if _foldable(section_of(outline, sid)):
        out[sid] = out[sid].model_copy(update={"folded": True, "fold_locked": True})
    return out


def toggle_fold(outline: PageOutline, states: Mapping[str, SectionState], sid: str) -> States:
    if not _foldable(section_of(outline, sid)):
        raise ValueError(f"section {sid} is not foldable")
    out = states_for(outline, states)
    out[sid] = out[sid].model_copy(update={"folded": not out[sid].folded, "fold_locked": True})
    return out


def set_pinned(outline: PageOutline, states: Mapping[str, SectionState], sid: str,
               pinned: bool = True) -> States:
    section_of(outline, sid)
    out = states_for(outline, states)
    out[sid] = out[sid].model_copy(update={"pinned": pinned})
    return out


def keep_locked(old: Page | None, new: Page) -> Page:
    """`new` (what a write made of the page) with every locked section of `old` as it was: its
    outline entry, text and state, in place when `new` still has the section, else right after the
    nearest section before it that `new` has (or at the top). The one guarantee behind 「锁定」: a
    whole-page write or a new outline cannot change or drop a locked section."""
    locked = [s for s in old.outline.sections if old.state(s.id).pinned] if old is not None else []
    if not locked:
        return new
    assert old is not None
    secs = list(new.outline.sections)
    texts, states = dict(new.texts), dict(new.states)
    before: list[str] = []
    for s in old.outline.sections:
        if old.state(s.id).pinned:
            ids = [x.id for x in secs]
            if s.id in ids:
                secs[ids.index(s.id)] = s
            else:
                near = next((x for x in reversed(before) if x in ids), None)
                secs.insert(ids.index(near) + 1 if near is not None else 0, s)
            states[s.id] = old.state(s.id)
            if (t := old.texts.get(s.id)) is not None:
                texts[s.id] = t
        before.append(s.id)
    return new.model_copy(update={"outline": new.outline.model_copy(update={"sections": tuple(secs)}),
                                  "texts": texts, "states": states})


def collapse_page(outline: PageOutline, states: Mapping[str, SectionState],
                  k: KView) -> FoldResult:
    """Reference card: every foldable section folded, unless the user locked its fold state or
    one of its cells is 模糊 / 不会 (WEAK) — a forgotten section stays open (user ruling 2)."""
    out = states_for(outline, states)
    folded: list[str] = []
    for s in outline.sections:
        st = out[s.id]
        if any(k.cell(c).band in WEAK for c in s.cells):
            continue
        if _foldable(s) and not st.fold_locked and not st.pinned and not st.folded:
            out[s.id] = st.model_copy(update={"folded": True})
            folded.append(s.id)
    return FoldResult(states=out, folded=tuple(folded))


def k_snapshot(spec: PageSpec, k: KView) -> dict[str, Band]:
    return page_bands(spec, k)


def moved_cells(before: Mapping[str, Band], after: Mapping[str, Band]) -> list[str]:
    return [c for c, b in after.items() if c in before and before[c] is not b]


def is_stale(spec: PageSpec, page: Page, k: KView) -> bool:
    return bool(moved_cells(page.snapshot, k_snapshot(spec, k)))


def since_last_visit(spec: PageSpec, outline: PageOutline, before: Mapping[str, Band],
                     after: Mapping[str, Band], fold: FoldResult) -> str:
    """「自你上次来……」, from the K snapshot taken when the page was last brought up to date."""

    def heads(ids: Iterable[str]) -> str:
        return "".join(f"「{section_of(outline, i).heading}」" for i in ids)

    moved = moved_cells(before, after)
    parts: list[str] = []
    if moved:
        shown = "；".join(f"{cell_label(spec, c)} {BAND_ZH[before[c]]}→{BAND_ZH[after[c]]}"
                         for c in moved[:NOTE_MAX_MOVES])
        more = f" 等 {len(moved)} 处" if len(moved) > NOTE_MAX_MOVES else ""
        parts.append(f"你的状态变了：{shown}{more}")
    if fold.folded:
        parts.append(f"已折叠{heads(fold.folded)}，外面留了回想提示，全文还在")
    if fold.unfolded:
        parts.append(f"重新展开了{heads(fold.unfolded)}")
    return "自你上次来：" + "。".join(parts) + "。" if parts else ""
