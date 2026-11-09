# tests/store/test_harness_gate.py
from datetime import UTC, datetime

import psycopg
import pytest

from evowiki.content.models import Lang
from evowiki.store import harness_rsi
from evowiki.store.users import UserRow, create_user

NOW = datetime(2026, 10, 23, 1, 0, tzinfo=UTC)


def _setup(conn, uid="u1"):
    create_user(conn, UserRow(user_id=uid, background="none", condition="S2", lang=Lang.EN,
                              created_at=NOW))
    return harness_rsi.start_round(conn, uid, finding="f", now=NOW)


def test_gate_roundtrip_and_append_only(pool):
    with pool.connection() as conn:
        rid = _setup(conn)
        ref = harness_rsi.record_gate(conn, "u1", rid, "tool:fc_abc", "pass", ["证据充分，通过"],
                                      {"wins": 3}, now=NOW)
        assert ref.startswith("hg_")
        assert harness_rsi.get_gate(conn, "u1", "hg_missing") is None
        assert harness_rsi.get_gate(conn, "u2", ref) is None
        g = harness_rsi.get_gate(conn, "u1", ref)
        assert (g.subject, g.reasons, g.detail, g.at, g.round_id) == (
            "tool:fc_abc", ["证据充分，通过"], {"wins": 3}, NOW, rid)
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("update harness_gate set verdict = 'fail' where gate_id = %s", (ref,))
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("delete from harness_gate where gate_id = %s", (ref,))


def test_gate_subject_and_verdict_checks(pool):
    with pool.connection() as conn:
        rid = _setup(conn)
        assert harness_rsi.record_gate(conn, "u1", rid, "diff", "thin", [], {}, now=NOW)
        with pytest.raises(psycopg.errors.CheckViolation):
            harness_rsi.record_gate(conn, "u1", rid, "bogus", "pass", [], {}, now=NOW)
