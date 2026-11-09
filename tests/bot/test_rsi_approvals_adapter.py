"""bot.rsi_approvals.LedgerApprovals without a database: fail closed on an unreadable level, and
never write a proposal outside the RSI store's pinned transaction."""

from datetime import UTC, datetime

import pytest

from evowiki.bot.rsi_approvals import LedgerApprovals


class Ledger:
    def __init__(self, level=None, row=None):
        self._level, self._row = level, row

    def level(self, user_id, scope):
        if self._level is None:
            raise RuntimeError("db down")
        return self._level

    def get_by_key(self, key):
        return self._row


def test_asks_reads_the_level_and_fails_closed():
    assert LedgerApprovals(Ledger("ask"), object()).asks("u1", "rsi")
    assert not LedgerApprovals(Ledger("auto_undo"), object()).asks("u1", "rsi")
    assert LedgerApprovals(Ledger(None), object()).asks("u1", "rsi")


def test_propose_refuses_outside_the_rsi_transaction():
    with pytest.raises(RuntimeError, match="inside the RSI store transaction"):
        LedgerApprovals(Ledger("ask"), object()).propose(
            "u1", kind="rsi_patch", scope="rsi", key="rsi:u1:p", summary="s", why="w",
            content={}, at=datetime(2026, 10, 15, tzinfo=UTC))


def test_rejected_only_for_the_users_rejected_row():
    class Row:
        def __init__(self, user_id, status):
            self.user_id, self.status = user_id, status
    assert LedgerApprovals(Ledger(row=Row("u1", "rejected")), object()).rejected("u1", "k")
    assert not LedgerApprovals(Ledger(row=Row("u2", "rejected")), object()).rejected("u1", "k")
    assert not LedgerApprovals(Ledger(row=Row("u1", "proposed")), object()).rejected("u1", "k")
    assert not LedgerApprovals(Ledger(row=None), object()).rejected("u1", "k")
