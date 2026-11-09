"""Postgres for tests: EVOWIKI_TEST_DATABASE_URL if set, else the one long-lived test cluster.

The test cluster (reuse audit B8) replaces the per-run temp clusters (%TEMP%\\evowiki-pg-*, which
leaked whenever pytest was killed; scripts/clean_test_pg.py removes the leftovers). It lives in
EVOWIKI_TEST_PG_DIR (default %TEMP%\\evowiki-test-pg: data dir `data`, server logs in `log`
through logging_collector, pg_ctl's own startup log `data.log`) and listens on 127.0.0.1 at
EVOWIKI_TEST_PG_PORT (default 15433: above this machine's dynamic port range 1024-15000, which
WinNAT reserves blocks of, and never the dev port). The first run that finds it missing creates it
(initdb); the first that finds it stopped starts it detached (WMI on Windows, like the dev DB), so it
outlives pytest and every worktree's runs share it. A file lock (`<dir>/.lock`) serialises
creating and starting it; it is left running. Stop it with `pg_ctl -D <dir>/data stop -m fast`.

Templates: one per migration set, `evowiki_tpl_<sha of migrate.py + migrations/*.sql>`, built once
under an advisory lock (into `<name>_build`, renamed when complete, then is_template and no
connections), so runs from worktrees with different migrations never share a template and never
rebuild one another's. Templates unused for TEMPLATE_TTL_DAYS are dropped.

Test databases are `evowiki_t_<pid>_<hex>`, created from the template: the `db_url` fixture's is
one per pytest process (`ProcessDatabase`, emptied before every test), scripts and e2e backends
make their own (`fresh_database`). Those left by a killed run (that pid gone, no connections) are
dropped at the next session."""

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from evowiki.store.migrate import MIGRATIONS_DIR, migrate

DEV_PORT = 15432                    # = scripts/dev_db.py DEFAULT_PORT (the shared dev server's default port)
TEST_PORT = 15433
TEMPLATE_PREFIX = "evowiki_tpl_"
TEST_DB_PREFIX = "evowiki_t_"
TEMPLATE_TTL_DAYS = 14
LOCK_TIMEOUT = 300
START_TIMEOUT = 180
_ADVISORY_KEY = 0x45765447          # 'EvTG'
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_TEMPLATE: str | None = None        # set by build_template, the default of fresh_database


def check_not_dev_server(test_url: str, dev_url: str | None) -> None:
    """Tests create and drop databases: never let them point at the shared dev server."""
    t = conninfo_to_dict(test_url)
    port = str(t.get("port") or 5432)
    same = False
    if dev_url:
        d = conninfo_to_dict(dev_url)
        same = (t.get("host"), port) == (d.get("host"), str(d.get("port") or 5432))
    if same or port == str(DEV_PORT):
        raise RuntimeError(f"EVOWIKI_TEST_DATABASE_URL points at the shared dev server ({t.get('host')}:{port}); "
                           "tests must use a disposable Postgres")


def _dev_db():
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import dev_db
    return dev_db


def cluster_dir() -> Path:
    if os.environ.get("EVOWIKI_TEST_PG_DIR"):
        return Path(os.environ["EVOWIKI_TEST_PG_DIR"])
    # %TEMP%, not %LOCALAPPDATA%: initdb fails directly under %LOCALAPPDATA% on this machine (renaming the
    # first WAL segment gives "Improper link"), while %TEMP% works as the old per-run clusters did.
    if os.name == "nt":
        return Path(tempfile.gettempdir()) / "evowiki-test-pg"
    return Path.home() / ".cache" / "evowiki-test-pg"


def cluster_port() -> int:
    return int(os.environ.get("EVOWIKI_TEST_PG_PORT") or TEST_PORT)


def cluster_url(port: int) -> str:
    return f"postgresql://postgres@127.0.0.1:{port}/postgres"


