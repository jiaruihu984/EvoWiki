# tests/store/test_harness_rsi_store.py
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from evowiki.content.models import Lang
from evowiki.store import harness_rsi as s
from evowiki.store.users import UserRow, create_user

T0 = datetime(2026, 10, 21, 1, 0, tzinfo=UTC)
UTC_TZ = ZoneInfo("UTC")


def _user(pool, uid="u1"):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id=uid, background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))


def test_round_round_trip(pool):
    _user(pool)
    with pool.connection() as conn:
        rid = s.start_round(conn, "u1", finding="练习题太难", now=T0)
        r = s.last_round(conn, "u1")
        assert (r.round_id, r.status, r.finding, r.finished_at) == (rid, "running", "练习题太难", None)
        s.finish_round(conn, rid, "applied", reason="ok", signal_ids=["a"], cases=[1],
                       candidates={"g": 1}, comparisons=[2], decision={"d": 1}, diff={"k": "v"},
                       cost=0.5, improver_run_id="run1", now=T0 + timedelta(minutes=1))
        r = s.last_round(conn, "u1")
        assert r.status == "applied" and r.diff == {"k": "v"} and r.cost == 0.5
        assert r.signal_ids == ["a"] and r.improver_run_id == "run1"
        assert r.finished_at == T0 + timedelta(minutes=1)
        rid2 = s.start_round(conn, "u1", finding=None, now=T0 + timedelta(hours=1))
        assert [x.round_id for x in s.rounds(conn, "u1", 10)] == [rid2, rid]
        assert len(s.rounds(conn, "u1", 1)) == 1
        assert s.last_round(conn, "nobody") is None


def test_last_consuming_round_skips_thin_skipped_error_and_running(pool):
    _user(pool)
    assert s.CONSUMING == ("applied", "asked", "failed", "no_change")
    with pool.connection() as conn:
        assert s.last_consuming_round(conn, "u1") is None
        for i, st in enumerate(("no_change", "thin", "skipped", "error", "running")):
            rid = s.start_round(conn, "u1", finding=None, now=T0 + timedelta(hours=i))
            if i == 0:
                first = rid
            if st != "running":
                s.finish_round(conn, rid, st, reason="x", now=T0 + timedelta(hours=i))
        assert s.last_round(conn, "u1").status == "running"
        assert s.last_consuming_round(conn, "u1").round_id == first


def test_round_status_transitions(pool):
    _user(pool)
    with pool.connection() as conn:
        rid = s.start_round(conn, "u1", finding=None, now=T0)
        s.finish_round(conn, rid, "thin", reason="x", now=T0)
    with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException, match="forbidden"):
        s.finish_round(conn, rid, "applied", reason="again", now=T0)
    with pool.connection() as conn:
        rid = s.start_round(conn, "u1", finding=None, now=T0)
        with pytest.raises(psycopg.errors.RaiseException, match="forbidden"):
            s.finish_round(conn, rid, "running", reason="x", now=T0)
    with pool.connection() as conn:
        rid = s.start_round(conn, "u1", finding=None, now=T0)
        with pytest.raises(psycopg.errors.RaiseException, match="only the result"):
            conn.execute("update harness_round set user_id = 'zz', status = 'failed', "
                         "finished_at = now() where round_id = %s", (rid,))
    with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("delete from harness_round")


def test_change_lifecycle(pool):
    _user(pool)
    with pool.connection() as conn:
        rid = s.start_round(conn, "u1", finding=None, now=T0)
        a = s.record_change(conn, "u1", rid, {"H1.write": "x"}, "asked", cost_before=1.0,
                            cost_after=0.8, reason="省钱", now=T0)
        b = s.record_change(conn, "u1", rid, {"H1.ask": "y"}, "applied", version=3,
                            now=T0 + timedelta(minutes=1))
        assert [c.change_id for c in s.applied_changes(conn, "u1")] == [b]
        s.settle_change(conn, a, "applied", version=4, now=T0 + timedelta(hours=1))
        got = s.applied_changes(conn, "u1")
        assert [c.change_id for c in got] == [b, a]  # newest first by created_at
        assert got[1].version == 4 and got[1].cost_before == 1.0 and got[1].reason == "省钱"
        assert got[1].settled_at == T0 + timedelta(hours=1)
        s.settle_change(conn, b, "undone", now=T0 + timedelta(hours=2))
        assert [c.change_id for c in s.applied_changes(conn, "u1")] == [a]


