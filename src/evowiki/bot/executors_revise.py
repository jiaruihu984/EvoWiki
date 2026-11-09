"""`page_revise` executor (materials phase 6, spec 2026-10-20 §7 D): a whole-page draft from
materials that the learner confirms or rejects as a whole (nothing to pick: the draft follows the
materials, with a ⟦和资料不同：…⟧ line where the writer thinks a material is wrong).

The row is the draft (`wikiapp.revise`): `content.state` is "writing" while the writer run fills it
(not approvable yet: `check_approvable` raises NotDecidable under the row lock, the row stays
`proposed`), "ready" once `content["draft"]` holds a `RevisionDraft`, "failed" when the run could not
write one. There is no `revise`: the draft is not editable, so the ledger refuses an approval that
carries one. `execute`
swaps the whole page in ONE commit with the ledger's effect, the undo data and the draft's assets,
checking the page's fingerprint against the row's `page_version` on every attempt (a page another
flow changed is never overwritten). `undo` puts the page back while it is still the swap's result.
User notes (`PageRecord.page is None`) are never changed."""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from evowiki.bot.actions import ActionRefused, ActionRow, AlreadyExecuted, NotDecidable
from evowiki.deep.store import AssetStore
from evowiki.store import bot as store
from evowiki.wikiapp.events import PageRecord, PageSaved, UsageAdded, UsageRecord, WikiEvent
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.refine_apply import make_assets
from evowiki.wikiapp.repo import Commit, Repo, WikiConflict
from evowiki.wikiapp.revise import (
    SAVE_ACTION,
    UNDO_ACTION,
    RevisionDraft,
    page_sources_of,
    revised_record,
)
from evowiki.wikiapp.sectionlog import written_logs

log = logging.getLogger(__name__)

COMMIT_TRIES = 3
WRITING = "草稿还在写"
FAILED = "这条草稿没写成"
USER_NOTE = "这是你自己的笔记，EvoWiki 不改"
PAGE_GONE = "这一页已经不在了"
NO_ASSETS = "没有交互资源库，不能提交带交互的草稿"
NO_CHECK = "草稿里有交互没有通过渲染检查，这一页没有改"
CONFLICT = "这一页一直在被别的流程改，这次没有改，请再试一次"
INTERNAL = "出错了，这一页没有改"
UNDO_STALE = "这一页之后又改过了，不能撤销"
NO_UNDO = "这条修改没有保存撤销数据，不能撤销"


def _draft_of(action: ActionRow) -> RevisionDraft:
    """The row's draft; NotDecidable while it is being written, ActionRefused when it failed or
    does not parse."""
    state = action.content.get("state")
    if state == "writing":
        raise NotDecidable(WRITING)
    if state != "ready" or action.content.get("draft") is None:
        raise ActionRefused(FAILED)
    try:
        return RevisionDraft.model_validate(action.content["draft"])
    except ValidationError as exc:
        log.warning("page_revise %s: draft does not parse: %s", action.action_id, exc)
        raise ActionRefused(FAILED) from exc