def start_admin_url() -> tuple[str, object | None]:
    """Return (admin DSN, handle keeping the server alive; None: the server outlives the run)."""
    dev_url = os.environ.get("EVOWIKI_DATABASE_URL")
    url = os.environ.get("EVOWIKI_TEST_DATABASE_URL")
    if url:
        check_not_dev_server(url, dev_url)
        return url, None
    port = cluster_port()
    check_not_dev_server(cluster_url(port), dev_url)
    _check_not_dev_port(port)
    return ensure_cluster(cluster_dir(), port), None


def _check_not_dev_port(port: int) -> None:
    """Also refuse the port of the main checkout's .env (the dev server, whatever port it is on)."""
    dev_db = _dev_db()
    try:
        dev_port = dev_db.configured_port(dev_db.main_checkout() / ".env")
    except (ValueError, OSError, subprocess.CalledProcessError):
        return
    if port == dev_port:
        raise RuntimeError(f"the test cluster's port {port} is the dev server's (.env); set EVOWIKI_TEST_PG_PORT")


# ---------- the long-lived cluster ----------

@contextlib.contextmanager
def file_lock(path: Path, timeout: float = LOCK_TIMEOUT):
    """An exclusive lock on `path` across processes; released when the holder exits, also when killed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")  # noqa: SIM115 - held for the with-block
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"test cluster lock {path} still held after {timeout}s") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def bin_dir() -> Path:
    """The binaries the long-lived cluster runs: the main checkout's stable copy (.pg18, from
    scripts/install_pg_service.ps1) when present, else the main checkout's .venv, else this venv's."""
    return _dev_db().preferred_bin_dir()


def cluster_conf(root: Path, port: int) -> str:
    log_dir = (root / "log").as_posix()
    return ("# managed by tests/pg.py (the long-lived test cluster); rewritten at every start\n"
            "listen_addresses = '127.0.0.1'\n"
            f"port = {port}\n"
            "max_connections = 300\n"
            "synchronous_commit = off\n"
            "logging_collector = on\n"
            f"log_directory = '{log_dir}'\n"
            "log_filename = 'postgresql-%a.log'\n"
            "log_rotation_age = 1d\n"
            "log_rotation_size = 0\n"
            "log_truncate_on_rotation = on\n")


def write_conf(data: Path, text: str, name: str) -> None:
    """`name` next to postgresql.conf, included from it once (idempotent)."""
    (data / name).write_text(text, encoding="utf-8")
    conf = data / "postgresql.conf"
    line = f"include_if_exists = '{name}'"
    body = conf.read_text(encoding="utf-8")
    if line not in body:
        conf.write_text(body.rstrip("\n") + f"\n\n# EvoWiki\n{line}\n", encoding="utf-8")


def _postmaster(data: Path) -> tuple[int, int, str] | None:
    """(pid, port, status) of a live postmaster on `data`, else None."""
    try:
        lines = (data / "postmaster.pid").read_text().splitlines()
        pid, port, status = int(lines[0]), int(lines[3]), lines[7].strip()
    except (OSError, ValueError, IndexError):
        return None
    try:
        if not psutil.Process(pid).name().lower().startswith("postgres"):
            return None
    except psutil.Error:
        return None
    return pid, port, status


def ensure_cluster(root: Path, port: int, timeout: int = START_TIMEOUT) -> str:
    """Create and/or start the long-lived cluster under `root` on `port` (under the file lock) and
    return its admin URL. Fails closed: a cluster running on another port is refused."""
    data = root / "data"
    with file_lock(root / ".lock"):
        pm = _postmaster(data)
        if pm is not None and pm[2] == "ready":
            if pm[1] != port:
                raise RuntimeError(f"the test cluster {data} runs on port {pm[1]}, not {port}; "
                                   f"stop it (pg_ctl -D {data} stop -m fast) or set EVOWIKI_TEST_PG_PORT={pm[1]}")
            return _check_connects(cluster_url(port))
        bins = bin_dir()
        exe = ".exe" if os.name == "nt" else ""
        if not (data / "PG_VERSION").exists():
            if data.exists() and any(data.iterdir()):
                raise RuntimeError(f"{data} exists but is not a Postgres data dir; remove it and run again")
            path = os.pathsep.join([*_dev_db().dll_dirs(), os.environ.get("PATH", "")])
            r = subprocess.run([str(bins / f"initdb{exe}"), "--auth=trust", "--auth-local=trust", "--encoding=utf8",
                                "-U", "postgres", "-D", str(data)], capture_output=True, text=True, errors="replace",
                               check=False, env={**os.environ, "PATH": path})
            if r.returncode != 0:
                raise RuntimeError(f"initdb of the test cluster failed:\n{(r.stderr or r.stdout)[-2000:]}")
        (root / "log").mkdir(exist_ok=True)
        write_conf(data, cluster_conf(root, port), "evowiki_test.conf")
        if pm is None:                              # not starting already (crash recovery) → start it
            _start(data, bins / f"pg_ctl{exe}", port, timeout)
        if not _dev_db().wait_ready(data, timeout, port):
            raise RuntimeError(f"the test cluster {data} is not ready after {timeout}s; see {root / 'log'} "
                               f"and {data}.log")
        return _check_connects(cluster_url(port))


def _start(data: Path, pg_ctl: Path, port: int, timeout: int) -> None:
    dev_db = _dev_db()
    dev_db.rotate_log(dev_db.log_path(data))
    if os.name == "nt":                         # outside pytest's process tree / job object
        rv, _pid = dev_db.start_detached(
            dev_db.pg_ctl_start_cmdline(pg_ctl, data, port, timeout, dev_db.dll_dirs()), data.parent)
        if rv != 0:
            raise RuntimeError(f"could not start the test cluster via WMI (ReturnValue {rv})")
        return
    subprocess.Popen([str(pg_ctl), "start", "-w", "-t", str(timeout), "-D", str(data), "-l",
                      str(dev_db.log_path(data)), "-o", f"-h 127.0.0.1 -p {port}"],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _check_connects(url: str, tries: int = 20) -> str:
    for i in range(tries):
        try:
            with psycopg.connect(url, connect_timeout=5) as conn:
                conn.execute("select 1")
            return url
        except psycopg.OperationalError:
            if i == tries - 1:
                raise
            time.sleep(0.5)
    return url


# ---------- templates and per-test databases ----------

def migrations_digest() -> str:
    h = hashlib.sha256((Path(MIGRATIONS_DIR).parent / "migrate.py").read_bytes())
    for p in sorted(Path(MIGRATIONS_DIR).glob("*.sql")):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def template_name() -> str:
    return TEMPLATE_PREFIX + migrations_digest()


def _retry(fn, tries: int = 20):
    """For DDL that fails while a just-closed connection's backend is still exiting."""
    for i in range(tries):
        try:
            return fn()
        except psycopg.errors.ObjectInUse:
            if i == tries - 1:
                raise
            time.sleep(0.25)
    return None


