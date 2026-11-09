"""PgLayer2Store (migrations 0041, 0042): the outcome series and the method registry round-trip like
MemoryLayer2Store; points and method versions are append-only, method changes upserted."""

from datetime import UTC, datetime, timedelta, timezone

import psycopg
import pytest

from evowiki.store.rsi_layer2 import PgLayer2Store
from evowiki.teach.contracts import (
    AnchorDelta,
    FitModel,
    MethodChange,
    MethodVersion,
    OutcomeOut,
)
from evowiki.teach.layer2 import method_registry
from evowiki.teach.layer2.store import MemoryLayer2Store, record_point
from tests.runtime.helpers import make_user

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
CST = timezone(timedelta(hours=8))


def test_terminal_method_without_mirror_does_not_create_action(pool):
    PgLayer2Store(pool).put_method_change("u", MethodChange(
        change_id="past", kind="A2", status="rolled_back", summary="s", why="w",
        from_version=1, to_version=2, created_at=T0, updated_at=T0))
    with pool.connection() as conn:
        assert conn.execute("select count(*) as n from agent_action").fetchone()["n"] == 0


def _o(cid: str, at: datetime, verdict="pending", n=12) -> OutcomeOut:
    return OutcomeOut(change_id=cid, at=at, verdict=verdict, note="看不出差别",
                      deltas=(AnchorDelta(metric="check_correct", before=0.4, after=0.8, delta=0.4,
                                          lo=0.1, hi=0.6, n_before=20, n_after=n),))


@pytest.mark.parametrize("make", ["pg", "memory"])
def test_round_trip_order_and_utc(pool, make):
    s = PgLayer2Store(pool) if make == "pg" else MemoryLayer2Store()
    late = _o("c1", T0 + timedelta(hours=2), "helpful", 30)
    s.add_outcome_point("u", late)
    s.add_outcome_point("u", _o("c1", (T0 + timedelta(hours=1)).astimezone(CST)))
    s.add_outcome_point("u", _o("c2", T0))
    s.add_outcome_point("v", _o("c1", T0))
    pts = s.outcome_points("u", "c1")
    assert [p.verdict for p in pts] == ["pending", "helpful"]
    assert pts[0].at == T0 + timedelta(hours=1) and pts[0].at.utcoffset() == timedelta(0)
    assert pts[1] == late
    assert [p.change_id for p in s.outcome_points("u")] == ["c2", "c1", "c1"]
    assert s.outcome_points("nobody") == []


def test_record_point_over_pg(pool):
    s = PgLayer2Store(pool)
    assert record_point(s, "u", _o("c1", T0, n=12))
    assert not record_point(s, "u", _o("c1", T0 + timedelta(hours=1), n=12))
    assert not record_point(s, "u", _o("c1", T0 + timedelta(hours=2), n=21))
    assert record_point(s, "u", _o("c1", T0 + timedelta(hours=3), n=22))     # MIN_EVENTS more
    assert len(s.outcome_points("u", "c1")) == 2


def test_outcome_points_are_append_only(pool):
    s = PgLayer2Store(pool)
    s.add_outcome_point("u", _o("c1", T0))
    with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("update rsi_outcome_point set verdict = 'helpful'")
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("delete from rsi_outcome_point")
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into rsi_outcome_point (user_id, change_id, at, verdict, body) "
                         "values ('u', 'c', now(), 'great', '{}')")
        conn.rollback()
    assert len(s.outcome_points("u")) == 1


# ---- method registry (migration 0042) ----

DEFAULT = method_registry.DEFAULT_FIT_MODEL


