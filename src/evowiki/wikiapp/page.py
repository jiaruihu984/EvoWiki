"""Opening a page (lazy update), fold / pin, 「学完了」 (plan Task 8).

Opening costs 0 LLM calls: when K moved since the page's snapshot, `page_update.open_page` folds /
unfolds, takes a new snapshot and records the conflicts (`Page.unresolved_conflicts`); the sections
that can fix them wait for the page writer (`update_page`, shared with the K-change flow).

「自你上次来」 is structured (D17): `DiffNote(cell_changes, folded, unfolded, patched)` built from
the snapshots, the fold result and the sections handed to the writer — never from the Chinese
note."""

import dataclasses
from collections.abc import Callable
from datetime import datetime

from evowiki.belief.aspects import PageRead
from evowiki.goals.contracts import FOLDABLE_ROLES, KView, OutlineSection
from evowiki.goals.fold import (
    Page,
    collapse_page,
    moved_cells,
    set_pinned,
    toggle_fold,
)
from evowiki.goals.guardrails import PageSpec
from evowiki.goals.page_update import UpdateResult, after_k_change, open_page
from evowiki.teach.contracts import SectionLog
from evowiki.wikiapp.events import (
    CellChange,
    DiffNote,
    PageOpened,
    PageRecord,
    PageSaved,
    SectionChange,
    UsageAdded,
    UsageRecord,
    WikiEvent,
)
from evowiki.wikiapp.kwrite import (
    Ids,
    is_page_stale,  # noqa: F401  (re-export, Task 8 API)
    page_spec,
)
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import K_SCOPE, Commit, Repo, Snapshot, WikiConflict, WikiInvalid
from evowiki.wikiapp.sectionlog import logs_event, section_logs
from evowiki.wikiapp.signals import signal_event
from evowiki.wikiapp.state import apply_event
from evowiki.wikiapp.teachspec import PageTeach, SpecInvalid, page_teach
from evowiki.wikiapp.views import page_out, record_section_out
from evowiki.wikiapp.wiki_schemas import DoneOut, PageOut, SectionOut

DONE_USAGE = "这一页我学完了"
OPEN_TRIES = 3          # open_page_flow: the first commit plus two retries on WikiConflict


# ---- shared with the K-change flow (Task 9) ----

def update_page(spec: PageSpec, page: Page, k: KView, *, opening: bool,
                skip: str | None = None, declared_too: bool = False) -> UpdateResult:
    """Bring a page up to date with K (0 calls). `opening`: `page_update.open_page` (nothing to do unless
    K moved); else `page_update.after_k_change(skip=…)`."""
    return (open_page(spec, page, k) if opening
            else after_k_change(spec, page, k, skip=skip, declared_too=declared_too))


def section_changes(res: UpdateResult) -> tuple[dict[str, SectionChange], tuple[str, ...]]:
    """Per-section change marks of one update and the sections it handed to the writer (marked
    "rewritten": they wait for it), page order (D17)."""
    changed: dict[str, SectionChange] = {sid: "folded" for sid in res.folded}
    changed.update({sid: "unfolded" for sid in res.unfolded})
    changed.update({sid: "rewritten" for sid in res.to_write})
    return changed, res.to_write


def diff_note(before: dict, res: UpdateResult, patched: tuple[str, ...],
              collapsed: tuple[str, ...] = ()) -> DiffNote | None:
    """「自你上次来」: band moves since `before` (the old snapshot), folds (including a
    reference-card collapse applied on this open, page order), unfolds and patched sections.
    None when no band moved and nothing was collapsed. A section collapsed and then unfolded by
    the fold pass in the same open ends where it was, so it is in neither list."""
    after = res.page.snapshot
    moved = moved_cells(before, after)
    if not moved and not collapsed:
        return None
    folded = {x for x in collapsed if x not in res.unfolded} | set(res.folded)
    return DiffNote(cell_changes=tuple(CellChange(cell=c, before=before[c], after=after[c])
                                       for c in moved),
                    folded=tuple(s.id for s in res.page.outline.sections if s.id in folded),
                    unfolded=tuple(x for x in res.unfolded if x not in collapsed),
                    patched=patched)


# ---- flows ----

def _record(snap: Snapshot, page_id: str) -> PageRecord:
    rec = snap.state.pages.get(page_id)
    if rec is None:
        raise LookupError(f"没有这个页面：{page_id}")
    return rec


def _wev(new_id: Ids, now: datetime, body) -> WikiEvent:
    return WikiEvent(event_id=new_id("wev"), ts=now, body=body)


def _after(snap: Snapshot, wiki: list[WikiEvent]) -> Snapshot:
    state = snap.state
    for e in wiki:
        state = apply_event(state, e)
    return dataclasses.replace(snap, state=state)


