import psycopg
import pytest

from evowiki.store.migrate import MIGRATIONS_DIR, migrate


def test_migrations_apply_once_and_enable_pgvector(db_url):
    with psycopg.connect(db_url) as conn:
        assert migrate(conn) == []  # db_url is already migrated
        ext = conn.execute("select extversion from pg_extension where extname='vector'").fetchone()
        assert ext is not None
        ids = [r[0] for r in conn.execute("select id from schema_migrations order by id")]
    assert ids == sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))


def test_forbid_mutation_function_blocks_updates(db_url):
    with psycopg.connect(db_url) as conn:
        conn.execute("create table t_ao (x int)")
        conn.execute("create trigger t_ao_ro before update or delete on t_ao "
                     "for each row execute function evowiki_forbid_mutation()")
        conn.execute("insert into t_ao values (1)")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("update t_ao set x = 2")


def test_database_url_requires_env():
    from evowiki.store.db import database_url
    with pytest.raises(RuntimeError, match="EVOWIKI_DATABASE_URL"):
        database_url({})
    assert database_url({"EVOWIKI_DATABASE_URL": "postgresql:///x"}) == "postgresql:///x"


def test_pool_uses_dict_rows_and_pgvector(pool):
    from pgvector import Vector

    with pool.connection() as conn:
        row = conn.execute("select '[1,2,3]'::vector as v").fetchone()
        assert conn.autocommit is False
        assert conn.execute("show timezone").fetchone()["TimeZone"] == "UTC"
    assert isinstance(row["v"], Vector)  # adapter registered, not a plain str
    assert row["v"].to_list() == [1.0, 2.0, 3.0]


def test_0066_applies_on_a_db_that_already_has_0067_to_0070(db_url):
    """migrate applies by presence in name order: a DB that took 0067-0070 (forge, from master)
    before 0066 (harness gate, this branch) gets 0066 on its next migrate."""
    later = ["0067_forge", "0068_forge_p2", "0069_forge_patch", "0070_page_run_patch"]
    tables = ("harness_gate", "harness_patch_observation")
    with psycopg.connect(db_url) as conn:
        conn.execute("delete from schema_migrations where id = '0066_harness_gate'")
        conn.execute("drop table harness_patch_observation, harness_gate")   # triggers go too
        conn.commit()
        assert migrate(conn) == ["0066_harness_gate"]
        ids = {r[0] for r in conn.execute("select id from schema_migrations")}
        found = {r[0] for r in conn.execute(
            "select table_name from information_schema.tables where table_name = any(%s)",
            (list(tables),))}
        triggers = {r[0] for r in conn.execute(
            "select tgname from pg_trigger where tgrelid in "
            "('harness_gate'::regclass, 'harness_patch_observation'::regclass)")}
    assert {"0066_harness_gate", *later} <= ids
    assert found == set(tables)
    assert {"harness_gate_append_only", "harness_patch_observation_append_only"} <= triggers
