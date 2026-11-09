# tests/store/test_harness_store.py
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from evowiki.content.models import Lang
from evowiki.harness.pack import HarnessInvalid
from evowiki.store import harness as store
from evowiki.store import prompts
from evowiki.store.db import fetch_all
from evowiki.store.users import UserRow, create_user

T0 = datetime(2026, 10, 21, 1, 0, tzinfo=UTC)


def _user(pool, uid="u1"):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id=uid, background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))


def test_empty_pack(pool):
    _user(pool)
    with pool.connection() as conn:
        p = store.load_pack(conn, "u1")
    assert p.empty and p.version == 0


def test_set_reads_back_and_versions(pool):
    _user(pool)
    with pool.connection() as conn:
        v1 = store.set_value(conn, "u1", "H4.deep.max_tool_calls", 100, source="user",
                             why="test", now=T0)
        v2 = store.set_value(conn, "u1", "H1.write", "先举例", source="user", why="t", now=T0)
        same = store.set_value(conn, "u1", "H1.write", "先举例", source="user", why="t", now=T0)
        p = store.load_pack(conn, "u1")
    assert (v1, v2, same) == (1, 2, None)
    assert p.version == 2 and p.int("H4.deep.max_tool_calls", 80) == 100
    assert p.guidance() == {"write": "先举例"}


def test_clear_is_a_version_too(pool):
    _user(pool)
    with pool.connection() as conn:
        store.set_value(conn, "u1", "H4.deep.max_tool_calls", 100, source="user", why="t", now=T0)
        v = store.set_value(conn, "u1", "H4.deep.max_tool_calls", None, source="rollback",
                            why="撤销", now=T0 + timedelta(seconds=1))
        p = store.load_pack(conn, "u1")
        h = store.history(conn, "u1")
    assert v == 2 and p.empty and p.version == 2
    assert [(r.version, r.before, r.after, r.source) for r in h] == [
        (2, 100, None, "rollback"), (1, None, 100, "user")]


def test_invalid_value_writes_nothing(pool):
    _user(pool)
    with pool.connection() as conn:
        with pytest.raises(HarnessInvalid):
            store.set_value(conn, "u1", "H4.deep.max_tool_calls", 999, source="user", why="t",
                            now=T0)
        assert store.load_pack(conn, "u1").empty


def test_h2_name_of_no_tool_is_refused_at_write(pool):
    _user(pool)
    with pool.connection() as conn:
        with pytest.raises(HarnessInvalid, match="没有这个设置"):
            store.set_value(conn, "u1", "H2.deep.call_tool", "说明", source="user", why="t",
                            now=T0)               # reserved: no forged tool can have this name
        v = store.set_value(conn, "u1", "H2.deep.place_component", "说明", source="user",
                            why="t", now=T0)
        assert v == 1 and store.load_pack(conn, "u1").version == 1
        # a pattern-valid unknown name may be a forged tool (P3): stored, skipped where applied
        assert store.set_value(conn, "u1", "H2.deep.no_such_tool", "说明", source="user",
                               why="t", now=T0) == 2


def test_history_is_append_only(pool):
    _user(pool)
    with pool.connection() as conn:
        store.set_value(conn, "u1", "H1.write", "a", source="user", why="t", now=T0)
        with pytest.raises(psycopg.Error):
            conn.execute("update harness_version set why = 'x'")


def test_prompt_store_is_h1(pool):
    _user(pool)
    with pool.connection() as conn:
        prompts.set_override(conn, "u1", "write", "先举例", T0)
        assert prompts.get_override(conn, "u1", "write") == "先举例"
        assert prompts.list_overrides(conn, "u1") == {"write": "先举例"}
        assert store.load_pack(conn, "u1").guidance() == {"write": "先举例"}
        assert store.source_of(conn, "u1") == {"H1.write": "user"}
        prompts.set_override(conn, "u1", "write", None, T0)
        assert prompts.get_override(conn, "u1", "write") is None


_MIGRATION = Path(__file__).parents[2] / "src/evowiki/store/migrations/0063_harness.sql"


def test_migration_copies_prompt_override(pool, admin_pool):
    """The two INSERT…SELECT statements of 0063 (sliced from the file) turn old prompt_override
    rows into dense per-user versions ordered by (updated_at, step)."""
    sql = _MIGRATION.read_text(encoding="utf-8")
    copy = sql[sql.index("insert into harness_version"):sql.index("drop table prompt_override")]
    _user(pool, "a")
    _user(pool, "b")
    t1 = T0 + timedelta(days=1)
    with admin_pool.connection() as conn:          # migration SQL runs as the owner
        conn.execute("create table prompt_override (user_id text not null references app_user "
                     "(user_id), step text not null, text text not null, "
                     "updated_at timestamptz not null, primary key (user_id, step))")
        conn.execute("insert into prompt_override values ('a','write','x2',%s),('a','path','x1',%s),"
                     "('a','outline','x0',%s),('b','write','y',%s)", (t1, T0, T0, T0))
        conn.execute(copy)
        versions = [(r["user_id"], r["version"], r["key"], r["after"]) for r in fetch_all(
            conn, "select * from harness_version order by user_id, version")]
        values = {(r["user_id"], r["key"]): (r["value"], r["version"]) for r in fetch_all(
            conn, "select * from harness_value")}
        pa, pb = store.load_pack(conn, "a"), store.load_pack(conn, "b")
    assert versions == [("a", 1, "H1.outline", "x0"), ("a", 2, "H1.path", "x1"),
                        ("a", 3, "H1.write", "x2"), ("b", 1, "H1.write", "y")]
    assert values[("a", "H1.write")] == ("x2", 3) and values[("a", "H1.outline")] == ("x0", 1)
    assert values[("b", "H1.write")] == ("y", 1)
    assert pa.version == 3 and pa.guidance() == {"outline": "x0", "path": "x1", "write": "x2"}
    assert pb.guidance() == {"write": "y"}