def test_change_forbidden_transitions(pool):
    _user(pool)
    with pool.connection() as conn:
        rid = s.start_round(conn, "u1", finding=None, now=T0)
        a = s.record_change(conn, "u1", rid, {}, "asked", now=T0)
        b = s.record_change(conn, "u1", rid, {}, "applied", version=1, now=T0)
        s.settle_change(conn, a, "rejected", now=T0)
        s.settle_change(conn, b, "rolled_back", now=T0)
    for cid, to in ((a, "applied"), (b, "applied"), (b, "undone")):
        with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException, match="forbidden"):
            s.settle_change(conn, cid, to, now=T0)
    with pool.connection() as conn:
        c = s.record_change(conn, "u1", rid, {}, "applied", version=1, now=T0)
        with pytest.raises(psycopg.errors.RaiseException, match="may change"):
            s.settle_change(conn, c, "undone", version=9, now=T0)
    with pool.connection() as conn:
        c = s.record_change(conn, "u1", rid, {}, "applied", version=1, now=T0)
        with pytest.raises(psycopg.errors.RaiseException, match="may change"):
            conn.execute("update harness_change set diff = '{\"a\":1}', status = 'undone', "
                         "settled_at = now() where change_id = %s", (c,))
    with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("delete from harness_change")


def test_experience_ranking_and_query(pool):
    _user(pool)
    now = T0 + timedelta(days=100)
    with pool.connection() as conn:
        old = s.add_experience(conn, "u1", {"agent": "deep", "kind": "a"}, {"c": 1}, {"e": 1},
                               "effective", "user", weight=1.0, now=now - timedelta(days=28))
        new = s.add_experience(conn, "u1", {"agent": "deep", "kind": "b"}, {"c": 2}, {"e": 2},
                               "harmful", "general", weight=0.6, now=now)
        mid = s.add_experience(conn, "u1", {"agent": "ask"}, {"c": 3}, {"e": 3},
                               "ineffective", "user", weight=1.0, now=now - timedelta(days=14))
        # scores: new 0.6, mid 0.5, old 0.25
        got = s.experience(conn, "u1", query=None, limit=10, now=now)
        assert [g.exp_id for g in got] == [new, mid, old]
        assert (got[0].verdict, got[0].scope, got[0].situation) == (
            "harmful", "general", {"agent": "deep", "kind": "b"})
        assert [e.exp_id for e in s.experience(conn, "u1", query={"agent": "deep"}, limit=10,
                                               now=now)] == [new, old]
        assert len(s.experience(conn, "u1", query=None, limit=1, now=now)) == 1
    with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("update harness_experience set weight = 2")


def test_month_cost(pool):
    _user(pool)
    _user(pool, "u2")
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    with pool.connection() as conn:
        for uid, when, cost in (("u1", now, 0.5), ("u1", now - timedelta(days=5), 0.25),
                                ("u1", datetime(2026, 9, 30, 23, 0, tzinfo=UTC), 9.0),
                                ("u2", now, 7.0)):
            r = s.start_round(conn, uid, finding=None, now=when)
            s.finish_round(conn, r, "applied", reason="x", cost=cost, now=when)
        s.start_round(conn, "u1", finding=None, now=now)  # running, cost null
        assert s.month_cost(conn, "u1", now, UTC_TZ) == pytest.approx(0.75)
        assert s.month_cost(conn, "u2", now, UTC_TZ) == pytest.approx(7.0)
        assert s.month_cost(conn, "nobody", now, UTC_TZ) == 0.0


def test_observation_points_are_ordered_and_append_only(pool):
    _user(pool)
    with pool.connection() as conn:
        rid = s.start_round(conn, "u1", finding=None, now=T0)
        cid = s.record_change(conn, "u1", rid, {"ops": []}, "applied", version=1, now=T0)
        a = s.add_observation(conn, "u1", cid, "harmful", [{"metric": "page_cost"}], now=T0)
        b = s.add_observation(conn, "u1", cid, "harmful", [], note="auto_rollback", now=T0)
        got = s.observations(conn, "u1", cid)
        assert [o.obs_id for o in got] == [a, b] and got[1].note == "auto_rollback"
        assert got[0].deltas == [{"metric": "page_cost"}] and got[0].lesson_id is None
    with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("update harness_observation set verdict = 'helpful'")
    with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("delete from harness_observation")
