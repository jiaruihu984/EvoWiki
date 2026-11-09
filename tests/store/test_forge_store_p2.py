from datetime import UTC, datetime

import psycopg
import pytest

from evowiki.content.models import Lang
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user

T0 = datetime(2026, 10, 22, tzinfo=UTC)


def _user(pool, uid="u1"):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id=uid, background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))


def cand(cid, user="u1", origin="round", status="built", name="double"):
    return store.CandidateRow(id=cid, user_id=user, kind="tool", name=name, parent_id=None,
                              spec={}, evidence={}, files={}, build=None, status=status,
                              run_id=None, cost_usd=None, created_at=T0, updated_at=T0,
                              origin=origin)


def test_origin_round_trips_and_filters(pool):
    _user(pool)
    with pool.connection() as conn:
        store.insert_candidate(conn, cand("fc_a", origin="session:s1"))
        store.insert_candidate(conn, cand("fc_b", origin="session:s1", status="build_failed"))
        store.insert_candidate(conn, cand("fc_c", origin="round"))
        got = store.candidates_by_origin(conn, "u1", "session:s1", statuses=("built",))
        default = store.get_candidate(conn, "fc_c")
    assert [c.id for c in got] == ["fc_a"]
    assert got[0].origin == "session:s1"
    assert default.origin == "round"


def test_vectors_are_tagged(pool):
    _user(pool)
    with pool.connection() as conn:
        store.insert_candidate(conn, cand("fc_a"))
        store.put_vec(conn, "fc_a", embedder="hash", model_version="hash-v1-d8", vec=[0.0] * 8)
        store.put_vec(conn, "fc_a", embedder="hash", model_version="hash-v1-d8", vec=[1.0] * 8)
        assert store.vecs(conn, ["fc_a"], model_version="hash-v1-d8") == {"fc_a": [1.0] * 8}
        assert store.vecs(conn, ["fc_a"], model_version="other") == {}
        assert store.vecs(conn, [], model_version="hash-v1-d8") == {}


def test_settings_with_prefix(pool):
    with pool.connection() as conn:
        store.set_setting(conn, "u1", "opt_in.deep.double", True, T0)
        store.set_setting(conn, "u1", "mask.double", "fc_a", T0)
        store.set_setting(conn, "u1", "optxin.deep.x", True, T0)
        store.set_setting(conn, "u2", "opt_in.deep.other", True, T0)
        assert store.settings_with_prefix(conn, "u1", "opt_in.deep.") == {
            "opt_in.deep.double": True}


def test_user_id_global_is_refused(pool):
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        create_user(conn, UserRow(user_id="global", background="none", condition="S2",
                                  lang=Lang.EN, created_at=T0))
