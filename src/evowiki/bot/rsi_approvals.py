"""`teach.approval.Approvals` over the action ledger (spec 2026-10-14 §4): the RSI cycle asks the
user's level of teaching_params / rsi, and a change that waits for the user becomes a `proposed`
ledger row written on the RSI store's pinned connection (`store.pinned`), so the awaiting record and
its row commit together."""

import logging
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from evowiki.bot.actions import ActionLedger
from evowiki.store import pinned

log = logging.getLogger(__name__)

# The Bot trigger whose RSI cycle is running in this context (B5c, set by the service's
# `bot_rsi_runner`): a change that waits for the user is tied to it (why chain).
BOT_TRIGGER: ContextVar[str | None] = ContextVar("bot_trigger", default=None)


class LedgerApprovals:
    def __init__(self, ledger: ActionLedger, pool):
        self._ledger, self._pool = ledger, pool

    def asks(self, user_id: str, scope: str) -> bool:
        """Fails closed: an unreadable level means ask."""
        try:
            return self._ledger.level(user_id, scope) == "ask"   # type: ignore[arg-type]
        except Exception:
            log.exception("%s level of %s unreadable; RSI asks first", scope, user_id)
            return True

    def propose(self, user_id: str, *, kind: str, scope: str, key: str, summary: str, why: str,
                content: dict[str, Any], at: datetime) -> None:
        """Only inside the RSI store's transaction (else the row and the awaiting record could
        commit apart): RuntimeError otherwise."""
        if not pinned.is_pinned(self._pool):
            raise RuntimeError("an RSI proposal must be written inside the RSI store transaction")
        trigger = BOT_TRIGGER.get()
        with pinned.connection(self._pool) as conn:
            self._ledger.propose_in(conn, user_id, kind=kind, scope=scope, source="rsi",
                                    summary=summary, why=why, content=content, key=key,
                                    level="ask", trigger_id=trigger)

    def rejected(self, user_id: str, key: str) -> bool:
        row = self._ledger.get_by_key(key)
        return row is not None and row.user_id == user_id and row.status == "rejected"