def _open_update(snap: Snapshot, rec: PageRecord,
                 now: datetime) -> tuple[PageRecord, tuple[SectionLog, ...]]:
    """The opened record and the `patch` SectionLogs of the sections the update handed to the page
    writer (they wait for it: `stale.patch_pending`). A user whose old guidelines overflow the spec
    (SpecInvalid) still opens the page: fold / unfold and the conflicts, but nothing waits for the
    writer (no logs)."""
    assert rec.page is not None
    teach: PageTeach | None
    try:
        teach = page_teach(snap.state, rec.page_id)
        spec = page_spec(snap.state, snap.k, rec.page_id, user_id=snap.user_id, teach=teach)
    except SpecInvalid:
        teach = None
        spec = page_spec(snap.state, snap.k, rec.page_id, user_id=snap.user_id, bands_only=True)
    page = rec.page
    changed: dict[str, SectionChange] = {}
    collapsed: tuple[str, ...] = ()
    collapsing = rec.shelved and not rec.collapse_applied
    if collapsing:              # a regenerated reference card: fold all but weak sections (0 calls)
        c = collapse_page(page.outline, page.states, snap.kview(now))
        page = page.model_copy(update={"states": c.states})
        collapsed = c.folded
    before = page.snapshot
    kview = snap.kview(now)
    res = update_page(spec, page, kview, opening=True)
    marks, patched = section_changes(res)
    logs = (section_logs(res.page.outline, patched, producer="patch", spec=spec, k=kview,
                         teach=teach, at=now, infer_version=snap.infer_version)
            if patched and teach is not None else ())
    for sid in collapsed:
        if sid in res.unfolded:                 # collapsed, then unfolded again: no net change
            marks.pop(sid, None)
        else:
            changed[sid] = "folded"
    changed.update(marks)
    diff = diff_note(before, res, patched, collapsed)
    return rec.model_copy(update={
        "page": res.page, "diff": diff,
        "changed": changed if (collapsing or diff is not None) else rec.changed,
        "collapse_applied": rec.collapse_applied or collapsing}), logs


def _open_events(rec: PageRecord, new_rec: PageRecord, page_id: str, *, now: datetime,
                 new_id: Ids, logs: tuple[SectionLog, ...] = ()) -> list[WikiEvent]:
    wiki: list[WikiEvent] = []
    if new_rec != rec:
        wiki.append(_wev(new_id, now, PageSaved(record=new_rec, action="opened")))
        wiki += logs_event(logs, new_id=new_id, now=now)
    wiki.append(_wev(new_id, now, PageOpened(page_id=page_id)))
    return wiki


def open_page_flow(repo: Repo, gw: CountingGateway | None, user_id: str, page_id: str, *,
                   now: datetime, new_id: Ids) -> PageOut:
    """POST /wiki/pages/{id}/open (0 LLM calls). Records `PageOpened` (the current page, D7) and,
    when the record changed, `PageSaved(action="opened")` before it.

    A commit conflict is retried up to OPEN_TRIES in all: when the page's record and the aspect
    seq are unchanged on the fresh load, the already computed record is committed again;
    otherwise it is recomputed on the fresh snapshot. After the last try the conflict propagates
    (409)."""
    snap = repo.load(user_id)
    _record(snap, page_id)
    if page_id in snap.state.trashed_pages:        # say so: nothing is updated or opened
        return page_out(snap.state, snap.kview(now), snap.state.pages[page_id], user_id=user_id)
    calls0 = gw.calls if gw is not None else 0
    rec = _record(snap, page_id)
    # a page a run is writing is shown as committed so far: no fold, no update (its run writes it)
    new_rec, logs = _open_update(snap, rec, now) if rec.has_body else (rec, ())
    wiki = _open_events(rec, new_rec, page_id, now=now, new_id=new_id, logs=logs)
    for attempt in range(OPEN_TRIES):
        try:
            # computed from K (fold / expand) and its own page: a write to either since the load
            # conflicts
            done = repo.commit(snap, Commit(wiki=wiki,
                                            depends=frozenset({K_SCOPE, f"page:{page_id}"})))
            break
        except WikiConflict:
            if attempt == OPEN_TRIES - 1:
                raise
            fresh = repo.load(user_id)
            fresh_rec = _record(fresh, page_id)
            if not (fresh_rec == rec and fresh.aspect_seq == snap.aspect_seq):
                rec = fresh_rec
                new_rec, logs = _open_update(fresh, rec, now) if rec.has_body else (rec, ())
            snap = fresh
            wiki = _open_events(rec, new_rec, page_id, now=now, new_id=new_id, logs=logs)
    calls = (gw.calls - calls0) if gw is not None else 0
    return page_out(done.state, done.kview(now), done.state.pages[page_id], calls=calls,
                    user_id=user_id)


