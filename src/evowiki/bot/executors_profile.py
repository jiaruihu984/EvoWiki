"""`profile_fact` (stage 3 T1): one fact the learner said about himself, written by the assistant's
`remember_about_me` into his profile note as a ledger row (recorded and executed at once, undoable).

A fact waiting for his confirmation (`wikiapp.preferences.land_preference`: words from an earlier
message, or inferred) is the same kind as a proposed `ask` row; confirming it executes it here.

The row's content is its own undo data: `{section, quote, text, old_line, meta}` (`meta`: the
fact's provenance, kept in `Profile.fact_meta`). Executing adds the line
(`profile.note.add_fact`: `old_line` names the owned line it updates, the tool's `replaces`);
undoing takes the line out and puts `old_line` back. The profile write and the row's status commit in one transaction (`effect`)
when the store writes to the database (`update_in`); an in-memory store (tests) has no connection."""

from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from evowiki.bot.actions import ActionRefused, register_undo
from evowiki.profile import known as kn
from evowiki.profile.note import FACT_SECTIONS, KNOWN, FactMeta, Profile, add_fact, line_key
from evowiki.store.bot import ActionRow
from evowiki.wikiapp.kchange import k_changed
from evowiki.wikiapp.kwrite import apply_in_memory
from evowiki.wikiapp.repo import Commit, WikiConflict

KIND = "profile_fact"
FORGET_KIND = "profile_forget"     # 「忘掉我是大三学生」: a line of 我 goes, after his 确认

KNOWN_RETRACT = "known_retract"    # a line deleted from 已经会的: what he said about it no longer counts

register_undo(KIND, lambda row: True)
register_undo(KNOWN_RETRACT, lambda row: bool(row.undo))
register_undo(FORGET_KIND, lambda row: True)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def _fields(action: ActionRow) -> tuple[str, str, str]:
    c = action.content
    section, text, old = c.get("section"), _norm(c.get("text")), _norm(c.get("old_line"))
    if section not in FACT_SECTIONS or not text:
        raise ActionRefused("这条档案记录的格式不对")
    return str(section), text, old


def _without(p: Profile, section: str, text: str, restore: str) -> Profile:
    lines = list(p.owned.get(section, ()))
    if text not in lines:
        raise ActionRefused("这句之后被改过或删掉了，不用再撤销")
    if restore and restore not in lines:
        lines[lines.index(text)] = restore
    else:
        lines.remove(text)
    owned = {**p.owned, section: tuple(lines)} if lines else {
        k: v for k, v in p.owned.items() if k != section}
    metas = {k: v for k, v in p.fact_meta.items() if k != line_key(section, text)}
    return p.model_copy(update={"owned": owned, "fact_meta": metas})


class ProfileFactExecutor:
    """`store`: a profile store with `update_in(conn, user_id, fn)` (PgProfileStore,
    MemoryProfileStore); `pool`: the database pool the effect commits on (None: no connection)."""

    def __init__(self, store: Any, pool: Any = None):
        self._store, self._pool = store, pool

    def _tx(self):
        if self._pool is None:
            return nullcontext(None)
        return _Tx(self._pool)

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        section, text, old = _fields(action)
        raw = action.content.get("meta")
        try:
            meta = (FactMeta.model_validate({**raw, "action_id": action.action_id})
                    if isinstance(raw, dict) else None)
        except ValueError as e:
            raise ActionRefused("这条档案记录的来源格式不对") from e
        with self._tx() as conn:
            self._store.update_in(conn, user_id, lambda p: add_fact(p, section, text, old, meta))
            effect(conn)
        return {"section": section, "text": text}

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    def undo(self, user_id: str, action: ActionRow) -> dict:
        section, text, old = _fields(action)
        with self._tx() as conn:
            self._store.update_in(conn, user_id, lambda p: _without(p, section, text, old))
        return {"removed": text}


class ProfileForgetExecutor(ProfileFactExecutor):
    """`profile_forget`: the row's content names the line ({section, text}); executing takes it out,
    its provenance kept in the result, and undoing puts both back."""

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        section, text, _ = _fields(action)
        meta: dict[str, Any] = {}

        def out(p: Profile) -> Profile:
            kept = p.fact_meta.get(line_key(section, text))
            if kept is not None:
                meta.update(kept.model_dump(mode="json"))
            return _without(p, section, text, "")
        with self._tx() as conn:
            self._store.update_in(conn, user_id, out)
            effect(conn)
        return {"section": section, "text": text, "meta": meta}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        section, text, _ = _fields(action)
        raw = (action.result or {}).get("meta")
        meta = FactMeta.model_validate(raw) if raw else None
        with self._tx() as conn:
            self._store.update_in(conn, user_id, lambda p: add_fact(p, section, text, "", meta))
        return {"restored": text}


class KnownRetractLog:
    """`profile.service.KnownLog` on the ledger: the row is recorded with the retraction's commit."""

    def __init__(self, ledger: Any):
        self._ledger = ledger

    def record_in(self, conn: Any, user_id: str, key: str, retraction: kn.Retraction) -> None:
        first = retraction.lines[0]
        summary = (f"不再记你会「{first}」" if len(retraction.lines) == 1
                   else f"不再记你会「{first}」等 {len(retraction.lines)} 条")
        self._ledger.record_in(conn, user_id, kind=KNOWN_RETRACT, scope="suggestion", source="user",
                               summary=summary, content={"lines": list(retraction.lines)}, key=key,
                               undo=retraction.undo)

    def action_id(self, key: str) -> str | None:
        row = self._ledger.get_by_key(key)
        return row.action_id if row is not None else None


class KnownRetractExecutor:
    """`known_retract`: only its undo: what the deleted lines said counts again (`known.restored`, new
    events) and they show in 已经会的 again, by the one K-change path."""

    def __init__(self, repo: Any, store: Any, clock: Callable[[], Any], new_id: Callable[[str], str]):
        self._repo, self._store, self._clock, self._new_id = repo, store, clock, new_id

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        raise ActionRefused("这条记录已经生效了")

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = action.undo or {}
        for attempt in (1, 2, 3):
            snap, now = self._repo.load(user_id), self._clock()
            aspect = kn.restored(u, ts=now, new_id=self._new_id)
            kc = k_changed(snap, apply_in_memory(snap, aspect), snap.state.current_page, None,
                           skip=None, now=now, new_id=self._new_id, action="declare")
            try:
                self._repo.commit(snap, Commit(aspect=aspect, wiki=list(kc.wiki)))
                break
            except WikiConflict as exc:
                if attempt == 3:
                    raise ActionRefused("K 正在被别的操作改动，这次没有撤销") from exc
        lines = set(u["lines"])
        def back(p: Profile) -> Profile:
            left = tuple(x for x in p.dropped.get(KNOWN, ()) if x not in lines)
            dropped = {**p.dropped, KNOWN: left} if left else {k: v for k, v in p.dropped.items() if k != KNOWN}
            return p.model_copy(update={"dropped": dropped})
        self._store.update(user_id, back)
        return {"restored": sorted(lines)}


class _Tx:
    def __init__(self, pool: Any):
        self._pool = pool

    def __enter__(self):
        self._cm = self._pool.connection()
        conn = self._cm.__enter__()
        self._t = conn.transaction()
        self._t.__enter__()
        return conn

    def __exit__(self, *exc):
        try:
            self._t.__exit__(*exc)
        finally:
            self._cm.__exit__(*exc)
        return False