def build_template(admin_url: str) -> str:
    """The template for this checkout's migrations (built if missing); also the default template of
    `fresh_database` from now on. Drops expired templates and databases of dead runs."""
    global _TEMPLATE
    name = template_name()
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("select pg_advisory_lock(%s)", (_ADVISORY_KEY,))
        try:
            conn.execute("create table if not exists evowiki_test_templates "
                         "(name text primary key, last_used timestamptz not null default now())")
            if conn.execute("select 1 from pg_database where datname = %s", (name,)).fetchone() is None:
                build = name + "_build"
                conn.execute(f"drop database if exists {build} with (force)")
                conn.execute(f"create database {build}")
                with psycopg.connect(make_conninfo(admin_url, dbname=build)) as c:
                    migrate(c)
                _retry(lambda: conn.execute(f"alter database {build} rename to {name}"))
                conn.execute(f"alter database {name} with is_template true allow_connections false")
            conn.execute("insert into evowiki_test_templates (name) values (%s) "
                         "on conflict (name) do update set last_used = now()", (name,))
            _prune(conn, keep=name)
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (_ADVISORY_KEY,))
    _TEMPLATE = name
    return name


def _prune(conn, keep: str) -> None:
    old = [r[0] for r in conn.execute(
        "select name from evowiki_test_templates where name <> %s and last_used < now() - make_interval(days => %s::int)",
        (keep, TEMPLATE_TTL_DAYS))]
    for t in old:
        if not re.fullmatch(rf"{TEMPLATE_PREFIX}[0-9a-f]+", t):
            continue
        with contextlib.suppress(psycopg.Error):
            conn.execute(f"alter database {t} with is_template false")
            conn.execute(f"drop database if exists {t}")
            conn.execute("delete from evowiki_test_templates where name = %s", (t,))
    busy = {r[0] for r in conn.execute("select distinct datname from pg_stat_activity where datname is not null")}
    for (db,) in conn.execute("select datname from pg_database where datname like %s",
                              (TEST_DB_PREFIX.replace("_", r"\_") + "%",)).fetchall():
        m = re.fullmatch(rf"{TEST_DB_PREFIX}(\d+)_[0-9a-f]+", db)
        if m is None or db in busy or psutil.pid_exists(int(m[1])):
            continue                                # an old-style name, in use, or its run may be alive
        with contextlib.suppress(psycopg.Error):
            conn.execute(f"drop database if exists {db}")