def _section_flow(repo: Repo, user_id: str, page_id: str, sid: str, action: str, *, now: datetime,
                  new_id: Ids, change: Callable[[Page], dict | None],
                  extra: Callable[[Snapshot], list[WikiEvent]] | None = None) -> SectionOut:
    """0 calls. `change` returns the new section states, or None when nothing changes (no
    event is written). `extra(snapshot)`: more events for the same commit (when it changes)."""
    snap = repo.load(user_id)
    rec = _record(snap, page_id)
    if rec.page is None or all(s.id != sid for s in rec.page.outline.sections):
        raise LookupError(f"没有这一段：{sid}")
    states = change(rec.page)
    if states is not None:
        rec = rec.model_copy(update={"page": rec.page.model_copy(update={"states": states})})
        more = extra(snap) if extra is not None else []
        snap = repo.commit(snap, Commit(wiki=[_wev(new_id, now, PageSaved(record=rec,
                                                                          action=action)),
                                              *more]))
    return record_section_out(snap.state, snap.kview(now), rec, sid, user_id=user_id)


def set_fold(repo: Repo, user_id: str, page_id: str, sid: str, folded: bool, *, now: datetime,
             new_id: Ids) -> SectionOut:
    """Fold / unfold by hand: locks the fold state (auto-fold leaves it alone afterwards).
    Not foldable → WikiInvalid (422); unknown section → LookupError (404)."""
    def change(page: Page) -> dict | None:
        s = next(x for x in page.outline.sections if x.id == sid)
        if s.role not in FOLDABLE_ROLES:
            raise WikiInvalid(f"这一段不能折叠：{sid}")
        if page.state(sid).folded == folded:
            return None
        return toggle_fold(page.outline, page.states, sid)

    def unfold_signal(snap: Snapshot) -> list[WikiEvent]:      # an unfold by hand → `expand`
        return [] if folded else [signal_event(snap.state, "expand", now=now, new_id=new_id,
                                               page_id=page_id, section_id=sid)]
    return _section_flow(repo, user_id, page_id, sid, "fold", now=now, new_id=new_id,
                         change=change, extra=unfold_signal)


def set_pin(repo: Repo, user_id: str, page_id: str, sid: str, pinned: bool, *, now: datetime,
            new_id: Ids) -> SectionOut:
    """Pin / unpin: a pinned section is never handed to the writer by a K change."""
    def change(page: Page) -> dict | None:
        if page.state(sid).pinned == pinned:
            return None
        return set_pinned(page.outline, page.states, sid, pinned)
    return _section_flow(repo, user_id, page_id, sid, "pin", now=now, new_id=new_id,
                         change=change)


def mark_done(repo: Repo, user_id: str, page_id: str, *, now: datetime,
              new_id: Ids, checks: tuple[OutlineSection, ...] = (),
              expected_page: PageRecord | None = None) -> DoneOut:
    """「这一页我学完了」: done=True and a usage record (WikiConflict: no body yet, or in the
    trash); the page's cards start their schedule (`PageRead`, no evidence). Nothing else runs."""
    from evowiki.wikiapp.review import sources  # review imports kchange, which imports this module
    snap = repo.load(user_id)
    rec = _record(snap, page_id)
    if expected_page is not None and rec != expected_page:
        raise WikiConflict("这一页在出题时发生了变化，请重试")
    # only a written page can be finished (D39): a placeholder or a page in the trash is not read
    if page_id in snap.state.trashed_pages:
        raise WikiConflict("这一页在回收站里，要先重新写")
    if not rec.has_body:              # not written, or a run is writing it / left it part-way
        raise WikiConflict("这一页还没写好，写好后再学完")
    wiki: list[WikiEvent] = []
    if rec.done and not checks:
        return DoneOut()
    if checks:
        outline = rec.page.outline.model_copy(update={
            "sections": (*rec.page.outline.sections, *checks)})
        rec = rec.model_copy(update={"page": rec.page.model_copy(update={"outline": outline})})
    if not rec.done or checks:
        wiki.append(_wev(new_id, now, PageSaved(record=rec.model_copy(update={"done": True}),
                                                action="done")))
    if not rec.done:
        wiki.append(_wev(new_id, now, UsageAdded(record=UsageRecord(action=DONE_USAGE,
                                                                    page=page_id))))
    known = {f"{c}.{a}" for c in snap.k.concept_ids() for a in snap.k.aspects(c)}
    cells = {c for s in sources(snap.state) if s.page_id == page_id for c in s.cells if c in known}
    aspect = [PageRead(event_id=new_id("aev"), ts=now, cell=c) for c in sorted(cells)
              if not rec.done and snap.k.memory(c).last_tested is None]
    repo.commit(snap, Commit(aspect=aspect, wiki=wiki))
    return DoneOut()
