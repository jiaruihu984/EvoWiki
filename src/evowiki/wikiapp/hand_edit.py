"""A hand edit in a note becomes the page's text (user ruling 2026-11-04: 「手改内容和那一段直接不改，进正文」).

POST /wiki/pages/{id}/hand-edit: the plugin sends the sections the user changed (the reading text
`body` and the note text `note_body`, `SectionText`) and those he deleted. Each changed section is
written and locked (`SectionState.pinned`, reused: no system write changes it, automatic folding leaves
it alone, `goals.fold.keep_locked`), a deleted one leaves the outline. One `HandEdited` event holds what
undo puts back, section by section, so 撤销 still works after other sections of the page changed. An edit
is also one `note_edit` signal and a usage record: what the user did, not a complaint
(`teach.signal_kinds`, `teach.localize`). 0 LLM calls.

The edit is also a row of the action ledger in the same commit (`ledger`), so 个性化 · 活动记录 lists it
and its 撤销 is `undo_hand_edit` (`bot.executors_hand`); the page's top line says so only right after.

Sending the same edit again changes nothing (the plugin resends when the note could not be written
after the server took the edit)."""

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from evowiki.goals.contracts import OutlineSection, SectionRole, SectionText
from evowiki.goals.fold import Page, states_for
from evowiki.wikiapp.events import (
    HandEditChange,
    HandEdited,
    HandEditMark,
    HandEditUndone,
    PageRecord,
    PageSaved,
    UsageAdded,
    UsageRecord,
    WikiEvent,
)
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.page_version import text_fingerprint
from evowiki.wikiapp.refine_apply import kept_marks
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict, WikiInvalid
from evowiki.wikiapp.sectionlog import partial_write
from evowiki.wikiapp.signals import signal_event
from evowiki.wikiapp.views import page_out
from evowiki.wikiapp.wiki_schemas import HandEditIn, HandEditOut, HandEditSectionIn

if TYPE_CHECKING:
    from evowiki.bot.actions import ActionLedger

KIND = "hand_edit"                # the ledger row
SAVE_ACTION = "hand_edit"
UNDO_ACTION = "hand_edit_undo"
USAGE = "改了笔记"
COMMIT_TRIES = 3
GONE = "这一段已经不在这一页了"
ALL_GONE = "一页不能把所有段都删掉"
CHECK_EDIT = "确认题不能手改，只能整段删掉"
WRITING = "这一页还没写完，写完后才能改"
STALE = "这一节服务器上改过，没有覆盖"


def _record(snap: Snapshot, page_id: str) -> PageRecord:
    rec = snap.state.pages.get(page_id)
    if rec is None:
        raise LookupError(f"没有这个页面：{page_id}")
    return rec


def _same(s: OutlineSection, t: SectionText | None, e: HandEditSectionIn, pinned: bool,
          folded: bool) -> bool:
    return (t is not None and pinned and t.body == e.body and t.note_body == e.note_body
            and (e.heading is None or e.heading == s.heading)
            and (e.cue is None or e.cue == t.cue)
            and (e.folded is None or e.folded == folded))


def _written(edit_id: str, s: OutlineSection, t: SectionText | None,
             e: HandEditSectionIn) -> SectionText:
    """The new text. A recall card the user did not touch stays: the Bot replaces it when the text no longer answers it
    (`bot.card_stale`), so a section is never left without its card in between. A cue he typed is his fold title: the old
    answer does not answer it, so that section has no card."""
    cue = e.cue if e.cue is not None else t.cue if t is not None else ""
    answer = t.answer if t is not None and cue == t.cue else ""
    return SectionText(section_id=s.id, cue=cue, answer=answer, body=e.body,
                       note_body=e.note_body, cache_key=f"hand:{edit_id}:{s.id}")


