"""Action executors of the Bot ledger (RSI patches and methods, preferences, prompt overrides). The reading assistant's page-edit executor lives with the assistant
(`assistant.edit_executor`)."""

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from evowiki.bot.actions import ActionRefused, ActionRow
from evowiki.llm.prompt_steps import STEP_BY_KEY
from evowiki.store import bot as store
from evowiki.store import pinned
from evowiki.store import prompts as prompt_store
from evowiki.teach.approval import PendingGone, PendingStale

log = logging.getLogger(__name__)

class RsiMirrorExecutor:
    """`rsi_patch` / `rsi_method` rows only mirror the RSI tables; their native rows are undone in
    the RSI list."""

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        raise ActionRefused("这条调整已自动生效")

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    def undo(self, user_id: str, action: ActionRow) -> dict:
        raise ActionRefused(UNDO_REFUSED)


GONE_DETAIL = "这条改动已不需要确认"
UNDO_REFUSED = "这条调整现在不能撤销"
UNDO_GONE = "这条调整已经不在了"


class RsiExecutor(RsiMirrorExecutor):
    """`rsi_patch` / `rsi_method` rows. An executed row is a mirror (undone in the RSI list); a
    `proposed` row is a change the RSI cycle left waiting for the user (their teaching_params /
    rsi level was ask, `teach.approval`): approve applies it through `RsiCycle.approve_pending`
    with the ledger's effect inside the transaction that commits it (`store.pinned`: the RSI
    stores and the ledger share the pool, so the effect joins that transaction), under the user's
    RSI cycle lock; reject closes it through `RsiCycle.reject_pending`.

    `cycle_for(user_id, lock)` is a context manager yielding the user's `RsiCycle` (service glue,
    `service.rsi_runs.rsi_decider`); with `lock` it holds the user's cycle lock and raises
    CycleBusy (409, the row stays `approved`: approve again) when a cycle runs. Reject takes no
    lock (a cycle never decides a waiting change, and the ledger serializes approve against
    reject); if closing the record fails, the next cycle closes it from the ledger's rejected
    row (`RsiCycle._close_rejected`)."""

    def __init__(self, pool, cycle_for: Callable[[str, bool], Any], clock: Callable[[], datetime],
                 undo_for: Callable[[str, str], Any] | None = None):
        self._pool, self._cycle_for, self._clock = pool, cycle_for, clock
        self._undo_for = undo_for

    def undo(self, user_id: str, action: ActionRow) -> dict:
        """An executed mirror: the RSI change itself is undone (`undo_for(user, change id)`, the
        RSI list's own undo: `service.rsi_runs.undo_rsi_change`; LookupError gone, ValueError
        refused), so a chat undo needs no detour through 参数; its store moves this mirror row to
        `undone`. Refusal diagnostics stay in the audit; explicit public fields cross the
        error boundary. A busy cycle (409) propagates."""
        if self._undo_for is None:
            raise ActionRefused(UNDO_REFUSED)
        ref = self._ref(action)
        try:
            self._undo_for(user_id, ref)
        except LookupError as exc:
            raise ActionRefused(UNDO_GONE) from exc
        except ValueError as exc:
            raise ActionRefused(str(exc),
                                public_message=getattr(exc, "public_message", None),
                                code=getattr(exc, "error_code", None) or "undo_unavailable",
                                params=getattr(exc, "params", None)) from exc
        return {"undone": ref}

    @staticmethod
    def _ref(action: ActionRow) -> str:
        ref = action.content.get("patch_id") or action.content.get("change_id")
        if not ref:
            raise ActionRefused(GONE_DETAIL)
        return ref

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        ref = self._ref(action)

        def on_accept() -> None:        # inside the RSI commit: atomic with it, or not at all
            if not pinned.is_pinned(self._pool):
                raise RuntimeError("the approval effect must run inside the RSI transaction")
            with pinned.connection(self._pool) as conn:
                effect(conn)
        try:
            with self._cycle_for(user_id, True) as cycle:
                cycle.approve_pending(user_id, ref, now=self._clock(), on_accept=on_accept)
        except PendingStale as exc:
            raise ActionRefused(str(exc)) from exc
        except PendingGone as exc:
            raise ActionRefused(GONE_DETAIL) from exc
        return {"applied": ref}

    def rejected(self, user_id: str, action: ActionRow) -> None:
        ref = action.content.get("patch_id") or action.content.get("change_id")
        if not ref:
            return
        try:
            with self._cycle_for(user_id, False) as cycle:
                cycle.reject_pending(user_id, ref, now=self._clock())
        except PendingGone:
            log.debug("rsi change %s was not waiting any more", ref)


