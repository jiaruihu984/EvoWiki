"""Action ledger (spec 2026-10-14 §4): one table for every knowledge-base change an agent makes.
Approval levels follow the actual side effect and are the user's setting (`BotSettings.levels`,
defaults `bot.settings.LEVELS`); `ask` actions wait for the user (a pending item never expires),
`auto_undo` / `auto` ones are mirrored here after they happened. Approval is exactly-once:
the `executed` status is written with `apply_once` inside the wiki commit itself."""

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from evowiki.agent.effects import EffectConflict, apply_once, payload_hash
from evowiki.bot.settings import BotSettings, Level, Scope
from evowiki.store import bot as store
from evowiki.store import prompts as prompt_store
from evowiki.store.bot import ActionRow
from evowiki.store.locks import xact_lock

__all__ = [
    "SPLIT_FRESH",
    "STALE_DETAIL",
    "TURN_COST",
    "ActionExecutor",
    "ActionLedger",
    "ActionRefused",
    "ActionRow",
    "AlreadyExecuted",
    "NotDecidable",
    "can_undo",
    "level_for",
    "register_undo",
    "strictest",
]

log = logging.getLogger(__name__)

TURN_COST = "turn_cost_usd"      # result key: USD of the assistant turn's calls behind the row
STALE_DETAIL = "页面在提议之后改过了，这条修改已作废，不会覆盖新版本"
# what a split does to the content: said to the model with its draft and with its result
SPLIT_FRESH = ("确认后新页面是空页，按标题重新写：打开时写；他开了「空闲时写页面」时可能提前写。"
               "原页的内容不会搬过去，原页留在 Wiki")


class ActionRefused(Exception):
    """The executor cannot (or will not) do it; `detail` is shown to the user (Chinese)."""

    def __init__(self, detail: str, *, stale: bool = False,
                 public_message: str | None = None, code: str = "action_refused",
                 params: dict[str, object] | None = None):
        super().__init__(detail)
        self.detail = STALE_DETAIL if stale else detail
        self.stale = stale
        self.public_message = public_message
        self.error_code = "page_changed" if stale else code
        self.params = params or {}


def _redraftable(row: ActionRow) -> bool:
    """A draft 重试 may compose again: still waiting, or its 确认 refused because the page moved."""
    return row.status == "proposed" or (row.status == "failed"
                                         and row.kind in ("page_practice", "page_revise"))


class AlreadyExecuted(Exception):
    """Raised inside the effect: another approve already executed the row, so this wiki commit
    must roll back."""


_STATUS_ZH = {"proposed": "待确认", "approved": "正在执行", "executed": "已生效",
              "rejected": "已拒绝", "undone": "已撤销", "failed": "未成功"}


def _status_zh(status: str) -> str:
    return _STATUS_ZH.get(status, "已处理")


class NotDecidable(ValueError):
    """The action is in the wrong status for this operation → HTTP 409. `error_code`, when set, is
    sent as the response's `code` (e.g. `patches_off`) so clients branch on it, not on the text."""

    def __init__(self, *args: object, error_code: str | None = None,
                 public_message: str | None = None,
                 params: dict[str, object] | None = None) -> None:
        super().__init__(*args)
        self.error_code = error_code
        self.public_message = public_message
        self.params = params or {}
        if error_code == "patches_off":
            self.public_message = "Code changes are disabled. Enable them in settings before approving."
            self.params = {}


_UNDO_RULES: dict[str, Callable[[ActionRow], bool]] = {}


def register_undo(kind: str, fn: Callable[[ActionRow], bool]) -> None:
    """Declare when an `executed` row of `kind` can be undone (replaces an earlier rule)."""
    _UNDO_RULES[kind] = fn


def _default_undo(row: ActionRow) -> bool:
    return row.undo is not None