def _applied(rec: PageRecord, body: HandEditIn, edit_id: str, now: datetime,
             running: bool = True) -> tuple[PageRecord, list[HandEditChange]] | None:
    """The record with the edit in it and what undo needs; None when the page already holds it. `running`: a run is
    writing the page right now; a run stopped part-way left its mark and its sections, and those are his to edit."""
    page = rec.page
    if page is None or (rec.writing is not None and running):
        raise WikiConflict(WRITING)
    by_id = {s.id: s for s in page.outline.sections}
    edits = {e.section_id: e for e in body.sections}
    if any(sid not in by_id for sid in edits):
        raise WikiConflict(GONE)
    if any(by_id[sid].role is SectionRole.CHECK for sid in edits):
        raise WikiInvalid(CHECK_EDIT)
    # a section with no text yet (a run stopped part-way) is not in the note: its absence there is no deletion
    deletions = {e.section_id: e for e in body.removed}
    removed = {sid for sid in deletions if sid in by_id and (
        by_id[sid].role is SectionRole.CHECK or ((t := page.texts.get(sid)) is not None and t.body.strip()))}
    if len(removed) == len(by_id):
        raise WikiInvalid(ALL_GONE)
    secs: list[OutlineSection] = []
    texts, states = dict(page.texts), dict(page.states)
    changes: list[HandEditChange] = []
    prev: str | None = None
    for s in page.outline.sections:
        t, st = page.texts.get(s.id), page.state(s.id)
        e = edits.get(s.id)
        if s.id in removed:
            if deletions[s.id].fingerprint != text_fingerprint(s.heading, t.body if t is not None else ""):
                raise WikiConflict(STALE, code="hand_edit_stale")
            changes.append(HandEditChange(section_id=s.id, before_section=s, before_text=t,
                                          before_state=st, after_section=prev))
            texts.pop(s.id, None)
            states.pop(s.id, None)
            prev = s.id          # 撤销 brings them back in order, each after the one before it
            continue
        if e is not None and not _same(s, t, e, st.pinned, st.folded):
            if e.fingerprint is not None and e.fingerprint != text_fingerprint(
                    s.heading, t.body if t is not None else ""):
                raise WikiConflict(STALE, code="hand_edit_stale")
            new = _written(edit_id, s, t, e)
            changes.append(HandEditChange(section_id=s.id, before_section=s, before_text=t,
                                          before_state=st, after_section=prev, after_text=new))
            texts[s.id] = new
            states[s.id] = st.model_copy(update={
                "pinned": True, "folded": st.folded if e.folded is None else e.folded})
            if e.heading is not None:
                s = s.model_copy(update={"heading": e.heading})
        secs.append(s)
        prev = s.id
    if not changes:
        return None
    outline = page.outline.model_copy(update={"sections": tuple(secs)})
    new_page = page.model_copy(update={"outline": outline, "texts": texts,
                                       "states": states_for(outline, states)})
    written = tuple(c.section_id for c in changes if c.after_text is not None)
    gone = tuple(c.before_section.heading for c in changes if c.after_text is None)
    untouched = {sid for sid in texts if sid not in written}
    sources, issues = kept_marks(rec, untouched, new_page)
    touched = {c.section_id for c in changes}
    return rec.model_copy(update={
        "page": new_page, "sources": tuple(sources), "cite_issues": tuple(issues),
        "changed": {k: v for k, v in rec.changed.items() if k not in touched},
        "hand_edit": HandEditMark(edit_id=edit_id, at=now, sections=written, removed=gone)}), changes


def _key(edit_id: str) -> str:
    return f"hand:{edit_id}"


def _row_summary(title: str, new: PageRecord) -> str:
    assert new.hand_edit is not None and new.page is not None
    heading = {s.id: s.heading for s in new.page.outline.sections}
    parts = "、".join(f"「{heading[s]}」" for s in new.hand_edit.sections)
    gone = "、".join(f"「{h}」" for h in new.hand_edit.removed)
    return f"在「{title}」里" + "，".join(x for x in (f"改了{parts}" if parts else "",
                                                     f"删除了{gone}" if gone else "") if x)


def _ledger_row(ledger: "ActionLedger", user_id: str, page_id: str, edit_id: str,
                summary: str) -> Callable[[Any], None]:
    """The edit's 活动记录 row, written in the page's commit."""
    return lambda conn: ledger.record_in(
        conn, user_id, kind=KIND, scope="page_body", source="user", summary=summary,
        content={"edit_id": edit_id}, key=_key(edit_id), undo={"edit_id": edit_id},
        page_id=page_id)


