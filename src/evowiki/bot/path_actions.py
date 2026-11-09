"""The Bot's learning-path edits (B5b, spec 2026-10-15 §1.3 / §1.5): ledger action `path_edit`,
scope `path` (「学习路径顺序」, default auto_undo). The weekly review moves the page that teaches a
weak prerequisite right before the next page, or inserts a page for it; never deletes, never
touches the user's own path edits.

Exactly once and version-checked: the executor stores the edit through the same path-edit commit
the user's edits use (`wikiapp.path_edits.save_path`), with the ledger's effect in that commit;
the path record must still be the one the proposal saw (`page_version` = `path_version`), else the
action fails with a reason and nothing is overwritten. Undo restores the record from before the
edit only while the path has not changed since (else refused, like an assistant edit's undo)."""

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from evowiki.bot.actions import ActionRefused, ActionRow
from evowiki.store import bot as store
from evowiki.wikiapp.events import PathRecord
from evowiki.wikiapp.path_edits import bot_edited, save_path
from evowiki.wikiapp.path_order import shown, stored_index
from evowiki.wikiapp.repo import Repo, WikiConflict, WikiInvalid
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.views import page_title

log = logging.getLogger(__name__)

KIND = "path_edit"
SCOPE = "path"
STALE = "学习路径在提议之后改过了，这条调整已作废，不会覆盖你的修改"
GONE = "这条学习路径已经没有了"
UNDO_STALE = "学习路径在这之后又改过了，不能撤销"


def path_version(rec: PathRecord) -> str:
    return hashlib.sha256(rec.model_dump_json().encode()).hexdigest()[:20]


def order_titles(state: WikiState, rec: PathRecord) -> list[str]:
    """The shown pages' titles, in order."""
    out = []
    for i in shown(rec):
        pid = rec.page_ids[stored_index(rec, i.key)] if i.page is not None else None
        out.append(page_title(state, pid) if pid in state.pages else i.title)
    return out


def path_key(path_id: str, week: str) -> str:
    """One Bot edit per path and ISO week."""
    return f"path:{path_id}:{week}"


def proposal(state: WikiState, rec: PathRecord, payload: dict) -> dict[str, Any] | None:
    """`ActionLedger.propose_in` arguments for a goal_review payload of situation prereq_gap
    (move) / prereq_missing (add); None when the edit no longer fits the path."""
    sit = payload.get("situation")
    if sit == "prereq_gap":
        content = {"path_id": rec.path.path_id, "op": "move", "page_key": payload["page_key"],
                   "before_key": payload["before_key"], "node_id": payload["node_id"],
                   "title": payload["name"]}
        summary = f"把「{payload.get('page_title') or payload['name']}」挪到「{payload['for_title']}」前面"
    elif sit == "prereq_missing":
        content = {"path_id": rec.path.path_id, "op": "add", "node_id": payload["node_id"],
                   "title": payload["name"], "before_key": payload["before_key"]}
        summary = f"在「{payload['for_title']}」前面加一页「{payload['name']}」"
    else:
        return None
    try:
        after = bot_edited(state, rec, content)
    except (WikiConflict, WikiInvalid, LookupError):
        log.info("bot path edit for %s no longer fits; card only", rec.path.path_id)
        return None
    content["order_before"] = order_titles(state, rec)
    content["order_after"] = order_titles(state, after)
    return {"kind": KIND, "scope": SCOPE, "source": "bot", "summary": summary,
            "why": f"「{payload['for_title']}」要用到「{payload['name']}」，它还没打牢",
            "content": content, "key": path_key(rec.path.path_id, payload.get("week", "")),
            "page_version": path_version(rec)}


class PathEditExecutor:
    def __init__(self, repo: Repo, clock: Callable[[], datetime], new_id: Callable[[str], str]):
        self._repo, self._clock, self._new_id = repo, clock, new_id

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        snap = self._repo.load(user_id)
        rec = snap.state.paths.get(action.content.get("path_id", ""))
        if rec is None:
            raise ActionRefused(GONE)
        if path_version(rec) != action.page_version:
            raise ActionRefused(STALE)
        try:
            new = bot_edited(snap.state, rec, action.content)
        except (WikiConflict, WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)[:200]) from exc
        undo = {"path_id": rec.path.path_id, "before": rec.model_dump(mode="json"),
                "after": path_version(new)}

        def write_undo(conn) -> None:       # in the path's commit: an executed edit has its undo
            store.set_action_undo(conn, action.action_id, undo)
        try:
            save_path(self._repo, snap, new, now=self._clock(), new_id=self._new_id,
                      extra=[effect, write_undo], source="bot")
        except WikiConflict as exc:
            raise ActionRefused(STALE) from exc
        return {"path_id": rec.path.path_id, "order": [i.key for i in shown(new)]}

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = action.undo
        if not u:
            raise ActionRefused("这条调整没有保存撤销数据，不能撤销")
        snap = self._repo.load(user_id)
        rec = snap.state.paths.get(u["path_id"])
        if rec is None:
            raise ActionRefused(GONE)
        if path_version(rec) != u["after"]:
            raise ActionRefused(UNDO_STALE)
        before = PathRecord.model_validate(u["before"])

        def mark_undone(conn) -> None:      # the path and the ledger row move together
            store.set_action_status(conn, action.action_id, status="undone", at=self._clock(),
                                    from_statuses=("executed",))
        try:
            save_path(self._repo, snap, before, now=self._clock(), new_id=self._new_id,
                      extra=[mark_undone], source="bot")
        except WikiConflict as exc:
            raise ActionRefused(UNDO_STALE) from exc
        return {"path_id": u["path_id"], "order": [i.key for i in shown(before)]}
