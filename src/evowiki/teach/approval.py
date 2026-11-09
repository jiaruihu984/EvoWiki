"""The user's approval before an RSI change goes live (Bot setting 「讲解规则和学习状态评估」 = 要我批准,
spec 2026-10-14 §4). The statistical Gate still decides whether a change is worth proposing; with
the user's level `ask` a change that passed is NOT applied: its record waits (`awaiting_user`) and a
`proposed` row goes into the action ledger (`propose`, inside the record's own store transaction,
so record and row commit together). The ledger's approve runs `RsiCycle.approve_pending` exactly
once (its effect commits with the change); reject runs `RsiCycle.reject_pending`.

Scopes (the same as the ledger mirrors of `store.rsi` / `store.rsi_layer2`): track-A fit patches
are `teaching_params`; the layer-2 method changes (A2) and the harness loop's changes are `rsi`.
Automatic rollbacks of a harmful change and the user's undo are never asked.

teach/ never imports bot/: the service plugs `bot.rsi_approvals.LedgerApprovals` in. Without an
`Approvals` (None) nothing is ever asked (the behaviour before this setting)."""

from datetime import datetime
from typing import Any, Protocol

TEACHING_PARAMS = "teaching_params"
RSI = "rsi"
PATCH_KIND = "rsi_patch"
METHOD_KIND = "rsi_method"


def patch_key(user_id: str, patch_id: str) -> str:
    """The ledger key of an A / B patch (its proposal and its mirror share it)."""
    return f"rsi:{user_id}:{patch_id}"


def method_key(user_id: str, change_id: str) -> str:
    """The ledger key of a layer-2 method change."""
    return f"rsi-method:{user_id}:{change_id}"


class Approvals(Protocol):
    def asks(self, user_id: str, scope: str) -> bool:
        """The user's level of `scope` is `ask` (an unreadable level counts as ask: fail closed)."""

    def propose(self, user_id: str, *, kind: str, scope: str, key: str, summary: str, why: str,
                content: dict[str, Any], at: datetime) -> None:
        """Record the pending approval; called inside the store transaction that stores the
        awaiting record."""

    def rejected(self, user_id: str, key: str) -> bool:
        """The ledger row of `key` was rejected (the cycle closes a record whose reject hook
        did not get to run)."""


class PendingGone(LookupError):
    """No change waiting for the user under this id (unknown, another user's, or decided)."""


class PendingStale(ValueError):
    """What the change was checked against moved since it was proposed; it was closed without
    being applied. The message is shown to the user (Chinese)."""


STALE_A = "学习状态评估在提议之后又变过了，这条调整已作废，没有应用"
STALE_B = "讲解规则在提议之后又改过了，这条改动已作废，没有应用"
STALE_METHOD = "这个方法在提议之后又变过了，这条改动已作废，没有应用"


def line(kind: str, text: str) -> dict[str, str]:
    """One 改动对比 line (`ActionOut.diff`): kind ctx / add / del."""
    return {"kind": kind, "text": text}