def hand_edit_flow(repo: Repo, user_id: str, page_id: str, body: HandEditIn, *, now: datetime,
                   new_id: Ids, ledger: "ActionLedger | None" = None, running: Callable[[], bool] | None = None) -> HandEditOut:
    """404 unknown page; 409 a section is gone or a run is writing the page (`running`: whether one is, the mark alone
    when not given); 422 the edit deletes
    every section or changes a check. Nothing is written in any of those."""
    edit_id = new_id("he")
    for attempt in range(COMMIT_TRIES):
        snap = repo.load(user_id)
        rec = _record(snap, page_id)
        done = _applied(rec, body, edit_id, now, running is None or running())
        if done is None:
            return HandEditOut(page=page_out(snap.state, snap.kview(now), rec, user_id=user_id))
        new, changes = done
        written = new.hand_edit.sections if new.hand_edit is not None else ()
        new, logs = partial_write(snap, new, written, kind="hand", ref=edit_id, producer="edit",
                                  now=now, new_id=new_id)
        only = changes[0].section_id if len(changes) == 1 and changes[0].after_text else None
        sig = signal_event(snap.state, "note_edit", now=now, new_id=new_id, page_id=page_id,
                           section_id=only, text=body.summary)
        wiki = [
            WikiEvent(event_id=new_id("wev"), ts=now, body=PageSaved(record=new,
                                                                     action=SAVE_ACTION)),
            WikiEvent(event_id=new_id("wev"), ts=now, body=HandEdited(
                edit_id=edit_id, page_id=page_id, signal_id=sig.body.signal.signal_id,  # type: ignore[union-attr]
                changes=tuple(changes), prev_sources=rec.sources, prev_issues=rec.cite_issues)),
            *logs, sig,
            WikiEvent(event_id=new_id("wev"), ts=now, body=UsageAdded(record=UsageRecord(
                action=USAGE, page=page_id, text=body.summary)))]
        try:
            extra = [] if ledger is None else [_ledger_row(
                ledger, user_id, page_id, edit_id, _row_summary(rec.title, new))]
            done_snap = repo.commit(snap, Commit(wiki=wiki, extra=extra,
                                                 depends=frozenset({f"page:{page_id}"})))
        except WikiConflict:
            if attempt == COMMIT_TRIES - 1:
                raise
            continue
        cur = done_snap.state.pages[page_id]
        return HandEditOut(edit_id=edit_id, page=page_out(done_snap.state, done_snap.kview(now),
                                                          cur, user_id=user_id))
    raise AssertionError("unreachable")


def _row_undone(ledger: "ActionLedger", edit_id: str, now: datetime) -> Callable[[Any], None]:
    return lambda conn: ledger.set_status_in(conn, _key(edit_id), "undone", at=now, missing_ok=True)


def _restored(page: Page, ed: HandEdited) -> Page:
    secs = list(page.outline.sections)
    texts, states = dict(page.texts), dict(page.states)
    for c in ed.changes:
        ids = [s.id for s in secs]
        if c.section_id in ids:
            secs[ids.index(c.section_id)] = c.before_section
        else:
            at = ids.index(c.after_section) + 1 if c.after_section in ids else 0
            secs.insert(at, c.before_section)
        if c.before_text is not None:
            texts[c.section_id] = c.before_text
        else:
            texts.pop(c.section_id, None)
        if c.before_state is not None:
            states[c.section_id] = c.before_state
    outline = page.outline.model_copy(update={"sections": tuple(secs)})
    return page.model_copy(update={"outline": outline, "texts": texts,
                                   "states": states_for(outline, states)})


def undo_hand_edit(repo: Repo, user_id: str, page_id: str, edit_id: str, *, now: datetime,
                   new_id: Ids, ledger: "ActionLedger | None" = None) -> HandEditOut:
    """撤销: the edited sections as they were (text, heading, lock, place), other sections as they
    are now. 404 unknown edit; 409 already undone, or a section of it changed since. `ledger`: its
    活动记录 row moves in the same commit (the top line's 撤销; the row's own 撤销 has the ledger do it)."""
    for attempt in range(COMMIT_TRIES):
        snap = repo.load(user_id)
        ed = snap.state.hand_edits.get(edit_id)
        if ed is None or ed.page_id != page_id:
            raise LookupError(f"没有这次改动：{edit_id}")
        if edit_id in snap.state.hand_edits_undone:
            raise WikiConflict("这次改动已经撤销了")
        rec = _record(snap, page_id)
        page = rec.page
        if page is None:
            raise WikiConflict(GONE)
        for c in ed.changes:
            on_page = any(s.id == c.section_id for s in page.outline.sections)
            same = (page.texts.get(c.section_id) == c.after_text if c.after_text is not None
                    else not on_page)
            if not same:
                raise WikiConflict("这一段在这次改动之后又改过，不能直接撤销")
        cited = {(s.source_id, s.title) for s in rec.sources}
        new = rec.model_copy(update={
            "page": _restored(page, ed),
            "sources": (*rec.sources, *(s for s in ed.prev_sources
                                        if (s.source_id, s.title) not in cited)),
            "cite_issues": (*rec.cite_issues, *(i for i in ed.prev_issues
                                                if i not in rec.cite_issues)),
            "hand_edit": None})
        try:
            done = repo.commit(snap, Commit(wiki=[
                WikiEvent(event_id=new_id("wev"), ts=now, body=PageSaved(record=new,
                                                                         action=UNDO_ACTION)),
                WikiEvent(event_id=new_id("wev"), ts=now, body=HandEditUndone(edit_id=edit_id))],
                extra=[] if ledger is None else [_row_undone(ledger, edit_id, now)],
                depends=frozenset({f"page:{page_id}"})))
        except WikiConflict:
            if attempt == COMMIT_TRIES - 1:
                raise
            continue
        return HandEditOut(page=page_out(done.state, done.kview(now), done.state.pages[page_id],
                                         user_id=user_id))
    raise AssertionError("unreachable")