@pytest.mark.parametrize("make", ["pg", "memory"])
def test_method_versions_round_trip_through_the_registry(pool, make):
    s = PgLayer2Store(pool) if make == "pg" else MemoryLayer2Store()
    assert method_registry.current_model(s, "u") == DEFAULT
    fm = FitModel(version=1, paths=("slip",))
    method_registry.commit(s, "u", "A2", fit_model=fm, change_id="mc1",
                           now=(T0 + timedelta(hours=1)).astimezone(CST))
    assert method_registry.current_model(s, "u") == fm.model_copy(update={"version": 2})
    method_registry.rollback(s, "u", "A2", 1, now=T0 + timedelta(hours=2))
    rows = s.method_versions("u", "A2")
    assert [(r.version, r.change_id) for r in rows] == [(1, None), (2, "mc1"), (3, None)]
    assert rows[1].fit_model == fm.model_copy(update={"version": 2})
    assert rows[1].at == T0 + timedelta(hours=1) and rows[1].at.utcoffset() == timedelta(0)
    assert method_registry.current_model(s, "u") == DEFAULT.model_copy(update={"version": 3})
    assert s.method_versions("v", "A2") == []
    with pytest.raises(ValueError, match="not the next"):
        s.add_method_version("u", rows[0])


@pytest.mark.parametrize("make", ["pg", "memory"])
def test_method_version_gaps_are_rejected_without_advancing_registry(pool, make):
    s = PgLayer2Store(pool) if make == "pg" else MemoryLayer2Store()

    def version(n):
        return MethodVersion(kind="A2", version=n, at=T0,
                             fit_model=DEFAULT.model_copy(update={"version": n}))

    with pytest.raises(ValueError, match=r"u/A2: version 2 is not the next \(1\)"):
        s.add_method_version("u", version(2))
    assert s.method_versions("u", "A2") == []
    s.add_method_version("u", version(1))
    with pytest.raises(ValueError, match=r"u/A2: version 3 is not the next \(2\)"):
        s.add_method_version("u", version(3))
    s.add_method_version("u", version(2))
    assert [v.version for v in s.method_versions("u", "A2")] == [1, 2]


def test_a_stored_version_with_the_b2_era_null_key_still_reads(pool):
    """A2 rows written while B2 existed carry `"b_method": null` in their body."""
    from psycopg.types.json import Jsonb
    s = PgLayer2Store(pool)
    body = {"kind": "A2", "version": 1, "at": T0.isoformat(), "change_id": None,
            "fit_model": DEFAULT.model_dump(mode="json"), "b_method": None}
    with pool.connection() as conn:
        conn.execute("insert into rsi_method_version (user_id, kind, version, at, change_id, body) "
                     "values ('u', 'A2', 1, %s, null, %s)", (T0, Jsonb(body)))
    assert s.method_versions("u", "A2")[0].fit_model == DEFAULT


@pytest.mark.parametrize("make", ["pg", "memory"])
def test_method_changes_upsert_and_belong_to_one_user(pool, make):
    if make == "pg":
        make_user(pool, "u")        # the action ledger mirror of a change references app_user
    s = PgLayer2Store(pool) if make == "pg" else MemoryLayer2Store()
    c = MethodChange(change_id="mc1", kind="A2", status="applied", summary="估计时少考虑一项",
                     why="猜得更准", from_version=1, to_version=2, gate={"checks": ["sim"]},
                     created_at=T0 + timedelta(hours=1))
    s.put_method_change("u", c)
    s.put_method_change("u", MethodChange(change_id="mc0", kind="A2", status="gate_failed",
                                          summary="多拟合一个参数", why="w", from_version=1,
                                          created_at=T0))
    later = c.model_copy(update={"status": "rolled_back", "updated_at": T0 + timedelta(days=3)})
    s.put_method_change("u", later)
    assert s.method_changes("u") == [s.method_changes("u", "A2")[0], later]
    assert [x.change_id for x in s.method_changes("u", "A2")] == ["mc0", "mc1"]
    with pytest.raises(ValueError, match="another user"):
        s.put_method_change("v", c)
    assert s.method_changes("v") == []


def test_method_versions_are_append_only(pool):
    s = PgLayer2Store(pool)
    method_registry.commit(s, "u", "A2", fit_model=FitModel(version=1, paths=("slip",)),
                           change_id="mc1", now=T0)
    with pool.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("update rsi_method_version set change_id = 'x'")
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("delete from rsi_method_version")
        conn.rollback()
    assert len(s.method_versions("u", "A2")) == 2
