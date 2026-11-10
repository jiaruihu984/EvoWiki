"""Daily custom-format PostgreSQL backup plus external forget ledger and snapshot manifest.

Completed files remain authoritative; instance-local control state records destination failures
without making the service unavailable. An OS lock fences automatic/manual backups. Production restore is
implemented in scripts/evowiki_backup.py and always uses a new database.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from evowiki.file_lock import file_lock

log = logging.getLogger(__name__)

DEFAULT_DIR = ".evowiki-run/backups"
KEEP = 14
EVERY_S = 24 * 3600
FRESH_S = 20 * 3600
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class BackupConfig:
    url: str                        # a role that may read every table (EVOWIKI_ADMIN_DATABASE_URL, else the service's)
    dbname: str
    folder: Path
    state_file: Path                # instance-local control state, independent of the backup destination
    keep: int = KEEP
    every_s: float = EVERY_S
    ledger: Path | None = None
    snapshot: str | None = None
    pg_dump: str | None = None

    @staticmethod
    def from_env(env: Mapping[str, str], env_file: Path | None = None) -> "BackupConfig | None":
        """None: off (EVOWIKI_BACKUP_DIR=off), no database url, or not the real database (`real_database`)."""
        root = (env_file or repo_env_file() or Path(__file__).resolve().parents[3] / ".env").resolve().parent
        where = env.get("EVOWIKI_BACKUP_DIR", str(root / DEFAULT_DIR))
        service_url = env.get("EVOWIKI_DATABASE_URL", "")
        admin = env.get("EVOWIKI_ADMIN_DATABASE_URL")
        url = admin if admin and _where(admin) == _where(service_url) else service_url     # the same database, the owner's role
        if where.lower() == "off" or not url or not real_database(service_url, env_file):
            return None
        from evowiki.memory.tombstone import ledger_path
        ledger = ledger_path(env)
        if not ledger.is_absolute():
            ledger = root / ledger
        folder = Path(where)
        if not folder.is_absolute():
            folder = root / folder
        identity = hashlib.sha256(json.dumps(_where(url)).encode()).hexdigest()[:16]
        keep = int(env.get("EVOWIKI_BACKUP_KEEP", str(KEEP)))
        if not 1 <= keep <= 365:
            raise ValueError("EVOWIKI_BACKUP_KEEP must be between 1 and 365")
        state_file = root / ".evowiki-run" / "backup-state" / f"{identity}.json"
        return BackupConfig(url, conninfo_to_dict(url).get("dbname") or "", folder, state_file,
                            keep=keep, ledger=ledger, pg_dump=env.get("EVOWIKI_PG_DUMP"))


def _where(url: str) -> tuple[str, str, str]:
    d = conninfo_to_dict(url)
    host = d.get("host") or ""
    return "local" if host in LOCAL_HOSTS else host, str(d.get("port") or "5432"), d.get("dbname") or ""


def repo_env_file() -> Path | None:
    """The `.env` of the repository this code runs from (a worktree's is its main checkout's)."""
    for d in Path(__file__).resolve().parents:
        if (d / ".env").is_file():
            return d / ".env"
    return None


def real_database(url: str, env_file: Path | None = None) -> bool:
    """Whether `url` is the database the repository's `.env` names in EVOWIKI_DATABASE_URL (same kind of host, port and
    name). The one rule for which database is worth a backup."""
    env_file = env_file or repo_env_file()
    if not url or env_file is None:
        return False
    real = dotenv_values(env_file, interpolate=False).get("EVOWIKI_DATABASE_URL")
    return bool(real) and _where(real) == _where(url) and _where(url)[2] != ""


def scrub(text: str, url: str) -> str:
    """`text` without the password of `url` (a message is logged and shown)."""
    options = conninfo_to_dict(url)
    for key in ("password", "sslpassword", "oauth_client_secret", "scram_client_key", "scram_server_key"):
        if value := options.get(key):
            text = text.replace(value, "***")
    return text


def pg_dump_path(env: Mapping[str, str] | None = None, env_file: Path | None = None) -> str:
    """pg_dump, found without importing anything from the test-only Postgres package: EVOWIKI_PG_DUMP (a path), else the one
    on PATH, else the stable copy of the Postgres binaries next to the repository's `.env` (<main checkout>/.pg18/bin, where
    scripts/install_pg_service.ps1 puts them). BackupFailed when there is none."""
    env = os.environ if env is None else env
    exe = "pg_dump.exe" if sys.platform == "win32" else "pg_dump"
    if env.get("EVOWIKI_PG_DUMP"):
        return env["EVOWIKI_PG_DUMP"]
    if found := shutil.which("pg_dump"):
        return found
    env_file = env_file or repo_env_file()
    stable = None if env_file is None else env_file.parent / ".pg18" / "bin" / exe
    if stable is not None and stable.is_file():
        return str(stable)
    raise BackupFailed("pg_dump not found: put it on PATH, or set EVOWIKI_PG_DUMP to its full path")


class BackupFailed(RuntimeError):
    pass


class BackupBusy(BackupFailed):
    """Another process owns the backup lock; no backup attempt has failed."""


def _mine(cfg: BackupConfig) -> list[Path]:
    found = []
    for p in cfg.folder.glob(f"{cfg.dbname}-*.dump"):
        try:
            _taken(p, datetime.now(UTC))
            with p.open("rb") as f:
                complete = f.read(5) == b"PGDMP"
            if complete:
                found.append(p)
        except (ValueError, OSError):
            continue
    return sorted(found)


def atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".part")
    part = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def _save_status(cfg: BackupConfig, state: dict) -> None:
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(cfg.state_file, {k: state[k] for k in (
        "last_success_at", "last_success_file", "last_failure_at", "last_failure_reason")})


def _record_failure(cfg: BackupConfig, now: datetime, exc: Exception) -> None:
    state = status(cfg)
    reason = scrub(str(exc), cfg.url)
    state.update(last_failure_at=now.isoformat(), last_failure_reason=reason)
    try:
        _save_status(cfg, state)
    except OSError as save_error:
        raise BackupFailed(f"{reason}; cannot save backup status: {scrub(str(save_error), cfg.url)}") from save_error


@contextmanager
def _backup_lock(cfg: BackupConfig):
    with file_lock(cfg.folder / f"{cfg.dbname}.lock", busy_error=BackupBusy):
        yield


def status(cfg: BackupConfig | None) -> dict:
    out = {"disabled": cfg is None, "running": False, "last_success_at": None,
           "last_success_file": None, "last_failure_at": None, "last_failure_reason": None,
           "warning": None, "files": []}
    if cfg is None:
        out["warning"] = "backups are disabled for this database"
        return out
    state_error = None
    try:
        saved = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        for kind, detail in (("success", "file"), ("failure", "reason")):
            at, text = f"last_{kind}_at", f"last_{kind}_{detail}"
            if isinstance(saved.get(at), str) and isinstance(saved.get(text), str):
                datetime.fromisoformat(saved[at])
                out.update({at: saved[at], text: saved[text]})
    except FileNotFoundError as exc:
        if cfg.state_file.parent.exists() and not cfg.state_file.parent.is_dir():
            state_error = f"cannot read backup status: {scrub(str(exc), cfg.url)}"
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        state_error = f"cannot read backup status: {scrub(str(exc), cfg.url)}"
    target_error = None
    try:
        if not cfg.folder.is_dir():
            target_error = f"backup destination is not a directory: {cfg.folder}"
        files = _mine(cfg) if target_error is None else []
    except OSError as exc:
        files = []
        target_error = scrub(str(exc), cfg.url)
    out["files"] = [str(p) for p in files]
    if files:
        latest = _taken(files[-1], datetime.now(UTC))
        if out["last_success_at"] is None or latest >= datetime.fromisoformat(out["last_success_at"]):
            out.update(last_success_at=latest.isoformat(), last_success_file=str(files[-1]))
        if cfg.ledger is not None and not _bundle_present(files[-1]):
            out["warning"] = "latest dump is missing its forget ledger or manifest; run backup again"
        elif cfg.ledger is not None and not _current_schema_bundle(files[-1]):
            out["warning"] = "latest backup predates the current schema; run backup again"
    try:
        with file_lock(cfg.state_file.with_suffix(".lock"), busy_error=BackupBusy):
            pass
    except BackupBusy:
        out["running"] = True
    except OSError as exc:
        state_error = f"cannot access backup status lock: {scrub(str(exc), cfg.url)}"
    if cfg.folder.exists():
        try:
            with _backup_lock(cfg):
                pass
        except BackupBusy:
            out["running"] = True
        except OSError as exc:
            target_error = scrub(str(exc), cfg.url)
    if out["last_failure_at"]:
        out["warning"] = out["last_failure_reason"]
    if state_error or target_error:
        out["warning"] = state_error or target_error
    elif not files and out["warning"] is None:
        out["warning"] = "no completed backup exists"
    return out


def file_hash(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _bundle_present(path: Path) -> bool:
    return path.with_suffix(".ledger.jsonl").is_file() and path.with_suffix(".manifest.json").is_file()


def _current_schema_bundle(path: Path) -> bool:
    from evowiki.store.migrate import MIGRATIONS_DIR
    if not _bundle_present(path):
        return False
    try:
        manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        applied = manifest.get("migrations")
        return bool(applied) and applied == sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))
    except (OSError, ValueError, AttributeError):
        return False


def inventory(conn) -> dict:
    """Content fingerprints include Wiki events, conversations and attachment references."""
    conn.execute("set local timezone to 'UTC'")
    conn.execute("set local datestyle to 'ISO, YMD'")
    conn.execute("set local intervalstyle to 'postgres'")
    conn.execute("set local bytea_output to 'hex'")
    conn.execute("set local extra_float_digits to 3")
    tables = [r[0] for r in conn.execute(
        "select tablename from pg_tables where schemaname='public' order by 1")]
    out = {}
    for table in tables:
        h, count = hashlib.sha256(), 0
        with conn.cursor(name="backup_inventory") as cursor:
            cursor.execute(psycopg.sql.SQL('select row_to_json(t)::text collate "C" from public.{} t order by 1')
                           .format(psycopg.sql.Identifier(table)))
            for (row,) in cursor:
                h.update(row.encode("utf-8") + b"\n")
                count += 1
        out[table] = {"rows": count, "sha256": h.hexdigest()}
    return out


def backup_before_migration(env: Mapping[str, str], env_file: Path, folder: Path) -> Path:
    cfg = BackupConfig.from_env({**env, "EVOWIKI_BACKUP_DIR": str(folder)}, env_file)
    if cfg is None:
        raise BackupFailed("migration backup requires the database configured in .env")
    made = run_once(cfg, datetime.now(UTC), force=True)
    assert made is not None
    return made


def _taken(p: Path, like: datetime) -> datetime:
    """When a dump was taken: the time in its name, read in `like`'s time zone."""
    stamp = p.stem.rsplit("-", 2)[-2:]
    return datetime.strptime("-".join(stamp), "%Y%m%d-%H%M%S").replace(tzinfo=like.tzinfo)