# Errors a CREATE DATABASE outlives: the template still has a backend for a moment (its build's
# connection exiting; "source database ... is being accessed by other users"), or the cluster is
# restarting after a backend died (every session is cut, then "in recovery mode" for seconds).
_TRANSIENT = ("is being accessed by other users", "recovery mode", "not yet accepting connections",
              "terminating connection", "server closed the connection", "connection refused")


def _create_database(admin_url: str, sql: str, timeout: float = 120) -> None:
    deadline, delay, retried = time.monotonic() + timeout, 0.25, False
    while True:
        try:
            with psycopg.connect(admin_url, autocommit=True, connect_timeout=10) as conn:
                conn.execute(sql)
            return
        except psycopg.errors.DuplicateDatabase:
            if retried:                          # the attempt that lost its connection had created it
                return
            raise
        except psycopg.OperationalError as e:
            msg = str(e).lower()
            if time.monotonic() >= deadline or not any(t.lower() in msg for t in _TRANSIENT):
                raise
        retried = True
        time.sleep(delay)
        delay = min(delay * 2, 5)


def _new_name() -> str:
    return f"{TEST_DB_PREFIX}{os.getpid()}_{uuid.uuid4().hex[:10]}"


def fresh_database(admin_url: str, template: str | None = None) -> tuple[str, str]:
    template = template or _TEMPLATE
    if template is None:
        raise RuntimeError("call build_template(admin_url) before fresh_database")
    name = _new_name()
    _create_database(admin_url, f"create database {name} template {template}")
    return make_conninfo(admin_url, dbname=name), name


def blank_database(admin_url: str) -> tuple[str, str]:
    """A brand-new database without the template (no extension, no schema), named like the
    per-test databases so a killed run's leftover is dropped at the next session start."""
    name = _new_name()
    _create_database(admin_url, f"create database {name}")
    return make_conninfo(admin_url, dbname=name), name


