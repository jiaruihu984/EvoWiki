"""The Bot's idle page writes in the action ledger (assistant-actions spec 2026-10-15 §4, Task 7).

`bot_write` rows are of two shapes:
- a page the Bot wrote (recorded `executed` by `wikiapp.write_queue.WriteQueue.after_write`, scope
  `new_page`, `auto_undo`, `result` = {cost_usd, calls}, `undo` = {page_id}). `undo` puts the page back
  to unwritten: it goes to the trash, and a trashed page is never picked by the idle Bot again
  (`no_auto`); the learner's 「重新写」 (a write request) writes it again and takes it out of the trash;
- a proposal 「要写 N 页」 (the learner's level for new pages is `ask`): `content["picks"]` are the
  pages; one approval creates and queues them all as idle writes."""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from evowiki.bot.actions import ActionRefused, ActionRow
from evowiki.store import bot as store
from evowiki.wikiapp.events import PagesTrashed, UsageAdded, UsageRecord, WikiEvent
from evowiki.wikiapp.repo import Commit, Repo

log = logging.getLogger(__name__)

KIND = "bot_write"
WriteBatch = Callable[[str, list[dict[str, Any]]], list[str]]
Cancel = Callable[[str, list[str]], Any]


class BotWriteExecutor:
    def __init__(self, repo: Repo, clock: Callable[[], datetime], new_id: Callable[[str], str],
                 write_batch: WriteBatch, cancel: Cancel,
                 forget: Callable[[str, str], Any] = lambda _u, _p: None,
                 merged_by: Callable[[str, str], bool] = lambda _u, _p: False):
        self._merged_by = merged_by
        self._repo, self._clock, self._new_id = repo, clock, new_id
        self._write_batch, self._cancel, self._forget = write_batch, cancel, forget

    def _wev(self, body) -> WikiEvent:
        return WikiEvent(event_id=self._new_id("wev"), ts=self._clock(), body=body)

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        picks = action.content.get("picks")
        if not isinstance(picks, list) or not picks or not all(isinstance(p, dict) for p in picks):
            raise ActionRefused("这条记录不是一批要生成的页面")
        snap = self._repo.load(user_id)
        # the approval itself: one marker event carries the ledger's effect (the pages follow)
        self._repo.commit(snap, Commit(wiki=[self._wev(UsageAdded(record=UsageRecord(
            action="bot_write_batch", text=action.action_id)))], extra=[effect]))
        try:
            run_ids = self._write_batch(user_id, picks)
        except Exception as exc:        # the approval stands; the pages' own runs show the problem
            log.exception("bot_write batch %s for %s failed", action.action_id, user_id)
            return {"run_ids": [], "write_error": f"{type(exc).__name__}: {exc}"[:300]}
        return {"run_ids": run_ids, "requested": len(run_ids)}

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    def undo(self, user_id: str, action: ActionRow) -> dict:
        page_id = (action.undo or {}).get("page_id")
        if not isinstance(page_id, str):
            raise ActionRefused("这条记录没有保存撤销数据，不能撤销")
        snap = self._repo.load(user_id)
        rec = snap.state.pages.get(page_id)
        if rec is None:
            raise ActionRefused("这一页已经不在了")
        if rec.done:
            raise ActionRefused(f"「{rec.title}」已经学完了，不能撤销")
        if self._merged_by(user_id, page_id):
            # its undo is the merge's own (the originals come back with it), which the chat can
            # undo as well: the reason names that change, never another panel
            raise ActionRefused(f"「{rec.title}」是合并出来的页，要撤销的是那次合并，"
                                "原来的页会一起放回来")

        def mark(conn) -> None:
            store.set_action_status(conn, action.action_id, status="undone", at=self._clock(),
                                    from_statuses=("executed",))
        self._cancel(user_id, [page_id])
        wiki = ([] if page_id in snap.state.trashed_pages else
                [self._wev(PagesTrashed(page_ids=(page_id,), trashed=True))])
        if wiki:
            self._repo.commit(snap, Commit(wiki=wiki, extra=[mark]))
        self._forget(user_id, page_id)       # opening the trashed page must not replay the old run
        return {"trashed": page_id}