def run_once(cfg: BackupConfig, now: datetime,
             dump: Callable[[BackupConfig, Path], None] | None = None, *, force: bool = False) -> Path | None:
    """One round: a dump unless the latest is younger than FRESH_S, then the oldest beyond `cfg.keep` removed.
    Returns the new file (None: not needed). Raises BackupFailed."""
    now = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
    try:
        # Serialize the entire attempt and its result, including destination setup failures.
        # Keep the destination lock too: another instance can share that destination.
        with file_lock(cfg.state_file.with_suffix(".lock"), busy_error=BackupBusy):
            try:
                with _backup_lock(cfg):
                    return _run_once(cfg, now, dump, force=force)
            except BackupBusy:
                raise
            except Exception as exc:
                _record_failure(cfg, now, exc)
                raise BackupFailed(scrub(str(exc), cfg.url)) from exc
    except BackupBusy:
        raise
    except OSError as exc:
        raise BackupFailed(f"cannot lock backup status: {scrub(str(exc), cfg.url)}") from exc


def _run_once(cfg, now, dump, *, force):
    have = _mine(cfg)
    if (not force and have and (cfg.ledger is None or _current_schema_bundle(have[-1]))
            and (now - _taken(have[-1], now)).total_seconds() < FRESH_S):
        _save_status(cfg, status(cfg))
        return None
    target = cfg.folder / f"{cfg.dbname}-{now:%Y%m%d-%H%M%S}.dump"
    part = target.with_suffix(".part")
    if target.exists():
        raise BackupFailed("a backup with this timestamp already exists; retry in one second")
    state = status(cfg)
    sidecars = []
    try:
        manifest = {"format": 1, "created_at": now.isoformat(), "database": cfg.dbname,
                    "application_version": version("evowiki"), "commit": os.environ.get("EVOWIKI_COMMIT", ""),
                    "files": {}, "migrations": [], "inventory": {}}
        if dump is None:
            with psycopg.connect(cfg.url, connect_timeout=10) as conn:
                conn.execute("set transaction isolation level repeatable read read only")
                snapshot = conn.execute("select pg_export_snapshot()").fetchone()[0]
                _pg_dump(replace(cfg, snapshot=snapshot), part)
                manifest["inventory"] = inventory(conn)
                if conn.execute("select to_regclass('public.schema_migrations')").fetchone()[0] is not None:
                    manifest["migrations"] = [r[0] for r in conn.execute("select id from schema_migrations order by id")]
                manifest["owner"] = conn.info.user
                manifest["postgres_version"] = conn.info.server_version
                if cfg.ledger is not None and not cfg.ledger.exists():
                    if manifest["inventory"].get("forget_request", {}).get("rows", 0):
                        raise BackupFailed("forget ledger is missing despite existing forget requests")
                    cfg.ledger.parent.mkdir(parents=True, exist_ok=True)
                    with cfg.ledger.open("x", encoding="utf-8"):
                        pass
        else:
            dump(cfg, part)
        with part.open("rb") as f:
            if f.read(5) != b"PGDMP":
                raise BackupFailed("pg_dump did not produce a custom-format backup")
        if cfg.ledger is not None:
            from evowiki.memory.tombstone import Ledger
            if not cfg.ledger.exists():
                raise BackupFailed("forget ledger is missing; cannot guarantee forgotten content stays deleted")
            ledger = target.with_suffix(".ledger.jsonl")
            # Parse a stable copy: a concurrent append cannot leave a half JSON line unnoticed.
            ledger_part = ledger.with_suffix(".part")
            shutil.copyfile(cfg.ledger, ledger_part)
            Ledger(ledger_part).rows()
            ledger_part.replace(ledger)
            sidecars.append(ledger)
            manifest["files"][ledger.name] = file_hash(ledger)
        manifest["files"][target.name] = file_hash(part)
        manifest_path = target.with_suffix(".manifest.json")
        atomic_json(manifest_path, manifest)
        sidecars.append(manifest_path)
        part.replace(target)
        for old in _mine(cfg)[:-cfg.keep]:
            old.unlink(missing_ok=True)
            old.with_suffix(".ledger.jsonl").unlink(missing_ok=True)
            old.with_suffix(".manifest.json").unlink(missing_ok=True)
        state.update(last_success_at=now.isoformat(), last_success_file=str(target),
                     last_failure_at=None, last_failure_reason=None)
        _save_status(cfg, state)
    except Exception as exc:
        part.unlink(missing_ok=True)
        target.with_suffix(".ledger.part").unlink(missing_ok=True)
        if not target.exists():
            for p in sidecars:
                p.unlink(missing_ok=True)
        raise BackupFailed(scrub(str(exc), cfg.url)) from exc
    return target


