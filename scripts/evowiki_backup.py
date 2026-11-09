"""Backup/list/restore-check/restore without model calls or test infrastructure.

Restore creates a new database and directory. The launcher stops the owned backend before
a confirmed restore switches .env. The latest external forget ledger is required separately.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from dotenv import set_key
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from envfile import read_env_file
from evowiki_args import backup_parser

from evowiki.service import backup


def inspect_backup(path: Path) -> dict:
    """A complete bundle is the dump and both checksummed sidecars."""
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise backup.BackupFailed("unsupported backup format")
    if (not isinstance(manifest.get("migrations"), list) or not isinstance(manifest.get("inventory"), dict)
            or not isinstance(manifest.get("postgres_version"), int) or not isinstance(manifest.get("owner"), str)
            or not isinstance(manifest.get("files"), dict)):
        raise backup.BackupFailed("backup manifest metadata is missing or invalid")
    required = {path.name, path.with_suffix(".ledger.jsonl").name}
    if set(manifest.get("files", {})) != required:
        raise backup.BackupFailed("backup is missing the database or forget ledger")
    for name, digest in manifest["files"].items():
        if Path(name).name != name or backup.file_hash(path.parent / name) != digest:
            raise backup.BackupFailed("backup file checksum does not match its manifest")
    return manifest


def merge_ledger(archived: Path, latest: Path, target: Path) -> None:
    from evowiki.memory.tombstone import Ledger
    if not latest.is_file():
        raise backup.BackupFailed("latest external forget ledger is missing; provide --ledger before restoring")
    seen, lines = set(), []
    for path in (archived, latest):
        Ledger(path).rows()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            canonical = json.dumps(json.loads(line), sort_keys=True, ensure_ascii=False)
            if canonical not in seen:
                seen.add(canonical)
                lines.append(canonical)
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _pg_restore(url: str, path: Path, *, pg_dump: str | None = None) -> None:
    c = conninfo_to_dict(url)
    pg = {"PGHOST": c.get("host", ""), "PGPORT": c.get("port", "5432"),
          "PGUSER": c.get("user", ""), "PGPASSWORD": c.get("password", ""), "PGDATABASE": c["dbname"]}
    tool = Path(pg_dump or backup.pg_dump_path()).with_name("pg_restore.exe" if sys.platform == "win32" else "pg_restore")
    r = subprocess.run([str(tool), "--exit-on-error", "--dbname", c["dbname"], str(path)],
                       env={**{k: v for k, v in os.environ.items() if not k.startswith("PG")}, **pg},
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", check=False)
    if r.returncode:
        raise backup.BackupFailed(backup.scrub(r.stderr.strip() or "pg_restore failed", url))


def validate_application(env: dict[str, str], admin: str, ledger: Path) -> dict:
    """Real app startup/read routes, with durable work deliberately not resumed."""
    from fastapi.testclient import TestClient

    from evowiki.service.app import create_app
    from evowiki.service.deps import build_services
    from evowiki.store.db import role_problem
    validation_vault = ledger.resolve().parent / "validation-vault"
    (validation_vault / ".obsidian").mkdir(parents=True)
    safe_env = {k: v for k, v in env.items() if not k.endswith("_API_KEY")}
    safe_env.update(EVOWIKI_DATABASE_URL=make_conninfo(env["EVOWIKI_DATABASE_URL"],
                                                    dbname=conninfo_to_dict(admin)["dbname"]),
                    EVOWIKI_ADMIN_DATABASE_URL=admin, EVOWIKI_FORGET_LEDGER=str(ledger),
                    EVOWIKI_VAULT=str(validation_vault),
                    EVOWIKI_BOT="off", EVOWIKI_BACKUP_DIR="off", EVOWIKI_OTEL_ENDPOINT="")
    with psycopg.connect(safe_env["EVOWIKI_DATABASE_URL"], connect_timeout=10) as conn:
        if problem := role_problem(conn):
            raise backup.BackupFailed(problem)
        users = [r[0] for r in conn.execute("select user_id from app_user order by user_id")]
    svc = build_services(safe_env)
    svc.durable = None
    try:
        app = create_app(svc)
        with TestClient(app) as client:
            ready = client.get("/health/ready")
            if ready.status_code != 200 or not app.state.memory_ready:
                raise backup.BackupFailed("restored application is not ready, including forget reconciliation")
            for user in users:
                response = client.get("/wiki/graph", headers={"X-EvoWiki-User": user})
                if response.status_code != 200:
                    raise backup.BackupFailed("restored Wiki could not be read by the application")
        return {"ready": True, "wiki_users_verified": len(users), "forget_reconciled": True,
                "validation_vault": str(validation_vault)}
    finally:
        svc.pool.close()


def restore_backup(env: dict[str, str], path: Path, database: str, target: Path,
                   *, latest_ledger: Path | None = None) -> dict:
    from evowiki.memory.tombstone import Ledger, ledger_path
    from evowiki.store.db import APP_ROLE
    from evowiki.store.migrate import MIGRATIONS_DIR, migrate
    manifest = inspect_backup(path)
    known = sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))
    applied = manifest["migrations"]
    if not applied or applied != known[:len(applied)] or not any(x.startswith("0082_") for x in applied):
        raise backup.BackupFailed("unsupported backup migrations: use this release or a supported older schema (0082+)")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database):
        raise backup.BackupFailed("new database name must use lowercase letters, numbers or underscores")
    admin = env.get("EVOWIKI_ADMIN_DATABASE_URL") or env["EVOWIKI_DATABASE_URL"]
    if database in {conninfo_to_dict(admin).get("dbname"), conninfo_to_dict(env["EVOWIKI_DATABASE_URL"]).get("dbname")}:
        raise backup.BackupFailed("restore requires a different, new database")
    target = target.resolve()
    if target.exists():
        raise backup.BackupFailed("restore target directory must not already exist")
    latest = latest_ledger or ledger_path(env)
    if not latest.is_file():
        raise backup.BackupFailed("latest external forget ledger is missing; provide --ledger")
    Ledger(latest).rows()
    target.mkdir(parents=True)
    merged = target / "forget-ledger.jsonl"
    merge_ledger(path.with_suffix(".ledger.jsonl"), latest, merged)
    restored = make_conninfo(admin, dbname=database)
    with psycopg.connect(make_conninfo(admin, dbname="postgres"), autocommit=True, connect_timeout=10) as conn:
        if conn.execute("select 1 from pg_database where datname=%s", (database,)).fetchone():
            raise backup.BackupFailed("restore database already exists; choose a new name")
        if not conn.execute("select rolsuper from pg_roles where rolname=current_user").fetchone()[0]:
            raise backup.BackupFailed("restore needs the cluster administrator to prepare roles and extension grants")
        if conn.info.server_version // 10000 < manifest["postgres_version"] // 10000:
            raise backup.BackupFailed("restoring to an older PostgreSQL major version is unsupported")
        for role in {APP_ROLE, "evowiki_forget_owner", manifest["owner"]}:
            if not conn.execute("select 1 from pg_roles where rolname=%s", (role,)).fetchone():
                login = psycopg.sql.SQL("LOGIN") if role == APP_ROLE else psycopg.sql.SQL("NOLOGIN")
                conn.execute(psycopg.sql.SQL("create role {} {}").format(psycopg.sql.Identifier(role), login))
                password = conninfo_to_dict(env["EVOWIKI_DATABASE_URL"]).get("password")
                if role == APP_ROLE and password:
                    conn.execute(psycopg.sql.SQL("alter role {} password {}").format(
                        psycopg.sql.Identifier(role), psycopg.sql.Literal(password)))
        if not conn.execute("select 1 from pg_available_extensions where name='vector'").fetchone():
            raise backup.BackupFailed("pgvector is not installed on the target PostgreSQL server")
        conn.execute(psycopg.sql.SQL("create database {} template template0").format(psycopg.sql.Identifier(database)))
    _pg_restore(restored, path, pg_dump=env.get("EVOWIKI_PG_DUMP"))
    with psycopg.connect(restored) as conn:
        if backup.inventory(conn) != manifest["inventory"]:
            raise backup.BackupFailed("restored table content differs from the backup snapshot")
        actual = [r[0] for r in conn.execute("select id from schema_migrations order by id")]
        if actual != applied:
            raise backup.BackupFailed("restored migration history differs from the manifest")
        conn.execute(psycopg.sql.SQL("grant connect, temporary on database {} to {}").format(
            psycopg.sql.Identifier(database), psycopg.sql.Identifier(APP_ROLE)))
        conn.commit()
        migrate(conn)
    report = {"database": database, "directory": str(target), "dump_sha256": backup.file_hash(path),
              "content_verified": True, "migrations_verified": True,
              **validate_application(env, restored, merged)}
    backup.atomic_json(target / "restore-report.json", report)
    return report


def switch_config(env_file: Path, env: dict[str, str], database: str, ledger: Path) -> None:
    original = env_file.read_bytes()
    part = env_file.with_name(env_file.name + ".restore-part")
    part.write_bytes(original)
    try:
        for key in ("EVOWIKI_DATABASE_URL", "EVOWIKI_ADMIN_DATABASE_URL"):
            url = env.get(key) or env["EVOWIKI_DATABASE_URL"]
            parsed = urlsplit(url)
            if parsed.scheme not in {"postgresql", "postgres"}:
                raise backup.BackupFailed("restore configuration requires a postgresql:// database URL")
            set_key(str(part), key, urlunsplit(parsed._replace(path="/" + quote(database, safe=""))))
        set_key(str(part), "EVOWIKI_FORGET_LEDGER", str(ledger))
        part.replace(env_file)
    finally:
        part.unlink(missing_ok=True)


def require_stopped(env: dict[str, str]) -> None:
    service = conninfo_to_dict(env["EVOWIKI_DATABASE_URL"])
    admin = env.get("EVOWIKI_ADMIN_DATABASE_URL") or env["EVOWIKI_DATABASE_URL"]
    with psycopg.connect(make_conninfo(admin, dbname="postgres"), connect_timeout=10) as conn:
        busy = conn.execute("select count(*) from pg_stat_activity where datname=%s and usename=%s",
                            (service["dbname"], service.get("user", "evowiki_app"))).fetchone()[0]
    if busy:
        raise backup.BackupFailed("stop this instance with evowiki down before restoring; application connections remain")


def main(argv: list[str] | None = None) -> int:
    parser = backup_parser(ROOT)
    args = parser.parse_args(argv)
    env = {}
    try:
        env = read_env_file(args.env)
        if env.get("EVOWIKI_FORGET_LEDGER") and not Path(env["EVOWIKI_FORGET_LEDGER"]).is_absolute():
            env["EVOWIKI_FORGET_LEDGER"] = str(args.env.resolve().parent / env["EVOWIKI_FORGET_LEDGER"])
        if args.command != "list" and (args.command != "restore" or args.confirm) and not env.get("EVOWIKI_PG_DUMP"):
            tool = os.environ.get("EVOWIKI_PG_DUMP") or shutil.which("pg_dump")
            if tool is None:
                from evowiki_ctl import pg_dump_path
                tool = str(pg_dump_path())
            env["EVOWIKI_PG_DUMP"] = tool
        if args.command in {"backup", "list"}:
            cfg = backup.BackupConfig.from_env(env, args.env)
            if args.command == "backup":
                if cfg is None:
                    raise backup.BackupFailed("backups are disabled; set EVOWIKI_BACKUP_DIR in .env")
                backup.run_once(cfg, datetime.now(UTC), force=True)
            report = backup.status(cfg)
        else:
            if args.command == "restore" and not args.confirm:
                raise backup.BackupFailed("restore requires --confirm to switch configuration after successful verification")
            if args.command == "restore":
                require_stopped(env)
            report = restore_backup(env, args.backup.resolve(), args.database, args.target_dir,
                                    latest_ledger=args.ledger)
            report["switched"] = False
            if args.command == "restore":
                report["previous_database"] = conninfo_to_dict(env["EVOWIKI_DATABASE_URL"])["dbname"]
                switch_config(args.env, env, args.database, args.target_dir.resolve() / "forget-ledger.jsonl")
                report["switched"] = True
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, psycopg.Error) as exc:
        message = str(exc)
        for key in ("EVOWIKI_DATABASE_URL", "EVOWIKI_ADMIN_DATABASE_URL"):
            if env.get(key):
                message = backup.scrub(message, env[key])
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