class PreferenceExecutor:
    """`preference` rows (a 不感兴趣 / 减少提醒 click) are written already executed; the only
    operation is undo: the card's not-interested counter goes back by one (floor 0; the time is
    cleared at 0) and the card, if there is one, is `shown` again unless it has expired. A 减少提醒
    row also carries the click time before it (`previous_at`: undoing the newest click gives the
    earlier cooldown back) and the accepted count at the click (a review started since has reset
    the counter this click added: nothing to take back). Counter, card and ledger status move in
    one transaction."""

    def __init__(self, pool, clock: Callable[[], datetime]):
        self._pool, self._clock = pool, clock

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        raise ActionRefused("这条偏好已经记录了")

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    def undo(self, user_id: str, action: ActionRow) -> dict:
        now = self._clock()
        with self._pool.connection() as conn, conn.transaction():
            key = action.content["dedup_key"]
            c = store.get_counters(conn, user_id, key)
            n = c.not_interested
            if action.content.get("accepted", c.accepted) == c.accepted:
                n = max(0, n - 1)
                last = c.last_not_interested_at if n else None
                previous = action.content.get("previous_at")
                if n and previous and last == action.created_at:
                    last = datetime.fromisoformat(previous)
                store.put_counters(conn, user_id, key, replace(
                    c, not_interested=n, last_not_interested_at=last))
            sid = action.content.get("suggestion_id")
            restored = sid is not None and store.restore_shown(conn, sid, now)
            store.set_action_status(conn, action.action_id, status="undone", at=now,
                                    from_statuses=("executed",))
        return {"counter": n, "card_restored": restored}


PROMPT_CHANGED = "「{name}」的写法后来又改过了，先撤销后来的那次"
PROMPT_BROKEN = "这条修改的记录不完整，不能撤销"
PROMPT_RETIRED = "「{name}」的输出格式改了，这条旧写法已经存档清掉，不能再撤销"


class PromptOverrideExecutor:
    """`prompt_override` rows (Prompt 设置, `bot.prompts`) are written already executed; undo puts
    back the text from before the change (or removes the override when there was none). Refused
    when the step's wording changed again since: undo the later change first. Override and ledger
    status move in one transaction."""

    def __init__(self, pool, clock: Callable[[], datetime]):
        self._pool, self._clock = pool, clock

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        raise ActionRefused("这条修改已经生效了")

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    def undo(self, user_id: str, action: ActionRow) -> dict:
        content = action.content if isinstance(action.content, dict) else {}
        key, before, after = content.get("step"), content.get("before"), content.get("after")
        if (not isinstance(key, str) or key not in STEP_BY_KEY or "before" not in content
                or "after" not in content
                or not all(v is None or isinstance(v, str) for v in (before, after))):
            raise ActionRefused(PROMPT_BROKEN, public_message=PROMPT_BROKEN, code="prompt_record_incomplete")          # a malformed row: 409, never a crash
        name = STEP_BY_KEY[key].name
        now = self._clock()
        with self._pool.connection() as conn, conn.transaction():
            prompt_store.lock_step(conn, user_id, key)
            # a later change of the same step (even one that came back to this text: A→B→A) must
            # be undone first; the text check also covers anything written outside the ledger
            if prompt_store.newer_change(conn, user_id, key, action.created_at,
                                         action.action_id):
                raise ActionRefused(PROMPT_CHANGED.format(name=name), public_message=PROMPT_CHANGED,
                                    code="prompt_changed", params={"name": name})
            if prompt_store.get_override(conn, user_id, key) != after:
                # the old-format cleanup (reading-mode spec §7 item 1) is no ledger row to undo
                message = PROMPT_RETIRED if prompt_store.retired(conn, user_id, key) else PROMPT_CHANGED
                raise ActionRefused(message.format(name=name), public_message=message,
                                    code="prompt_retired" if message == PROMPT_RETIRED else "prompt_changed",
                                    params={"name": name})
            prompt_store.set_override(conn, user_id, key, before, now, source=prompt_store.source_before(
                conn, user_id, key, before, action.created_at))
            store.set_action_status(conn, action.action_id, status="undone", at=now,
                                    from_statuses=("executed",))
        return {"step": key, "restored": before}
