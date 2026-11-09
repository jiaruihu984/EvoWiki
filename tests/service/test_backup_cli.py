"""Production restore behavior: complete content verification and latest-ledger replay."""

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import evowiki_backup as cli
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from evowiki.memory.tombstone import Ledger
from evowiki.service import backup
from tests.pg import bin_dir


def test_merge_keeps_latest_authoritative_states_and_rejects_corrupt_ledger(tmp_path):
    old, latest, merged = (tmp_path / n for n in ("old.jsonl", "latest.jsonl", "merged.jsonl"))
    Ledger(old).append("r1", "u", "s", None, "requested")
    latest.write_bytes(old.read_bytes())
    Ledger(latest).append("r1", "u", "s", None, "committed")
    Ledger(latest).append("r2", "u", "new", None, "requested")
    original = latest.read_bytes()
    cli.merge_ledger(old, latest, merged)
    assert Ledger(merged).requests()["r1"].state == "committed"
    assert "r2" in Ledger(merged).requests() and latest.read_bytes() == original
    latest.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        cli.merge_ledger(old, latest, merged)


def test_switch_changes_only_database_and_ledger_and_does_not_keep_secret_copy(tmp_path):
    from urllib.parse import urlsplit

    from dev_db import configured_port
    env_file = tmp_path / ".env"
    env = {"EVOWIKI_DATABASE_URL": "postgresql://evowiki_app:secret@127.0.0.1:1234/old",
           "EVOWIKI_ADMIN_DATABASE_URL": "postgresql://postgres:admin@127.0.0.1:1234/old",
           "UNRELATED": "中文 value"}
    env_file.write_text("\n".join(f"{k}='{v}'" for k, v in env.items()) + "\n", encoding="utf-8")
    cli.switch_config(env_file, env, "restored", tmp_path / "ledger.jsonl")
    got = cli.read_env_file(env_file)
    assert conninfo_to_dict(got["EVOWIKI_DATABASE_URL"])["dbname"] == "restored"
    assert conninfo_to_dict(got["EVOWIKI_DATABASE_URL"])["password"] == "secret"
    assert got["UNRELATED"] == env["UNRELATED"]
    assert urlsplit(got["EVOWIKI_DATABASE_URL"]).scheme == "postgresql"
    assert configured_port(env_file) == 1234
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_cli_requires_confirm_before_any_restore(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("EVOWIKI_DATABASE_URL=postgresql://u:secret@localhost/original\n", encoding="utf-8")
    monkeypatch.setattr(cli, "restore_backup", lambda *_a, **_kw: pytest.fail("must not restore"))
    code = cli.main(["--env", str(env_file), "restore", "--backup", "x.dump", "--database", "copy",
                     "--target-dir", str(tmp_path / "restore")])
    assert code == 1 and "--confirm" in capsys.readouterr().err


def test_failed_verification_keeps_original_configuration(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    original = b"EVOWIKI_DATABASE_URL=postgresql://u:secret@localhost/original\n"
    env_file.write_bytes(original)
    monkeypatch.setattr(cli, "require_stopped", lambda _env: None)

    def failed(*_args, **_kwargs):
        raise backup.BackupFailed("verification failed; password secret")

    monkeypatch.setattr(cli, "restore_backup", failed)
    code = cli.main(["--env", str(env_file), "restore", "--confirm", "--backup", "x.dump", "--database", "copy",
                     "--target-dir", str(tmp_path / "restore")])
    assert code == 1 and env_file.read_bytes() == original
    assert "secret" not in capsys.readouterr().err


def test_pg_restore_keeps_ownership_and_grants_and_never_puts_password_in_argv(tmp_path, monkeypatch):
    import subprocess
    seen = {}

    def fake_run(cmd, env, **_kwargs):
        seen.update(cmd=cmd, env=env)
        return subprocess.CompletedProcess(cmd, 1, "", "rejected secret")

    monkeypatch.setattr(backup, "pg_dump_path", lambda: "D:/pg/bin/pg_dump.exe")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    with pytest.raises(backup.BackupFailed) as exc:
        cli._pg_restore("postgresql://postgres:secret@localhost:1234/new", tmp_path / "x.dump")
    assert "secret" not in str(exc.value) and "secret" not in " ".join(seen["cmd"])
    assert "--no-owner" not in seen["cmd"] and "--no-privileges" not in seen["cmd"]
    assert "--exit-on-error" in seen["cmd"] and seen["env"]["PGPASSWORD"] == "secret"


@pytest.mark.parametrize("configured,path_tool,expected", [
    ("D:/configured/pg_dump.exe", None, "D:/configured/pg_dump.exe"),
    (None, "C:/external/pg_dump.exe", "C:/external/pg_dump.exe"),
    (None, None, "D:/bundled/pg_dump.exe"),
])
def test_cli_passes_the_script_owned_postgres_tool_to_service(tmp_path, monkeypatch, capsys, configured, path_tool, expected):
    import evowiki_ctl

    env_file = tmp_path / ".env"
    values = {"EVOWIKI_DATABASE_URL": "postgresql://app@127.0.0.1:1234/product"}
    if configured:
        values["EVOWIKI_PG_DUMP"] = configured
    original = "\n".join(f"{k}='{v}'" for k, v in values.items()) + "\n"
    env_file.write_text(original, encoding="utf-8")
    monkeypatch.delenv("EVOWIKI_PG_DUMP", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: path_tool)
    def bundled():
        assert configured is None and path_tool is None
        return Path("D:/bundled/pg_dump.exe")
    monkeypatch.setattr(evowiki_ctl, "pg_dump_path", bundled)
    seen = []
    monkeypatch.setattr(backup, "run_once", lambda cfg, *a, **kw: seen.append(cfg.pg_dump))
    monkeypatch.setattr(backup, "status", lambda cfg: {"ok": True})
    assert cli.main(["--env", str(env_file), "backup"]) == 0
    assert len(seen) == 1 and Path(seen[0]) == Path(expected)
    assert env_file.read_text(encoding="utf-8") == original
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_migration_backup_of_brand_new_database_has_empty_schema_inventory(db_url, tmp_path):
    database = "backup_empty_" + uuid.uuid4().hex[:12]
    admin = make_conninfo(db_url, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("create database {} template template0")
                     .format(psycopg.sql.Identifier(database)))
    try:
        cfg = backup.BackupConfig(make_conninfo(db_url, dbname=database), database, tmp_path / "backups",
                                  tmp_path / "control" / "backup.json",
                                  ledger=tmp_path / "forget-ledger.jsonl",
                                  pg_dump=str(bin_dir() / ("pg_dump.exe" if sys.platform == "win32" else "pg_dump")))
        made = backup.run_once(cfg, datetime.now(UTC), force=True)
        manifest = cli.inspect_backup(made)
        assert manifest["migrations"] == [] and manifest["inventory"] == {}
        assert "predates" in backup.status(cfg)["warning"]
        assert cfg.ledger.read_text(encoding="utf-8") == ""
        from datetime import timedelta

        from evowiki.store.migrate import migrate
        with psycopg.connect(cfg.url) as conn:
            migrate(conn)
        current = backup.run_once(cfg, datetime.now(UTC) + timedelta(hours=1))
        assert current is not None and current != made
        assert any(m.startswith("0082_") for m in cli.inspect_backup(current)["migrations"])
        assert backup.status(cfg)["warning"] is None
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("drop database {} with (force)")
                         .format(psycopg.sql.Identifier(database)))


def test_restore_old_dump_replays_latest_forget_ledger_and_preserves_clean_content(
        db_url, app_db_url, pool, tmp_path, monkeypatch):
    from evowiki.memory.forget import PLACEHOLDER
    from evowiki.service.deps import default_new_id
    from evowiki.sources.models import SourceRow
    from evowiki.sources.store import add_use, insert_source
    from evowiki.wikiapp.llm import require
    from evowiki.wikiapp.repo import PgRepo
    from tests.memory.forget_world import TAINTED, build
    from tests.runtime.helpers import seed_runtime
    from tests.wikiapp.flat_goal import flat_goal, flat_page
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_goal import BACKGROUND, GOAL, INTAKE_1, script_diag

    seed_runtime(pool)
    world = build(pool, tmp_path)
    world.ledger.path.write_text("", encoding="utf-8")
    script = Script()
    script.push("goals.intake", INTAKE_1)
    script_diag(script)
    repo, gateway, at = PgRepo(pool), require(script.gateway(), "u1"), datetime.now(UTC)
    draft = flat_goal(repo, gateway, "u1", GOAL, BACKGROUND, now=at, new_id=default_new_id)
    page_id = flat_page(repo, gateway, "u1", draft.draft.draft_id, now=at, new_id=default_new_id)
    expected_page = repo.load("u1").state.pages[page_id]
    # Reference with binary attachment data, preserved byte for byte by custom pg_dump.
    with pool.connection() as conn:
        for sid in ("keep-src", "secret-src"):
            at = datetime.now(UTC)
            insert_source(conn, SourceRow(sid, "u1", "pdf", "说明.pdf", "synthetic-" + sid,
                                          8, 1, "ready", None, (), None, {},
                                          "EvoWiki/attachments/说明.pdf", at, at, None),
                          b"pdf-test", "application/pdf")
            add_use(conn, "u1", sid, "ask", session_id="A", now=at)
    cfg = backup.BackupConfig(db_url, conninfo_to_dict(db_url)["dbname"], tmp_path / "backups",
                              tmp_path / "control" / "backup.json",
                              ledger=world.ledger.path,
                              pg_dump=str(bin_dir() / ("pg_dump.exe" if sys.platform == "win32" else "pg_dump")))
    made = backup.run_once(cfg, datetime.now(UTC), force=True)
    assert made is not None
    assert cli.inspect_backup(made)["inventory"]["assistant_entry"]["rows"] > 0
    out = world.service.forget_session("u1", "A", requested_by="restore-test", delete_source_ids=("secret-src",))
    assert out.state == "committed"
    current = world.ledger.path.read_bytes()
    database = "restore_test_" + uuid.uuid4().hex[:12]
    original_vault = tmp_path / "original-vault"
    (original_vault / ".obsidian").mkdir(parents=True)
    for name, content in (("personal.md", b"personal sentinel"),
                          ("EvoWiki/pages/derived.md", b"original derived page"),
                          ("EvoWiki/attachments/说明.pdf", b"original attachment")):
        file = original_vault / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    original_files = {p.relative_to(original_vault): p.read_bytes() for p in original_vault.rglob("*") if p.is_file()}
    env = {"EVOWIKI_DATABASE_URL": app_db_url, "EVOWIKI_ADMIN_DATABASE_URL": db_url,
           "EVOWIKI_FORGET_LEDGER": str(world.ledger.path), "EVOWIKI_EMBEDDER": "hash",
           "EVOWIKI_VAULT": str(original_vault), "EVOWIKI_PG_DUMP": cfg.pg_dump}
    target = tmp_path / "restored"
    from evowiki.service import deps
    build_services = deps.build_services

    def isolated_services(safe_env):
        assert safe_env["EVOWIKI_VAULT"] == str(target / "validation-vault")
        assert (target / "validation-vault" / ".obsidian").is_dir()
        return build_services(safe_env)
    monkeypatch.setattr(deps, "build_services", isolated_services)
    try:
        report = cli.restore_backup(env, made, database, target)
        assert report["ready"] and report["content_verified"] and report["forget_reconciled"]
        assert report["validation_vault"] == str(target / "validation-vault")
        assert {p.relative_to(original_vault): p.read_bytes() for p in original_vault.rglob("*") if p.is_file()} == original_files
        assert world.ledger.path.read_bytes() == current
        with psycopg.connect(make_conninfo(db_url, dbname=database)) as conn:
            assert conn.execute("select count(*) from assistant_session where session_id='A'").fetchone()[0] == 0
            for sid, mid in TAINTED:
                rows = conn.execute("select payload from assistant_entry where session_id=%s and type='assistant' "
                                    "and payload->>'message_id'=%s", (sid, mid + "-ans")).fetchall()
                assert rows and rows[0][0]["text"] == PLACEHOLDER
            assert conn.execute("select data from source_file where source_id='keep-src'").fetchone()[0] == b"pdf-test"
            assert conn.execute("select count(*) from source_file where source_id='secret-src'").fetchone()[0] == 0
            assert conn.execute("select count(*) from assistant_session where session_id='D'").fetchone()[0] == 1
        from evowiki.store.db import make_pool
        restored_pool = make_pool(make_conninfo(app_db_url, dbname=database), min_size=1, max_size=2)
        try:
            restored_state = PgRepo(restored_pool).load("u1").state
            assert restored_state.pages[page_id] == expected_page
            assert any(m.goal == GOAL for m in restored_state.maps.values())
        finally:
            restored_pool.close()
        assert json.loads((target / "restore-report.json").read_text(encoding="utf-8"))["content_verified"]
    finally:
        with psycopg.connect(make_conninfo(db_url, dbname="postgres"), autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("drop database if exists {} with (force)")
                         .format(psycopg.sql.Identifier(database)))