# assistant edit: while its undo data exists; a preference always;
# an RSI mirror always (its executor undoes the RSI change itself, `RsiExecutor.undo`; a refusal
# says why).
register_undo("assistant_edit", lambda row: bool(row.undo))
register_undo("preference", lambda row: True)
register_undo("rsi_patch", lambda row: True)
register_undo("rsi_method", lambda row: True)
# path_edit (the Bot's B5b edit and the assistant's reorder / postpone): while its undo data
# exists
register_undo("path_edit", lambda row: bool(row.undo))
# the assistant's graph / review-card / rule / settings actions (`bot.executors_graph`): while
# their undo data exists (the executor refuses with the reason when a later change depends on it)
# bot_write / user_write: a page the Bot wrote / a page written for the learner (undo = back to unwritten, never auto-written again); the
# `ask` batch proposal has no undo data
for _kind in ("node_split", "node_relation", "node_known", "node_move", "card_new",
              "card_fix", "review_postpone", "rule", "settings", "bot_write", "user_write"):
    register_undo(_kind, lambda row: bool(row.undo))
# page_revise (a whole-page draft from materials, `bot.executors_revise`): while its undo data
# exists (the executor refuses when the page changed after the swap)
register_undo("page_revise", lambda row: bool(row.undo))


def can_undo(row: ActionRow, superseded: Collection[str] = ()) -> bool:
    """Whether an undo of `row` can succeed, the one rule behind every `can_undo` the API shows:
    only an `executed` row, and then by the rule its kind registered (`register_undo`; default:
    the row has undo data); never one whose target moved on (`superseded`: ids from
    `ActionLedger.superseded`: a prompt override with a newer one of the same step, a rule edited
    or deleted since)."""
    if row.status != "executed" or row.action_id in superseded:
        return False
    if row.kind == "prompt_override":                # Prompt 设置 (bot.prompts)
        return True
    return _UNDO_RULES.get(row.kind, _default_undo)(row)


def made_of(row: ActionRow) -> list[dict]:
    """The pages an executed row made (the result's `made`): `{key, page_id, n, folder}` with the
    draft `key` each came from, its path position `n` (None: not on a path) and its folder. What a
    chat card needs to show each line's writing status; `[]` for any other row or a malformed
    result."""
    if row.status != "executed":
        return []
    made = (row.result or {}).get("made")
    if not isinstance(made, list):
        return []
    out = []
    for m in made:
        if not (isinstance(m, dict) and isinstance(m.get("page_id"), str)
                and (m.get("key") is None or isinstance(m["key"], str))
                and (m.get("n") is None or isinstance(m["n"], int))):
            return []
        out.append({"key": m.get("key"), "page_id": m["page_id"], "n": m.get("n"),
                    "folder": m.get("folder") if isinstance(m.get("folder"), str) else ""})
    return out


_LEVEL_ORDER: tuple[Level, ...] = ("never", "ask", "auto_undo", "auto")


def strictest(levels: Iterable[Level]) -> Level:
    """The strictest of several levels (`never` > `ask` > `auto_undo` > `auto`); an action made of
    several parts waits for the user as soon as one part must."""
    ls = list(levels)
    if not ls:
        raise ValueError("strictest of no levels")
    return min(ls, key=_LEVEL_ORDER.index)


class ActionExecutor(Protocol):
    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        """Put `effect` into the SAME commit as the wiki write (`Commit.extra`); return a JSON
        result; raise ActionRefused."""

    def rejected(self, user_id: str, action: ActionRow) -> None: ...

    def undo(self, user_id: str, action: ActionRow) -> dict:
        """Raises ActionRefused / LookupError."""

    # Optional: `revise(action, draft) -> dict` - the user edited the draft before 确认; returns the
    # new `content` (the ledger stores it and re-hashes it, then executes the revised row), raises
    # ActionRefused if the edit is invalid. Without it a draft body is NotDecidable.
    # Optional: `check_approvable(action)` - called under the row lock before a `proposed` /
    # `approved` row is revised, approved or executed; raises NotDecidable when the row cannot be
    # approved yet (a draft still being written), so the row stays as it was.
    # Optional: `decided(user_id, action)` - called by the ledger after approve changed the row's
    # final status (`executed` with its result, or `failed` with its detail). It is the only place
    # an executor mirrors the outcome elsewhere, so the mirror follows the ledger, never the
    # executor's local view (a racing approve that lost must not write a `failed` mirror).


def level_for(scope: Scope, settings: BotSettings) -> Level:
    """The user's approval level of a scope (`never` raises: no agent changes the user's notes)."""
    level = settings.levels[scope]
    if level == "never":
        raise PermissionError("不会修改你自己的笔记")
    return level