# Everything a test could change about the schema (objects, columns, constraints, triggers and
# whether they fire, functions, types, extensions, applied migrations): equal to the template's
# value = only rows changed, so emptying the tables restores the template.
_FINGERPRINT = r"""
with ns as (select oid from pg_namespace where nspname !~ '^pg_' and nspname <> 'information_schema'),
rel as (select oid from pg_class where relnamespace in (select oid from ns))
select md5(concat_ws('|',
  (select string_agg(nspname, ',' order by nspname) from pg_namespace where oid in (select oid from ns)),
  (select string_agg(format('%s:%s:%s', oid, relkind, relname), ',' order by oid) from pg_class
     where oid in (select oid from rel)),
  (select string_agg(format('%s.%s:%s:%s:%s:%s', attrelid, attnum, attname, atttypid, attnotnull,
                            attisdropped), ',' order by attrelid, attnum) from pg_attribute
     where attrelid in (select oid from rel) and attnum > 0),
  (select string_agg(oid::text, ',' order by oid) from pg_attrdef where adrelid in (select oid from rel)),
  (select string_agg(oid::text, ',' order by oid) from pg_constraint where connamespace in (select oid from ns)),
  (select string_agg(format('%s:%s', oid, tgenabled), ',' order by oid) from pg_trigger
     where tgrelid in (select oid from rel)),
  (select string_agg(oid::text, ',' order by oid) from pg_proc where pronamespace in (select oid from ns)),
  (select string_agg(oid::text, ',' order by oid) from pg_type where typnamespace in (select oid from ns)),
  (select string_agg(oid::text, ',' order by oid) from pg_enum),
  (select string_agg(format('%s:%s', extname, extversion), ',' order by extname) from pg_extension),
  (select string_agg(id, ',' order by id) from schema_migrations)))
"""
_TABLES = ("select format('%I.%I', schemaname, tablename) from pg_tables "
           "where schemaname !~ '^pg_' and schemaname <> 'information_schema' and tablename <> 'schema_migrations'")


class ProcessDatabase:
    """One database per pytest process, created from the template once and emptied before every
    test instead of a CREATE DATABASE + DROP DATABASE per test: each DROP forces an immediate
    checkpoint of the whole cluster, so concurrent runs queued behind one another's checkpoints.
    `reset` gives the next test the template's state: other sessions on it are terminated (as the
    old per-test DROP ... WITH (FORCE) did), rows are deleted from the tables that have any and the
    used sequences restart; if the test changed the schema (fingerprint differs: DDL, DBOS's schema,
    migrations), the database is dropped and created again instead.

    Not transaction rollback: the app commits its own transactions on pooled connections."""

    def __init__(self, admin_url: str, template: str | None = None):
        self.admin_url, self.template = admin_url, template
        self.url: str | None = None
        self.name: str | None = None
        self._print: str | None = None
        self._probe = ""

    def reset(self) -> str:
        if self.name is not None and self._empty():
            return self.url
        self.close()
        self.url, self.name = fresh_database(self.admin_url, self.template)
        with psycopg.connect(self.url, autocommit=True) as conn:
            self._print = conn.execute(_FINGERPRINT).fetchone()[0]
            tables = [t for (t,) in conn.execute(_TABLES)]
        # one round trip: the tables that have a row
        self._probe = " union all ".join(f"select '{t}' where exists (select from {t})" for t in tables)
        return self.url

    def _empty(self) -> bool:
        """Back to the template's state if only rows changed (True); False: recreate."""
        try:
            with psycopg.connect(self.url, autocommit=True, connect_timeout=10) as conn:
                conn.execute("select pg_terminate_backend(pid) from pg_stat_activity "
                             "where datname = current_database() and pid <> pg_backend_pid()")
                if conn.execute(_FINGERPRINT).fetchone()[0] != self._print:
                    return False
                dirty = [t for (t,) in conn.execute(self._probe)] if self._probe else []
                with conn.transaction():
                    # replica: neither the append-only guards nor the foreign keys' triggers fire
                    conn.execute("set local session_replication_role = replica")
                    for t in dirty:
                        conn.execute(f"delete from {t}")
                    conn.execute("select setval(format('%I.%I', schemaname, sequencename)::regclass, "
                                 "start_value, false) from pg_sequences where last_value is not null")
            return True
        except psycopg.Error:                     # e.g. schema_migrations dropped, connections refused
            return False

    def close(self) -> None:
        if self.name is not None:
            drop_database(self.admin_url, self.name)
            self.url = self.name = None


def drop_database(admin_url: str, name: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f"drop database if exists {name} with (force)")