@contextmanager
def pg_tool_connection(url: str):
    """Keep every libpq option in a private service file, never in process arguments."""
    options = conninfo_to_dict(url)
    if "service" in options:
        # libpq owns service lookup and precedence (user/system files, environment, LDAP).
        # Resolve through the same live connection the operation already requires.
        with psycopg.connect(url) as conn:
            options = {option.keyword.decode(): option.val.decode()
                       for option in conn.pgconn.info if option.val is not None and option.keyword != b"service"}
    if any("\n" in v or "\r" in v or v != v.strip() for v in options.values()):
        raise BackupFailed("connection parameters cannot contain newlines or surrounding whitespace")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    with tempfile.TemporaryDirectory(prefix="evowiki-pg-") as folder:
        path = Path(folder) / "pg_service.conf"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("[evowiki_backup]\n")
            for key, value in options.items():
                stream.write(f"{key}={value}\n")
        env.update(PGSERVICEFILE=str(path), PGSERVICE="evowiki_backup")
        yield env, make_conninfo(**options)


def _pg_dump(cfg: BackupConfig, out: Path) -> None:
    """pg_dump -Fc, preserving libpq settings without secrets in argv."""
    connection = cfg.url
    try:
        with pg_tool_connection(cfg.url) as (env, connection):
            r = subprocess.run([cfg.pg_dump or pg_dump_path(), "-Fc", *(["--snapshot", cfg.snapshot] if cfg.snapshot else []),
                                "-f", str(out)], env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except OSError as e:
        raise BackupFailed(scrub(str(e), connection)) from e
    if r.returncode != 0:
        raise BackupFailed(scrub(r.stderr.strip() or f"pg_dump exited with {r.returncode}", connection))


def start_background(cfg: BackupConfig, clock: Callable[[], datetime]) -> tuple[threading.Thread, threading.Event]:
    """The first round now, then one every `cfg.every_s`; set the event to stop."""
    stop = threading.Event()

    def loop() -> None:
        while True:
            try:
                made = run_once(cfg, clock())
                if made is not None:
                    log.info("database backup written: %s (%.1f MB)", made, made.stat().st_size / 1e6)
            except BackupBusy:
                log.info("database backup is already running")
            except (BackupFailed, OSError) as e:
                log.error("database backup FAILED, the service goes on without it: %s", e)
            if stop.wait(cfg.every_s):
                return

    t = threading.Thread(target=loop, name="db-backup", daemon=True)
    t.start()
    return t, stop
