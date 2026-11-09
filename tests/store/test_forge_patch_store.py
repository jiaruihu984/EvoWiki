from datetime import UTC, datetime

import psycopg
import pytest

from evowiki.store import forge as fstore
from evowiki.store import forge_patch as ps

T = datetime(2026, 10, 23, 9, tzinfo=UTC)


def _cand(conn, cid="fc_p1"):
    fstore.insert_candidate(conn, fstore.CandidateRow(
        id=cid, user_id=None, kind="patch", name=None, parent_id=None, spec={"goal": "g"},
        evidence={"citations": ["signal:1"]}, files={}, build=None, status="built", run_id=None,
        cost_usd=0.1, created_at=T, updated_at=T, origin="round"))


def _row(cid="fc_p1", state="built"):
    return ps.PatchRow(candidate_id=cid, user_id=None, title="修好重复题目", base_commit="a" * 40,
                       commit="b" * 40, branch=f"forge/{cid}", state=state, live_commit=None,
                       revert_commit=None, reason="", gate_ref=None, cost_usd=0.1,
                       created_at=T, updated_at=T)


def test_roundtrip_and_states(pool):
    with pool.connection() as conn:
        _cand(conn)
        ps.insert_patch(conn, _row())
        ps.set_state(conn, "fc_p1", "queued", now=T, gate_ref="g1")
        ps.set_state(conn, "fc_p1", "landed", now=T, live_commit="c" * 40)
        got = ps.get_patch(conn, "fc_p1")
        assert got.state == "landed" and got.live_commit == "c" * 40 and got.gate_ref == "g1"
        assert [p.candidate_id for p in ps.patches(conn, states=["landed"])] == ["fc_p1"]


def test_bad_state_rejected(pool):
    with pool.connection() as conn:
        _cand(conn)
        ps.insert_patch(conn, _row())
        with pytest.raises(psycopg.Error):
            conn.execute("update forge_patch set state = 'nope' where candidate_id = 'fc_p1'")


def test_events_append_only(pool):
    with pool.connection() as conn:
        ps.log_event(conn, "restart", candidate_id=None, detail={"why": "landing"}, now=T)
        assert ps.events(conn)[0]["kind"] == "restart"
        with pytest.raises(psycopg.Error):
            conn.execute("delete from forge_live_event")


def test_live_requests_taken_once(pool):
    with pool.connection() as conn:
        ps.set_live_request(conn, "revert_all", by="u1", now=T)
        assert [r["op"] for r in ps.take_live_requests(conn)] == ["revert_all"]
        assert ps.take_live_requests(conn) == []


def test_add_cost_unknown_stays_unknown(pool):
    with pool.connection() as conn:
        _cand(conn)
        ps.insert_patch(conn, _row())
        ps.add_cost(conn, "fc_p1", 0.05)
        assert ps.get_patch(conn, "fc_p1").cost_usd == pytest.approx(0.15)
        ps.add_cost(conn, "fc_p1", None)
        assert ps.get_patch(conn, "fc_p1").cost_usd is None
