from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from evowiki.content.models import Lang
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)


def _user(pool, uid="u1"):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id=uid, background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))


def _cand(cid, *, user="u1", name="t", parent=None, status="building", minutes=0):
    t = T0 + timedelta(minutes=minutes)
    return store.CandidateRow(
        id=cid, user_id=user, kind="tool", name=name, parent_id=parent,
        spec={"purpose": "x"}, evidence={"why": "y"}, files={}, build=None, status=status,
        run_id=None, cost_usd=None, created_at=t, updated_at=t)


def test_candidate_round_trip_and_set_build(pool):
    _user(pool)
    with pool.connection() as conn:
        store.insert_candidate(conn, _cand("c1"))
        store.set_build(conn, "c1", build={"ok": True}, files={"a.py": "x = 1\n"},
                        status="built", cost_usd=0.01, now=T0 + timedelta(minutes=1))
        c = store.get_candidate(conn, "c1")
        missing = store.get_candidate(conn, "nope")
    assert missing is None
    assert c.status == "built" and c.files == {"a.py": "x = 1\n"} and c.spec == {"purpose": "x"}
    assert c.build == {"ok": True} and c.cost_usd == 0.01


def test_registry_append_and_history(pool):
    with pool.connection() as conn:
        assert store.registry_head(conn, "global") == (0, {})
        v1 = store.append_registry(conn, "global", {"t": "c1"}, {"op": "promote"}, T0)
        v2 = store.append_registry(conn, "global", {"t": "c2"}, {"op": "promote"}, T0)
        assert (v1, v2) == (1, 2)
        assert store.registry_at(conn, "global", 1) == {"t": "c1"}
        assert store.registry_head(conn, "global") == (2, {"t": "c2"})
        assert store.registry_at(conn, "global", 9) is None
        assert [r.version for r in store.registry_history(conn, "global")] == [2, 1]


def test_registry_is_append_only(pool):
    with pool.connection() as conn:
        store.append_registry(conn, "s", {}, {"op": "promote"}, T0)
    with pytest.raises(psycopg.Error), pool.connection() as conn:
        conn.execute("update forge_registry set entries = '{}'::jsonb where scope = 's'")
    with pytest.raises(psycopg.Error), pool.connection() as conn:
        conn.execute("delete from forge_registry where scope = 's'")


def test_archive_newest_first_includes_failed(pool):
    _user(pool)
    _user(pool, "u2")
    with pool.connection() as conn:
        store.insert_candidate(conn, _cand("c1", minutes=0))
        store.insert_candidate(conn, _cand("c2", parent="c1", status="build_failed", minutes=1))
        store.insert_candidate(conn, _cand("c3", name="other", minutes=2))
        store.insert_candidate(conn, _cand("g1", user=None, minutes=3))
        store.insert_candidate(conn, _cand("x1", user="u2", minutes=4))
        allu = store.archive(conn, "u1")
        named = store.archive(conn, "u1", name="t")
        lim = store.archive(conn, "u1", limit=1)
        glob = store.archive(conn, None)
    assert [c.id for c in allu] == ["c3", "c2", "c1"]
    assert [c.id for c in named] == ["c2", "c1"] and named[0].parent_id == "c1"
    assert [c.id for c in lim] == ["c3"] and [c.id for c in glob] == ["g1"]


def test_setting_upsert(pool):
    with pool.connection() as conn:
        assert store.get_setting(conn, "u1", "enabled") is None
        store.set_setting(conn, "u1", "enabled", True, T0)
        store.set_setting(conn, "u1", "enabled", False, T0)
        assert store.get_setting(conn, "u1", "enabled") is False
