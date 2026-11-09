"""Teaching preferences in the action ledger (stage 3 T6 / T7; rule provenance contract §7 #4 / #5 /
#17 / #24).

`PreferenceExecutor` — both preference kinds `wikiapp.preferences.land_preference` writes:
- `stated_rule`: a rule he stated in this message, landed at once (the row is written executed by
  `land_preference`, never approved here). Undo removes it (`revoked`, via ledger_undo) and puts
  back the rule it superseded (`reaccepted`).
- `rule`: a rule waiting for his confirmation (a pending spec item + this proposed row). Confirming
  (the card's 确认, or `confirm` after `offer`) makes the item his (`authority` pending → user:
  `reaccepted`) and takes the rule it `replaces` out of force, in one version; rejecting removes the
  pending item (`rejected`, via ledger_reject); undoing a confirmed one removes it (`revoked`) and
  restores the superseded one.
No page is marked out of date by any of this: a new preference applies to pages written from now
on. Restyling old pages is its own draft:

`RestyleExecutor` — `restyle_pages`: the pages the learner confirmed are marked out of date
(`PagesStaleSet`, as the retired RSI-B's changes did), so opening one rewrites it under the current spec
(`wikiapp.stale.rewrite_due`). Undo clears the marks this row set that are still its own."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from evowiki.bot.actions import ActionRefused
from evowiki.bot.executors_assistant import _write_undo
from evowiki.bot.executors_graph import _Graph, _refusing
from evowiki.store.bot import ActionRow
from evowiki.teach.contracts import SpecItem
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.events import PagesStaleSet, StaleMark, WikiEvent
from evowiki.wikiapp.preferences import PENDING_KIND, STATED_KIND
from evowiki.wikiapp.repo import Commit, Snapshot
from evowiki.wikiapp.teach import commit_spec

GONE = "这条规则之后被改过或删掉了，不能撤销"
NOT_PENDING = "这条规则已经不在等你确认了"


def _item(spec, item_id: str) -> SpecItem | None:
    return next((i for i in spec.items if i.item_id == item_id), None)


def _restore(old: SpecItem) -> tspec.AddRule:
    return tspec.AddRule(old.item_id, old.text, old.scope, old.domain, origin=old.origin,
                         authority=old.authority, origin_ref=old.origin_ref)


class PreferenceExecutor(_Graph):
    def revise(self, action: ActionRow, draft: Any) -> dict[str, Any]:
        if action.kind != PENDING_KIND:
            raise ActionRefused("这条偏好已经记下了")
        if not isinstance(draft, dict) or set(draft) != {"text"} or not isinstance(draft["text"], str):
            raise ActionRefused("请填写规则内容")
        with _refusing():
            text = tspec._text(draft["text"])
        return {**action.content, "text": text}

    def _commit(self, snap: Snapshot, change: Callable, action: ActionRow, via: str,
                extra: list) -> None:
        commit_spec(self._repo, snap, change, actor="user", via=via,  # type: ignore[arg-type]
                    action="guideline", origin=action.action_id, now=self._clock(),
                    new_id=self._new_id, commit_extra=extra)

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        if action.kind == STATED_KIND:
            raise ActionRefused("这条偏好已经记下了")
        c = action.content
        item_id = c.get("item_id")
        with _refusing():
            snap = self._repo.load(user_id)
            spec = tspec.effective_spec(snap.state)
            cur = _item(spec, item_id) if isinstance(item_id, str) else None
            if cur is None or cur.authority != "pending":
                raise ActionRefused(NOT_PENDING)
            old_id = c.get("replaces")
            old = _item(spec, old_id) if isinstance(old_id, str) else None
            text = c["text"]

            def change(s):
                new = tspec.edit_item(s, cur.item_id, text=text)
                new = new.model_copy(update={"items": tuple(
                    i.model_copy(update={"authority": "user"}) if i.item_id == cur.item_id else i
                    for i in new.items)})
                if old is None:
                    return new
                return new.model_copy(update={"items": tuple(
                    i for i in new.items if i.item_id != old.item_id)})
            undo = {"mode": "confirmed", "item_id": cur.item_id, "text": text,
                    **({"old": old.model_dump(mode="json")} if old is not None else {})}
            self._commit(snap, change, action, "chat", [effect, _write_undo(action, undo)])
        return {"item_id": cur.item_id, "superseded": [old.item_id] if old is not None else []}

    def rejected(self, user_id: str, action: ActionRow) -> None:
        if action.kind != PENDING_KIND:
            return
        item_id = action.content.get("item_id")
        snap = self._repo.load(user_id)
        cur = _item(tspec.effective_spec(snap.state), item_id) if isinstance(item_id, str) else None
        if cur is None or cur.authority != "pending":
            return              # already removed elsewhere (the guidelines page)
        self._commit(snap, lambda s: tspec.remove_item(s, cur.item_id), action, "ledger_reject",
                     [])
        return

    def superseded(self, user_id: str, rows: Sequence[ActionRow]) -> set[str]:
        """Those of `rows` `undo` would refuse with GONE: the rule was edited or deleted since."""
        rows = [r for r in rows if r.undo]
        if not rows:
            return set()
        spec = tspec.effective_spec(self._repo.load(user_id).state)

        def gone(u: Mapping[str, Any]) -> bool:
            cur = _item(spec, u.get("item_id", ""))
            return cur is None or cur.text != u.get("text")
        return {r.action_id for r in rows if gone(r.undo)}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        with _refusing():
            snap = self._repo.load(user_id)
            cur = _item(tspec.effective_spec(snap.state), u["item_id"])
            if cur is None or cur.text != u["text"]:
                raise ActionRefused(GONE)
            if "old" in u:
                old = SpecItem.model_validate(u["old"])
                change: Callable = lambda s: tspec.write(
                    s, tspec.ReplaceRule(cur.item_id, _restore(old)), actor="user")
            else:
                change = lambda s: tspec.remove_item(s, cur.item_id)
            self._commit(snap, change, action, "ledger_undo", [self._mark(action)])
        return {"removed": cur.item_id, "restored": u.get("old", {}).get("item_id")}


class RestyleExecutor(_Graph):
    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        pages = [p.get("page_id") for p in action.content.get("pages") or ()
                 if isinstance(p, Mapping)]
        with _refusing():
            snap = self._repo.load(user_id)
            state = snap.state
            ids = [p for p in pages if isinstance(p, str) and p in state.pages
                   and p not in state.trashed_pages]
            if not ids:
                raise ActionRefused("这些页面都不在了")
            version = tspec.effective_version(state)
            marks = {p: StaleMark(patch_id=action.action_id, spec_version=version) for p in ids}
            ev = WikiEvent(event_id=self._new_id("wev"), ts=self._clock(),
                           body=PagesStaleSet(marked=marks,
                                              checked_version=state.stale_checked_version))
            self._repo.commit(snap, Commit(wiki=[ev], extra=[
                effect, _write_undo(action, {"page_ids": ids})]))
        return {"marked": ids}

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        with _refusing():
            snap = self._repo.load(user_id)
            state = snap.state
            mine = [p for p in u.get("page_ids", ())
                    if (m := state.stale_marks.get(p)) is not None
                    and m.patch_id == action.action_id]
            if not mine:
                raise ActionRefused("这些页面已经重写过了，不用再撤销")
            ev = WikiEvent(event_id=self._new_id("wev"), ts=self._clock(),
                           body=PagesStaleSet(cleared=tuple(mine),
                                              checked_version=state.stale_checked_version))
            self._repo.commit(snap, Commit(wiki=[ev], extra=[self._mark(action)]))
        return {"cleared": mine}