class ActionLedger:
    def __init__(self, pool, clock: Callable[[], datetime], new_id: Callable[[str], str],
                 executors: Mapping[str, ActionExecutor]):
        self._pool, self._clock, self._new_id = pool, clock, new_id
        self._executors: dict[str, ActionExecutor] = dict(executors)

    def register(self, kind: str, executor: ActionExecutor) -> None:
        """Late binding: an executor that needs services built after the ledger."""
        self._executors[kind] = executor

    def get_by_key(self, key: str) -> ActionRow | None:
        with self._pool.connection() as conn:
            return store.get_action_by_key(conn, key)

    def get(self, user_id: str, action_id: str) -> ActionRow | None:
        """The user's action by id (another user's or unknown -> None)."""
        with self._pool.connection() as conn:
            row = store.get_action(conn, action_id)
        return row if row is not None and row.user_id == user_id else None

    def executor_of(self, kind: str) -> ActionExecutor:
        """The executor registered for `kind` (for a kind-specific call such as node_known's
        `confirm`); NotImplementedError when there is none."""
        return self._executor(kind)

    # --- writing rows ---------------------------------------------------------------------

    def level_in(self, conn, user_id: str, scope: Scope) -> Level:
        """The user's level of `scope` now, read on the caller's connection."""
        return level_for(scope, store.get_settings(conn, user_id))

    def settings(self, user_id: str) -> BotSettings:
        with self._pool.connection() as conn:
            return store.get_settings(conn, user_id)

    def level(self, user_id: str, scope: Scope) -> Level:
        with self._pool.connection() as conn:
            return self.level_in(conn, user_id, scope)

    def _proposal(self, conn, user_id: str, *, kind, scope, source, summary, why, content, key,
                  page_id=None, page_version=None, run_id=None, step=None, trigger_id=None,
                  approved_now=False, level: Level | None = None,
                  result: dict | None = None) -> ActionRow:
        """A pending item has no expiry (`expires_at` NULL). `approved_now`: the user's accept
        click already is the approval (level `auto_undo`): the row starts `approved` and is
        executed at once by `approve`, never shown as pending."""
        level = level or self.level_in(conn, user_id, scope)   # the caller's read, if it has one
        if level != ("auto_undo" if approved_now else "ask"):
            raise ValueError(
                f"scope {scope!r} 的级别是 {level}：" + (
                    "只有 auto_undo 的修改能由一次点击直接批准" if approved_now
                    else "propose 只用于要批准的修改，自动的用 record_in"))
        now = self._clock()
        return ActionRow(
            action_id=self._new_id("act"), user_id=user_id, kind=kind, scope=scope, level=level,
            status="approved" if approved_now else "proposed", source=source, summary=summary,
            why=why, content=content, content_hash=payload_hash(content), idempotency_key=key,
            page_id=page_id, page_version=page_version, run_id=run_id, step=step,
            trigger_id=trigger_id, result=result, created_at=now,
            decided_at=now if approved_now else None)

    def propose_in(self, conn, user_id: str, **kw) -> ActionRow:
        """`propose` inside the caller's transaction. A repeat of the key with the same content
        returns the existing row; other content is an EffectConflict."""
        row = self._proposal(conn, user_id, **kw)
        store.insert_action(conn, row)
        got = store.get_action_by_key(conn, row.idempotency_key)
        if got.content_hash != row.content_hash or got.user_id != user_id:
            raise EffectConflict(
                f"idempotency key {row.idempotency_key!r} was used for a different action")
        return got

    def batches(self, user_id: str) -> list[ActionRow]:
        """All the user's `bot_write` batch proposals, any status, no limit."""
        with self._pool.connection() as conn:
            return store.batch_rows(conn, user_id)

    def asked_for(self, user_id: str, page_id: str, before: datetime) -> str | None:
        """The summary of the assistant action that made this page (the newest not after `before`)."""
        with self._pool.connection() as conn:
            row = store.assistant_row_making(conn, user_id, page_id, before)
        return None if row is None else row.summary

    def update_pending(self, user_id: str, action_id: str, *, summary: str,
                       content: dict) -> bool:
        """A proposal the Bot made that still waits changes its mind (the pages it would write
        changed): summary and content are replaced, the row stays the one pending item. False:
        the row is not the user's, or is not waiting any more."""
        with self._pool.connection() as conn, conn.transaction():
            row = store.lock_action(conn, action_id)
            if row is None or row.user_id != user_id or row.status != "proposed":
                return False
            conn.execute("update agent_action set summary = %s, content = %s, content_hash = %s "
                         "where action_id = %s", (summary, Jsonb(content), payload_hash(content),
                                                  action_id))
            return True

    def fill_draft(self, user_id: str, action_id: str, content: dict) -> bool:
        """A proposal whose draft a run writes after it was made (`page_revise`): the run's result
        (the draft, or why there is none) replaces the content, only while the row is the user's,
        still `proposed` and its `content.state` still "writing" (a row rejected meanwhile, or
        filled already, is left alone). The keys `content` lacks keep their stored value (the
        run id the starter wrote after the run read the row). False: nothing changed."""
        with self._pool.connection() as conn, conn.transaction():
            row = store.lock_action(conn, action_id)
            if (row is None or row.user_id != user_id or row.status != "proposed"
                    or row.content.get("state") != "writing"):
                return False
            merged = {**row.content, **content}
            conn.execute("update agent_action set content = %s, content_hash = %s "
                         "where action_id = %s", (Jsonb(merged), payload_hash(merged), action_id))
            return True

    def edit_draft(self, user_id: str, action_id: str, draft: dict) -> ActionRow:
        """The learner's edit of a waiting draft on its card, kept as it happens (a goal's
        blocks: each move / × / 加一块), WITHOUT approving: the executor's `revise(row, draft)`
        makes the new content, stored with its hash on the same row, which stays `proposed`
        (a later chat edit — `edit_goal_blocks` — or 确认 starts from it). Only while the row is
        `proposed` (NotDecidable otherwise, nothing written); a kind without `revise` →
        NotDecidable; another user's row → LookupError; a refused revision → ActionRefused
        (nothing written: the card shows why and reloads the row)."""
        with self._pool.connection() as conn:
            executor = self._executor(self._owned_plain(conn, user_id, action_id).kind)
        revise = getattr(executor, "revise", None)
        if revise is None:
            raise NotDecidable("这类改动的草稿不能修改")
        with self._pool.connection() as conn, conn.transaction():
            row = self._owned(conn, user_id, action_id)
            if row.status != "proposed":
                raise NotDecidable(f"这条改动{_status_zh(row.status)}，草稿不能再改")
            content = revise(row, draft)
            store.set_action_content(conn, action_id, content, payload_hash(content))
        return self._get(action_id)

    def redraft(self, user_id: str, action_id: str) -> ActionRow:
        """重试 on a draft that no longer fits the page (it waits, or its 确认 failed as stale): the
        executor's `redraft(user_id, row, key)` composes the same request again against the page as
        it is now (a model call, outside any lock) -> (content, page_version), stored on the SAME
        row, which waits for the learner again. NotDecidable for a kind without `redraft` or a row
        that was decided otherwise (also when that happened while composing); another user's row
        -> LookupError; the executor's ActionRefused passes through (nothing written)."""
        with self._pool.connection() as conn:
            row = self._owned_plain(conn, user_id, action_id)
        fn = getattr(self._executor(row.kind), "redraft", None)
        if fn is None:
            raise NotDecidable("这类改动不能重新生成草稿")
        if not _redraftable(row):
            raise NotDecidable(f"这条改动{_status_zh(row.status)}，不能重新生成")
        content, version = fn(user_id, row, f"redraft:{action_id}:{self._new_id('rd')}")
        with self._pool.connection() as conn, conn.transaction():
            row = self._owned(conn, user_id, action_id)
            if not _redraftable(row):
                raise NotDecidable(f"这条改动{_status_zh(row.status)}，不能重新生成")
            store.set_action_redrafted(conn, action_id, content, payload_hash(content), version)
        return self._get(action_id)

    def set_content_keys(self, user_id: str, action_id: str, keys: dict) -> bool:
        """Merges `keys` into the user's row's content (e.g. the run id of a `page_revise` row,
        known only once its run started), whatever its status. False: no such row."""
        with self._pool.connection() as conn, conn.transaction():
            row = store.lock_action(conn, action_id)
            if row is None or row.user_id != user_id:
                return False
            merged = {**row.content, **keys}
            conn.execute("update agent_action set content = %s, content_hash = %s "
                         "where action_id = %s", (Jsonb(merged), payload_hash(merged), action_id))
            return True

    def add_result_keys(self, user_id: str, action_id: str, keys: dict) -> bool:
        """Merges `keys` into the user's row's `result` (what the row did, not what it does: the
        content and its hash stay), e.g. `confirmed_by` after a chat confirmation (stage 3 T5).
        False: no such row."""
        with self._pool.connection() as conn, conn.transaction():
            row = store.lock_action(conn, action_id)
            if row is None or row.user_id != user_id:
                return False
            conn.execute("update agent_action set result = %s where action_id = %s",
                         (Jsonb({**(row.result or {}), **keys}), action_id))
            return True

    def open_page_revise(self, user_id: str, page_id: str) -> ActionRow | None:
        """The page's `page_revise` row whose draft is still open (writing / ready, proposed)."""
        with self._pool.connection() as conn:
            return store.open_page_revise(conn, user_id, page_id)

    def propose_unless(self, user_id: str, blocking: Callable[[Any], ActionRow | None],
                       **kw) -> tuple[ActionRow, bool]:
        """`propose` unless `blocking(conn)` finds a row that stands in its way (e.g. the page's
        draft still open): (that row, False). The check and the insert hold one advisory lock per
        user and kind, so two concurrent calls never both propose."""
        with self._pool.connection() as conn, conn.transaction():
            xact_lock(conn, f"evowiki.propose:{kw['kind']}:{user_id}")
            found = blocking(conn)
            if found is not None:
                return found, False
            return self.propose_in(conn, user_id, **kw), True

    def propose(self, user_id: str, **kw) -> ActionRow:
        with self._pool.connection() as conn, conn.transaction():
            return self.propose_in(conn, user_id, **kw)

    def propose_replacing(self, user_id: str, old_action_id: str, *, detail: str,
                          **kw) -> ActionRow:
        """A new draft in place of a pending one (the assistant's chat edit of a goal's blocks):
        the new row is proposed and the old one rejected with `detail` (e.g. "replaced", which
        the card shows as 「已换成新的分法」) in ONE transaction, the old row locked first, so the
        learner never sees both pending or neither. The old row not the user's → LookupError;
        not `proposed` any more (confirmed or cancelled meanwhile) → NotDecidable, nothing
        written. The old kind's `rejected` hook runs after the commit, as `reject`'s."""
        with self._pool.connection() as conn, conn.transaction():
            old = self._owned(conn, user_id, old_action_id)
            if old.status != "proposed":
                raise NotDecidable(f"这条改动{_status_zh(old.status)}，不能再换")
            row = self.propose_in(conn, user_id, **kw)
            store.set_action_status(conn, old_action_id, status="rejected", at=self._clock(),
                                    detail=detail)
        ex = self._executors.get(old.kind)
        if ex is not None:
            try:
                ex.rejected(user_id, self._get(old_action_id))
            except Exception:
                log.exception("executor.rejected failed for action %s; the rejection stands",
                              old_action_id)
        return row

    def latest(self, user_id: str, kind: str, *, match: Mapping[str, Any],
               has: str | None = None) -> ActionRow | None:
        """The user's newest row of `kind` (any status) whose content contains `match` (jsonb
        containment) and, with `has`, has that content key — e.g. a session's latest goal draft
        with blocks. No window: an old row is found as a new one is."""
        sql = ("select action_id from agent_action where user_id = %s and kind = %s "
               "and content @> %s")
        params: list[Any] = [user_id, kind, Jsonb(dict(match))]
        if has is not None:
            sql += " and content ? %s"
            params.append(has)
        with self._pool.connection() as conn:
            got = conn.execute(sql + " order by created_at desc, action_id desc limit 1",
                               params).fetchone()
            if got is None:
                return None
            action_id = got[0] if not isinstance(got, Mapping) else got["action_id"]
            return store.get_action(conn, action_id)

    def record_in(self, conn, user_id: str, *, kind, scope, source, summary, why="", content, key,
                  status="executed", result=None, wiki_event_ids: Sequence[str] = (), undo=None,
                  page_id=None, run_id=None, step=None, trigger_id=None) -> None:
        """An action that already happened inside the caller's transaction (the mirror of an
        existing mechanism, or one the user accepted); a repeat of the key updates the outcome.
        Its level is what it is now: `auto` for suggestions, otherwise `auto_undo` (undoable)
        whatever the user's level says for FUTURE changes of the scope. A `proposed` row (a pending
        mirror) carries the user's level of the scope instead."""
        if scope == "suggestion":
            level = "auto"
        elif status == "proposed":      # waits for the user: the level actually in force
            level = self.level_in(conn, user_id, scope)
        else:
            level = "auto_undo"
        if scope == "user_notes":
            raise PermissionError("不会修改你自己的笔记")
        now = self._clock()
        store.upsert_action_row(conn, ActionRow(
            action_id=self._new_id("act"), user_id=user_id, kind=kind, scope=scope, level=level,
            status=status, source=source, summary=summary, why=why, content=content,
            content_hash=payload_hash(content), idempotency_key=key, page_id=page_id,
            result=result, wiki_event_ids=list(wiki_event_ids), undo=undo, run_id=run_id,
            step=step, trigger_id=trigger_id, created_at=now, decided_at=now))

    def record(self, user_id: str, **kw) -> None:
        """`record_in` in a transaction of its own."""
        with self._pool.connection() as conn, conn.transaction():
            self.record_in(conn, user_id, **kw)

    def set_status_in(self, conn, key: str, status: str, *, detail: str = "", at: datetime,
                      from_statuses: Sequence[str] | None = None,
                      missing_ok: bool = False) -> bool:
        """Move the row of `key` to `status`. With `from_statuses` only a row currently in one of
        them moves (a mirror never fights the ledger's own decision); returns whether it moved.
        An unknown key is a LookupError unless `missing_ok` (history from before the ledger has
        no mirror rows: skipped with a debug log)."""
        row = store.get_action_by_key(conn, key)
        if row is None:
            if missing_ok:
                log.debug("no ledger row %r to move to %s (history from before the ledger)",
                          key, status)
                return False
            raise LookupError(f"unknown action key {key!r}")
        return store.set_action_status(conn, row.action_id, status=status, at=at, detail=detail,
                                       from_statuses=from_statuses)

    # --- decisions ------------------------------------------------------------------------

    def _owned(self, conn, user_id: str, action_id: str) -> ActionRow:
        row = store.lock_action(conn, action_id)
        if row is None or row.user_id != user_id:
            raise LookupError("没有找到这条改动")
        return row

    def _owned_plain(self, conn, user_id: str, action_id: str) -> ActionRow:
        row = store.get_action(conn, action_id)
        if row is None or row.user_id != user_id:
            raise LookupError("没有找到这条改动")
        return row

    def _executor(self, kind: str) -> ActionExecutor:
        ex = self._executors.get(kind)
        if ex is None:
            raise NotImplementedError(f"no executor for action kind {kind!r}")
        return ex

    def _get(self, action_id: str) -> ActionRow:
        with self._pool.connection() as conn:
            return store.get_action(conn, action_id)

    def approve(self, user_id: str, action_id: str, *,
                draft: dict | None = None) -> ActionRow:
        """Exactly-once approval. With `draft` (the user's edited version) a `proposed` row is first
        revised by its executor (content + hash, same transaction as the approval); a refused
        revision makes the row `failed` and nothing is executed.

        1. Short transaction: lock the row (another user's -> LookupError); `executed` -> return
           it (idempotent); rejected / undone / failed -> NotDecidable; `proposed` ->
           `approved` (decided_at = now); commit. An executor's `check_approvable` may refuse
           first (NotDecidable; nothing moves).
        2. effect(conn) = apply_once(conn, "action:<key>", {action_id, content_hash}, fn) where fn
           marks the row `executed`.
        3. executor.execute(user_id, row, effect): the executor checks `page_version` against the
           current page inside its own commit path and passes `effect` in `Commit.extra`, so the
           status change and the wiki write commit together.
        4. ActionRefused -> the row becomes `failed` with the detail (stale: the fixed text); the
           failed row is returned (no exception).
        5. A crash between 1 and 3 leaves `approved`; approving again re-runs 2-4 (safe: the
           apply_once key makes the effect run at most once)."""
        now = self._clock()
        with self._pool.connection() as conn:
            executor = self._executor(self._owned_plain(conn, user_id, action_id).kind)
        revise = getattr(executor, "revise", None)
        if draft is not None and revise is None:
            raise NotDecidable("这类改动不能带修改后的草稿确认")
        revise_refused = False
        with self._pool.connection() as conn, conn.transaction():
            row = self._owned(conn, user_id, action_id)
            if row.status == "executed":
                return row
            check = getattr(executor, "check_approvable", None)
            if check is not None and row.status in ("proposed", "approved"):
                check(row)            # NotDecidable rolls this transaction back: nothing moves
            if draft is not None and row.status != "proposed":
                raise NotDecidable(f"这条改动{_status_zh(row.status)}，不能带草稿确认")
            if row.status == "proposed":
                if draft is not None:
                    try:
                        content = revise(row, draft)
                    except ActionRefused as e:
                        revise_refused = True
                        store.set_action_status(conn, action_id, status="failed", at=now,
                                                detail=e.detail, result={**(row.result or {}),
                                                                       "error": {"code": e.error_code, "params": e.params, "message": e.public_message}})
                    else:
                        store.set_action_content(conn, action_id, content, payload_hash(content))
                if not revise_refused:
                    store.set_action_status(conn, action_id, status="approved", at=now)
            elif row.status != "approved":
                raise NotDecidable(f"这条改动{_status_zh(row.status)}，不能确认")
        row = self._get(action_id)
        if revise_refused:
            self._decided(user_id, row)
            return row

        def effect(conn) -> None:
            # Serialize with a concurrent approve: the row lock is held until this commit ends;
            # whoever comes second sees `executed` and rolls its whole wiki write back.
            if store.lock_action(conn, action_id).status != "approved":
                raise AlreadyExecuted(action_id)

            def mark() -> dict:
                store.set_action_status(conn, action_id, status="executed", at=self._clock())
                return {"action_id": action_id}
            apply_once(conn, f"action:{row.idempotency_key}",
                       {"action_id": action_id, "content_hash": row.content_hash}, mark)

        try:
            result = executor.execute(user_id, row, effect)
        except ActionRefused as e:
            return self._fail(user_id, action_id, e.detail, code=e.error_code, params=e.params,
                              message=e.public_message)
        except AlreadyExecuted:
            return self._get(action_id)
        result = result or {}
        with self._pool.connection() as conn, conn.transaction():
            cur = store.lock_action(conn, action_id)
            if cur.status == "executed":
                # the cost of the model calls that made the proposal (put on the row when it was
                # proposed) survives the executor's own result
                turn_cost = (cur.result or {}).get(TURN_COST)
                if turn_cost is not None and TURN_COST not in result:
                    result = {**result, TURN_COST: turn_cost}
                store.set_action_status(
                    conn, action_id, status="executed", at=cur.decided_at or now,
                    result=result, wiki_event_ids=result.get("wiki_event_ids"))
                done = True
            else:
                done = False
        if done:
            row = self._get(action_id)
            self._decided(user_id, row)
            return row
        return self._fail(user_id, action_id, "这条改动没有成功应用")

    def _fail(self, user_id: str, action_id: str, detail: str, *,
              code: str = "action_failed", params: dict | None = None,
              message: str | None = None) -> ActionRow:
        """`approved` -> `failed`. A row that is already `executed` (a racing approve won) is left
        alone and no hook fires."""
        changed = False
        with self._pool.connection() as conn, conn.transaction():
            cur = store.lock_action(conn, action_id)
            if cur.status == "approved":
                store.set_action_status(conn, action_id, status="failed", at=self._clock(),
                                        detail=detail, result={**(cur.result or {}),
                                                               "error": {"code": code, "params": params or {}, "message": message}})
                changed = True
        row = self._get(action_id)
        if changed:
            self._decided(user_id, row)
        return row

    def _decided(self, user_id: str, row: ActionRow) -> None:
        ex = self._executors.get(row.kind)
        hook = getattr(ex, "decided", None)
        if hook is None:
            return
        try:
            hook(user_id, row)
            void = getattr(ex, "voided_by", None)
            if void is not None and row.status == "executed":
                for key in void(user_id, row):
                    self._void(user_id, key, ex, row)
        except Exception:
            log.exception("executor.decided failed for action %s; the status stands", row.action_id)

    def _void(self, user_id: str, key: str, ex: Any, by: ActionRow) -> None:
        """A waiting draft another one's 确认 made pointless (its section changed): failed as a stale
        one would be at its own 确认, so it stops waiting on the page and the card says so."""
        with self._pool.connection() as conn, conn.transaction():
            got = store.get_action_by_key(conn, key)
            if got is None:         # a draft with no row yet: only its card says so
                ex.void_without_row(by, key)
                return
            if (got.user_id != user_id or got.status != "proposed"
                    or not store.set_action_status(conn, got.action_id, status="failed", at=self._clock(),
                                                   detail=STALE_DETAIL,
                                                   result={**(got.result or {}), "error": {"code": "page_changed", "params": {}}},
                                                   from_statuses=("proposed",))):
                return
        ex.decided(user_id, self._get(got.action_id))

    def reject(self, user_id: str, action_id: str, *, detail: str = "") -> ActionRow:
        with self._pool.connection() as conn, conn.transaction():
            row = self._owned(conn, user_id, action_id)
            if row.status == "rejected":
                return row
            if row.status != "proposed":
                raise NotDecidable(f"这条改动{_status_zh(row.status)}，不能拒绝")
            store.set_action_status(conn, action_id, status="rejected", at=self._clock(), detail=detail)
        row = self._get(action_id)
        ex = self._executors.get(row.kind)
        if ex is not None:
            try:
                ex.rejected(user_id, row)
            except Exception:
                log.exception("executor.rejected failed for action %s; the rejection stands",
                              action_id)
        return row

    def undo(self, user_id: str, action_id: str) -> ActionRow:
        """Executed `auto_undo` and `ask` rows go to the executor (ActionRefused -> NotDecidable);
        an `auto` row only when it has undo data and its kind's rule allows it (`can_undo`: the
        assistant's suggestion actions), otherwise it has nothing to undo. One transaction under a transaction-scoped advisory
        lock serializes concurrent undos of the same action (the lock is released at commit or
        rollback, so it cannot leak; not a row lock: executors may touch the mirror row
        themselves). The executor's return value is not stored."""
        with self._pool.connection() as conn, conn.transaction():
            xact_lock(conn, f"undo:{action_id}")
            row = self._owned_plain(conn, user_id, action_id)
            ex = self._executor(row.kind)
            if row.status != "executed":
                raise NotDecidable(f"这条改动{_status_zh(row.status)}，不能撤销",
                                   error_code="undo_unavailable",
                                   public_message="This change cannot be undone now.")
            if row.level == "auto" and not can_undo(row):
                raise NotDecidable("这类改动不需要撤销")
            try:
                ex.undo(user_id, row)
            except ActionRefused as e:
                raise NotDecidable(e.detail, error_code=e.error_code,
                                   public_message=e.public_message or "This change cannot be undone now.",
                                   params=e.params) from e
            # an executor may already have moved the row in its own commit (page + ledger atomic)
            store.set_action_status(conn, action_id, status="undone", at=self._clock(),
                                    from_statuses=("executed", "undone"))
            return store.get_action(conn, action_id)

    def superseded(self, user_id: str, rows: Sequence[ActionRow]) -> frozenset[str]:
        """Ids of the executed `rows` whose undo would be refused because their target moved on:
        prompt overrides a newer one of the same step replaced, and what an executor's optional
        `superseded(user_id, rows)` names (one read per kind present)."""
        done = [r for r in rows if r.status == "executed"]
        out: set[str] = set()
        if any(r.kind == "prompt_override" for r in done):
            with self._pool.connection() as conn:
                out |= set(prompt_store.superseded_ids(conn, user_id))
        seen: set[int] = set()
        for kind in {r.kind for r in done}:
            ex = self._executors.get(kind)
            fn = getattr(ex, "superseded", None)
            if fn is not None and id(ex) not in seen:
                seen.add(id(ex))
                out |= fn(user_id, [r for r in done if self._executors.get(r.kind) is ex])
        return frozenset(out)

    def list_changes(self, user_id: str, **kw) -> list[ActionRow]:
        with self._pool.connection() as conn:
            return store.list_change_rows(conn, user_id, **kw)

    def list(self, user_id: str, *, status: str | None = None, limit: int = 100,
             writes: bool = False) -> list[ActionRow]:
        """The user's ledger window; the per-page write records only with `writes`."""
        with self._pool.connection() as conn:
            return store.list_actions(conn, user_id, status=status, limit=limit,
                                      now=self._clock(), writes=writes)