class PageReviseExecutor:
    def __init__(self, repo: Repo, assets: AssetStore | None, clock: Callable[[], datetime],
                 new_id: Callable[[str], str]):
        self._repo, self._assets, self._clock, self._new_id = repo, assets, clock, new_id

    def check_approvable(self, action: ActionRow) -> None:
        """Called by the ledger under the row lock: a draft still being written cannot be
        approved (409 「草稿还在写」); the row stays `proposed`."""
        if action.content.get("state") == "writing":
            raise NotDecidable(WRITING)

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        rd = _draft_of(action)
        try:
            return self._execute(user_id, action, rd, effect)
        except (ActionRefused, AlreadyExecuted, NotDecidable):
            raise                  # AlreadyExecuted: another approve won, the commit rolled back
        except Exception as exc:   # never leak internals
            log.exception("executing page_revise %s failed", action.action_id)
            raise ActionRefused(INTERNAL) from exc

    def _execute(self, user_id: str, action: ActionRow, rd: RevisionDraft,
                 effect: Callable[[Any], None]) -> dict:
        draft = rd.draft
        if draft.needs_assets and self._assets is None:
            raise ActionRefused(NO_ASSETS)
        checks = {c.interactive_id: c for c in rd.checks}
        placed = [*(i.interactive_id for i in draft.interactives),
                  *(b.block_id for b in draft.blocks)]
        if any(iid not in checks or not checks[iid].ok for iid in placed):
            raise ActionRefused(NO_CHECK)
        now = self._clock()
        job_id = f"revise:{action.action_id}"
        stored = make_assets(draft, checks, user_id=user_id, job_id=job_id, now=now,
                             new_id=self._new_id)
        asset_writers = [self._assets.writer(stored)] if stored and self._assets else []
        undo: dict[str, Any] = {}

        def write_undo(conn) -> None:      # in the page's commit: an executed swap always has it
            store.set_action_undo(conn, action.action_id, dict(undo))
        for attempt in range(1, COMMIT_TRIES + 1):
            snap = self._repo.load(user_id)
            rec = snap.state.pages.get(rd.page_id)
            if rec is None:
                raise ActionRefused(PAGE_GONE)
            if rec.page is None:
                raise ActionRefused(USER_NOTE, public_message="EvoWiki does not modify your own notes.", code="user_note_readonly")
            version = page_fingerprint(rec)
            if version != action.page_version or version != rd.base_version:
                raise ActionRefused("", stale=True)
            try:
                if rd.scope:
                    from evowiki.wikiapp.refine_apply import written_record
                    if set(rd.scope) != {s.section_id for s in draft.sections}:
                        raise ValueError("scoped revision changes another section")
                    revised = written_record(rec, draft, stored, now=now, job_id=job_id, model="",
                                             kind="rewrite", sources=page_sources_of(rd.sources),
                                             cite_issues=rd.cite_issues)
                else:
                    revised = revised_record(rec, draft, stored, now=now, job_id=job_id, model="",
                                             rebuild=rd.rebuild, sources=page_sources_of(rd.sources),
                                             cite_issues=rd.cite_issues)
            except ValueError as exc:
                log.warning("page_revise %s does not fit the page: %s", action.action_id, exc)
                raise ActionRefused(FAILED) from exc
            undo.clear()
            undo.update({"page_id": rec.page_id, "prev_record": rec.model_dump(mode="json"),
                         "after_version": page_fingerprint(revised)})
            events = [
                WikiEvent(event_id=self._new_id("wev"), ts=now,
                          body=PageSaved(record=revised, action=SAVE_ACTION)),
                *written_logs(snap, revised, producer="writer", now=now,
                              new_id=self._new_id),
                WikiEvent(event_id=self._new_id("wev"), ts=now, body=UsageAdded(
                    record=UsageRecord(action=SAVE_ACTION, page=rec.page_id)))]
            try:
                self._repo.commit(snap, Commit(wiki=events,
                                               extra=[effect, write_undo, *asset_writers]))
            except WikiConflict:
                if attempt == COMMIT_TRIES:
                    break
                continue
            return {"page_id": rec.page_id, "sections": [s.section_id for s in draft.sections],
                    "assets": [a.meta.asset_id for a in stored],
                    "wiki_event_ids": [e.event_id for e in events]}
        raise ActionRefused(CONFLICT)

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None                # the draft is dropped with the row; the page never changed

    def undo(self, user_id: str, action: ActionRow) -> dict:
        """The page back to what it was before the swap, while the page is still the swap's
        result (refused otherwise: nothing newer is overwritten). Page and ledger row move in one
        commit."""
        data = action.undo
        if not data or "prev_record" not in data:
            raise ActionRefused(NO_UNDO)
        prev = PageRecord.model_validate(data["prev_record"])

        def mark_undone(conn) -> None:
            store.set_action_status(conn, action.action_id, status="undone", at=self._clock(),
                                    from_statuses=("executed",))
        for attempt in range(1, COMMIT_TRIES + 1):
            snap = self._repo.load(user_id)
            rec = snap.state.pages.get(prev.page_id)
            if rec is None:
                raise ActionRefused(PAGE_GONE)
            if page_fingerprint(rec) != data["after_version"]:
                raise ActionRefused(UNDO_STALE)
            # the page's content goes back, with the materials and citation check it came from;
            # what moved beside it since (check answers, done, folder) stays as it is now
            back = rec.model_copy(update={"page": prev.page, "changed": prev.changed,
                                          "written": prev.written, "revisions": prev.revisions,
                                          "feedback": prev.feedback, "sources": prev.sources,
                                          "cite_issues": prev.cite_issues})
            event = WikiEvent(event_id=self._new_id("wev"), ts=self._clock(),
                              body=PageSaved(record=back, action=UNDO_ACTION))
            try:
                self._repo.commit(snap, Commit(wiki=[event], extra=[mark_undone]))
            except WikiConflict:
                if attempt == COMMIT_TRIES:
                    break
                continue
            return {"restored": prev.page_id}
        raise ActionRefused(CONFLICT)
