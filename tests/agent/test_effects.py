"""apply_once: same key + same payload → the first result, fn not called again; same key +
different payload → EffectConflict; all inside the caller's transaction (a rollback undoes both)."""
import psycopg
import pytest

from evowiki.agent.effects import EffectConflict, apply_once
from evowiki.store.migrate import migrate


@pytest.fixture
def conn(db_url):
    with psycopg.connect(db_url) as c:
        migrate(c)
        c.execute("create table probe_target (n int)")
        c.commit()
        yield c


def test_second_apply_returns_first_result_without_running(conn):
    runs = []

    def fn():
        runs.append(1)
        conn.execute("insert into probe_target values (1)")
        return {"rows": 1}

    assert apply_once(conn, "k1", {"page": "p1"}, fn) == {"rows": 1}
    conn.commit()
    assert apply_once(conn, "k1", {"page": "p1"}, fn) == {"rows": 1}
    conn.commit()
    assert runs == [1]
    assert conn.execute("select count(*) from probe_target").fetchone()[0] == 1


def test_same_key_different_payload_conflicts(conn):
    apply_once(conn, "k2", {"page": "p1"}, dict)
    conn.commit()
    with pytest.raises(EffectConflict):
        apply_once(conn, "k2", {"page": "p2"}, dict)


def test_rollback_undoes_effect_and_key(conn):
    def fn():
        conn.execute("insert into probe_target values (2)")
        return {}

    apply_once(conn, "k3", {}, fn)
    conn.rollback()
    assert conn.execute("select count(*) from effect_applied where key='k3'").fetchone()[0] == 0
    assert conn.execute("select count(*) from probe_target").fetchone()[0] == 0
