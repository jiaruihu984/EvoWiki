"""tests/pg.py: the long-lived test cluster's pure parts (config, lock, template naming, guards)."""

import subprocess
import sys
import textwrap

import pytest

from tests import pg


def test_the_cluster_defaults_and_overrides(monkeypatch, tmp_path):
    monkeypatch.delenv("EVOWIKI_TEST_PG_DIR", raising=False)
    monkeypatch.delenv("EVOWIKI_TEST_PG_PORT", raising=False)
    assert pg.cluster_port() == 15433 and pg.cluster_dir().name == "evowiki-test-pg"
    monkeypatch.setenv("EVOWIKI_TEST_PG_DIR", str(tmp_path))
    monkeypatch.setenv("EVOWIKI_TEST_PG_PORT", "15999")
    assert pg.cluster_dir() == tmp_path and pg.cluster_port() == 15999


def test_the_cluster_never_takes_the_dev_port(monkeypatch):
    monkeypatch.delenv("EVOWIKI_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("EVOWIKI_TEST_PG_PORT", "15432")
    monkeypatch.setattr(pg, "ensure_cluster", lambda *a: pytest.fail("started"))
    with pytest.raises(RuntimeError, match="dev server"):
        pg.start_admin_url()


def test_conf_logs_outside_the_data_dir_and_include_is_written_once(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "postgresql.conf").write_text("port = 5432\n", encoding="utf-8")
    text = pg.cluster_conf(tmp_path, 15999)
    assert "port = 15999" in text and f"log_directory = '{(tmp_path / 'log').as_posix()}'" in text
    assert "logging_collector = on" in text and "postgresql-%a.log" in text
    for _ in range(2):
        pg.write_conf(data, text, "evowiki_test.conf")
    body = (data / "postgresql.conf").read_text(encoding="utf-8")
    assert body.count("include_if_exists = 'evowiki_test.conf'") == 1
    assert (data / "evowiki_test.conf").read_text(encoding="utf-8") == text


def test_the_template_name_follows_the_migrations(monkeypatch, tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (tmp_path / "migrate.py").write_text("x")
    (mig / "0001_a.sql").write_text("create table a ();")
    monkeypatch.setattr(pg, "MIGRATIONS_DIR", mig)
    one = pg.template_name()
    assert one.startswith("evowiki_tpl_") and one == pg.template_name()
    (mig / "0002_b.sql").write_text("create table b ();")
    assert pg.template_name() != one


def test_fresh_database_needs_a_template(monkeypatch):
    monkeypatch.setattr(pg, "_TEMPLATE", None)
    with pytest.raises(RuntimeError, match="build_template"):
        pg.fresh_database("postgresql://postgres@127.0.0.1:1/postgres")


def test_the_file_lock_excludes_another_process(tmp_path):
    lock = tmp_path / ".lock"
    child = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(pg.Path(pg.__file__).parents[1])!r})
        from tests.pg import file_lock
        try:
            with file_lock(__import__('pathlib').Path({str(lock)!r}), timeout=1):
                print("got")
        except RuntimeError:
            print("busy")
    """)
    with pg.file_lock(lock):
        out = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=60, check=False)
    assert out.stdout.strip() == "busy", out.stderr
    out = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=60, check=False)
    assert out.stdout.strip() == "got", out.stderr


def test_the_process_database_is_emptied_or_recreated(pg_admin_url):
    """Rows (also in append-only tables, behind foreign keys) and used sequences are reset in place;
    a schema change gets a new database from the template."""
    import psycopg
    db = pg.ProcessDatabase(pg_admin_url)
    try:
        url = db.reset()
        name = db.name
        with psycopg.connect(url, autocommit=True) as c:
            seq = c.execute("select format('%I.%I', schemaname, sequencename) from pg_sequences limit 1").fetchone()[0]
            first = c.execute("select nextval(%s)", (seq,)).fetchone()[0]
            c.execute("insert into app_user values ('u', 'b', 'S0', 'en', now())")
            c.execute("insert into user_session values ('s', 'u', now(), null)")
            c.execute("insert into wiki_event (user_id, seq, event_id, occurred_at, type, payload) "
                      "values ('u', 1, 'e', now(), 't', '{}')")
            c.execute("insert into schema_migrations (id) values ('9999_x')")
            c.execute("delete from schema_migrations where id = '9999_x'")
        assert db.reset() == url and db.name == name
        with psycopg.connect(url, autocommit=True) as c:
            assert c.execute("select (select count(*) from wiki_event) + (select count(*) from app_user)").fetchone()[0] == 0
            assert c.execute("select nextval(%s)", (seq,)).fetchone()[0] == first
            c.execute("create table t_extra (x int)")
        db.reset()
        assert db.name != name
        with psycopg.connect(db.url, autocommit=True) as c:
            assert c.execute("select to_regclass('t_extra')").fetchone()[0] is None
            assert c.execute("select count(*) from schema_migrations").fetchone()[0] > 0
    finally:
        db.close()
