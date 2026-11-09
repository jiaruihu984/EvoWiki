"""One command to run EvoWiki day to day: dev Postgres, migrations, backend, Obsidian plugin.

  uv run python scripts/evowiki_ctl.py up [--port 8002] [--vault DIR] [--no-bot]
  uv run python scripts/evowiki_ctl.py down [--db]
  uv run python scripts/evowiki_ctl.py status

Open EvoWiki.exe in the repo root to manage services from its tray menu.

`up` reuses only a healthy owned process with matching source/config and build identity. When it
must restart, it drains HTTP/DBOS work before stopping the old owned tree. Dependency syncing is
performed by evowiki_run.py only after this preflight; use that wrapper for ordinary commands.
The remaining steps are:
  1. database   - when the Windows service EvoWikiPG serves the data dir (install_pg_service.ps1):
                  start it if stopped (`sc start`; the install script granted you that right) and
                  check its port against .env. Otherwise start the dev Postgres (scripts/dev_db.py,
                  WMI-detached) unless it already runs, on the port of .env's EVOWIKI_DATABASE_URL;
                  a port Windows reserved is explained.
  2. migrations - apply pending SQL migrations; a pg_dump backup is taken first and a failed
                  migration stops everything (its transaction is rolled back; the backup path is printed).
  3. port       - the preferred port (8002) if free; else the next free
                  port that is not 8001 (another app of the user) and the plugin's serverUrl follows.
  4. backend    - `python -m evowiki.service serve`, started through WMI so it outlives this window
                  and Claude-app updates. Its pid goes into <main>/.evowiki-run/state.json.
  5. plugin     - rebuild obsidian-plugin/ when its sources are newer than main.js, copy main.js,
                  manifest.json and styles.css into <vault>/.obsidian/plugins/evowiki when they
                  differ, and check settings.serverUrl in its data.json.

Code, .env and the data dir all come from the MAIN checkout (like dev_db.py), also when this script
is run from a git worktree; pass --root to serve another checkout.

`down` stops only processes this script started (the pid in state.json, checked against its start
time and directory): never another app's process, never a backend started some other way. `--db`
also stops a detached Postgres only when `up` started it. Windows PostgreSQL services are retained
because a cluster can contain other applications' databases; service administrators manage them.

backend.log and supervisor.log keep the stdout capture (`>>` in the .cmd) and are rotated by size
(dev_db.rotate_log: 20 MB, 5 old files) each time `up` starts the process, while nothing holds them.

Live mode (plan P3 T11):
  uv run python scripts/evowiki_ctl.py up --from live [--resume]
  uv run python scripts/evowiki_ctl.py supervise --port N        (started by `up --from live`)

`up --from live` serves the sibling worktree <repo-name>-live on branch rsi/live (created from master once, never
reset) with EVOWIKI_COMMIT / EVOWIKI_LIVE_ROOT in the backend's env, and starts the supervisor
detached. Every SUPERVISE_POLL_S the supervisor runs one `forge.patch.live.step` (land, revert,
revert-all, rebase; restart only at an idle point, then canary + auto-revert). The kill switch and a
paused supervisor skip landings but keep serving reverts. A fallback to the pre-tick HEAD (or two
canary failures in a row) pauses landings until `up --from live --resume`. After a rebase the
supervisor exits with RESTART_CODE and its .cmd starts it again, so its own forge code matches the
new tree. It only ever restarts the backend this script started; `down` stops it first."""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dev_db
from evowiki_args import BACKEND_PORT, parse_launcher_args
from evowiki_run import runtime_args, vault_ownership_error
from launcher_i18n import cli_format, cli_text, print
from uv_args import env_file_arg

RESERVED_PORTS = frozenset({8001})          # another app of the user: never used, never touched
PORT_SEARCH = 20
PLUGIN_FILES = ("main.js", "manifest.json", "styles.css")
READY_TIMEOUT = 180
DRAIN_TIMEOUT = 120
STEPS = 5
# mirrors of evowiki.forge.limits (a test keeps them equal); `up` must not need the evowiki package
LIVE_ROOT = dev_db.main_checkout().with_name(dev_db.main_checkout().name + "-live")
LIVE_BRANCH = "rsi/live"
SUPERVISE_POLL_S = 30
RESTART_CODE = 75                           # supervisor exit code: its .cmd starts it again (re-exec)
FORGE_OFF = ("off", "0", "false")           # evowiki.forge.switch.OFF
PAUSE_FALLBACK = "启动失败，已回到之前的版本；自动改动已暂停"
PAUSE_TWICE = "连续两次启动失败；自动改动已暂停"


class Refused(Exception):
    """A step cannot continue safely; the message says why and what to do."""


def say(step: int, name: str, result: str, detail: str = "") -> None:
    name, result, detail = (cli_text(value) for value in (name, result, detail))
    print(f"[{step}/{STEPS}] {name:<4} {result}" + (f" - {detail}" if detail else ""), flush=True)


# ---------- paths and state ----------

class Paths:
    def __init__(self, main: Path, root: Path, vault: Path):
        self.main, self.root, self.vault = main, root, vault
        self.env = main / ".env"
        self.data_dir = main / ".pgdata"
        self.run = main / ".evowiki-run"
        self.state = self.run / "state.json"
        self.backend_log = self.run / "backend.log"
        self.backend_cmd = self.run / "backend.cmd"
        self.backups = self.run / "backups"
        self.supervisor_cmd = self.run / "supervisor.cmd"
        self.supervisor_log = self.run / "supervisor.log"
        self.plugin_src = root / "obsidian-plugin"
        self.plugin_dest = vault / ".obsidian" / "plugins" / "evowiki"


def load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise Refused(cli_format('运行记录 {p0} 无法读取；无法核验已有进程，先检查并恢复运行记录，不能继续升级', p0=path)) from exc
    if not isinstance(value, dict):
        raise Refused(cli_format('运行记录 {p0} 必须是 JSON 对象；无法核验已有进程，不能继续升级', p0=path))
    return value


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def owned_process(entry: dict | None) -> psutil.Process | None:
    """The process recorded in `entry` ({pid, create_time}) if it is still that same process."""
    if not entry or "pid" not in entry:
        return None
    try:
        p = psutil.Process(int(entry["pid"]))
        if abs(p.create_time() - float(entry["create_time"])) > 0.01:
            return None                      # the pid was reused by another process
        return p
    except (psutil.Error, KeyError, TypeError, ValueError):
        return None


def owned_backend(paths: Paths, state: dict) -> psutil.Process | None:
    entry = state.get("backend") or {}
    proc = backend_proc(state)
    if proc is None:
        return None
    root = entry.get("root")
    instance = entry.get("instance_root", root)
    if not root or not instance or not _same_path(instance, paths.main):
        raise Refused("后端进程的仓库归属无法确认；没有停止它，请检查 state.json 和运行目录")
    if not (_same_path(root, paths.main) or _same_path(root, paths.root) or
            live_worktree_branch(paths.main, Path(root)) == LIVE_BRANCH):
        raise Refused("记录的后端目录不是本实例 checkout，没有停止它")
    try:
        if not _same_path(proc.cwd(), root):
            raise Refused("后端进程的实际运行目录与记录不符，没有停止它")
    except psutil.Error as exc:
        raise Refused("无法读取后端进程的运行目录，没有停止它") from exc
    return proc


def runtime_signature(paths: Paths) -> str:
    """Detect source/config changes even in an uncommitted working tree; never store secrets."""
    h = hashlib.sha256()
    transient = {"EVOWIKI_NO_PAUSE", "EVOWIKI_COMMIT", "EVOWIKI_INSTANCE_ROOT", "EVOWIKI_LIVE_ROOT", "EVOWIKI_START_DRAINED", "EVOWIKI_CLI_LANGUAGE"}
    for name, value in sorted(os.environ.items()):
        if ((name.startswith(("EVOWIKI_", "ANTHROPIC_", "DEEPSEEK_", "TAVILY_")) and name not in transient) or
                name in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "FASTEMBED_CACHE_PATH", "PLAYWRIGHT_BROWSERS_PATH")):
            h.update(name.encode("utf-8") + b"=" + value.encode("utf-8") + b"\0")
    inputs = [paths.env, paths.root / "pyproject.toml", paths.root / "uv.lock"]
    for directory in (paths.root / "src" / "evowiki", paths.root / "scripts", paths.plugin_src / "src"):
        inputs.extend(p for p in directory.rglob("*") if p.is_file() and p.suffix in (".py", ".sql", ".ts", ".css"))
    inputs.extend(paths.plugin_src / name for name in ("package.json", "package-lock.json", "manifest.json", "styles.css", "esbuild.config.mjs"))
    for path in sorted(set(inputs)):
        h.update(str(path).encode("utf-8"))
        if path.is_file():
            h.update(path.read_bytes())
    return h.hexdigest()


read_env = dev_db.read_env


def git_head(root: Path) -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else "?"


def git_head_full(root: Path) -> str:
    r = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, capture_output=True, text=True,
                       check=False)
    if r.returncode != 0 or not r.stdout.strip():
        raise Refused(cli_format('读不到 {p0} 的当前提交：{p1}', p0=root, p1=r.stderr.strip()))
    return r.stdout.strip()


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", check=False)
    if check and r.returncode != 0:
        raise Refused(cli_format('git {p0} 失败：{p1}', p0=args[0], p1=r.stderr.strip()))
    return r


def _same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def live_worktree_branch(main: Path, live: Path) -> str | None:
    """The branch checked out at `live` when it is a worktree of `main`'s repo; "" when it is one but
    detached; None when it is not a worktree of that repo."""
    out = _git(main, "worktree", "list", "--porcelain").stdout
    for block in out.split("\n\n"):
        lines = dict(ln.split(" ", 1) if " " in ln else (ln, "") for ln in block.splitlines() if ln)
        if "worktree" in lines and _same_path(lines["worktree"], live):
            return lines.get("branch", "").removeprefix("refs/heads/")
    return None


def ensure_live(main: Path, live: Path, branch: str = LIVE_BRANCH) -> str:
    """Make `live` a worktree of `main` on `branch`: "existing" when it already is; else add it on the
    existing branch ("branch") or create the branch from master ("created"). Never `-B`, never
    resets a branch; refuses when `live` exists and is not that worktree."""
    have = live_worktree_branch(main, live)
    if have is not None:
        if have != branch:
            raise Refused(cli_format('{p0} 是本仓库的工作树，但检出的是 {p1}，不是 {p2}；请手动处理后再试', p0=live, p1=have or '游离 HEAD', p2=branch))
        return "existing"
    if live.exists():
        raise Refused(cli_format('{p0} 已存在，但不是本仓库 {p1} 的工作树；脚本不会动它，请移走后再试', p0=live, p1=branch))
    if _git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        _git(main, "worktree", "add", "-q", "--", str(live), branch)
        return "branch"
    _git(main, "worktree", "add", "-q", "-b", branch, "--", str(live), "master")
    return "created"


def check_vault(vault: Path, main: Path) -> None:
    v = vault.resolve()
    if vault_ownership_error(v, main):
        raise Refused(cli_format('{p0} 是其他实例的托管库或归属记录损坏；用 --vault 指定其他库', p0=vault))
    if not (v / ".obsidian").is_dir():
        raise Refused(cli_format('{p0} 不是 Obsidian 库（没有 .obsidian 文件夹）；先在 Obsidian 里打开它一次', p0=vault))


# ---------- 1. database ----------

def step_db(paths: Paths, state: dict) -> str:
    existed = (paths.data_dir / "PG_VERSION").exists()
    url = read_env(paths.env).get("EVOWIKI_DATABASE_URL", "")
    if not url:
        raise Refused(cli_format('{p0} 里没有 EVOWIKI_DATABASE_URL', p0=paths.env))
    try:
        port = dev_db.configured_port(paths.env)
    except ValueError as e:
        raise Refused(str(e)) from None
    svc = dev_db.service_info(paths.data_dir)
    if svc is not None:                     # the Windows service (install_pg_service.ps1) owns the server
        try:
            how = dev_db.ensure_service(paths.data_dir, port)
        except RuntimeError as e:
            raise Refused(str(e)) from None
        state["db"] = {"service": svc["name"]}
        say(1, "数据库", "跳过" if how == "running" else "已启动",
            cli_format('Windows 服务 {p0}（127.0.0.1:{p1}）', p0=svc['name'], p1=port))
        return url
    info = dev_db._info(paths.data_dir)
    if info is not None and info.is_running() and info.status == "ready":
        if info.port != port:
            raise Refused(cli_format('{p0} 上已有 Postgres 在端口 {p1} 运行，不是 {p2} 里的 {p3}；先停掉它（pg_ctl -D {p4} stop），或把 .env 的端口改成 {p5}', p0=paths.data_dir, p1=info.port, p2=paths.env, p3=port, p4=paths.data_dir, p5=info.port))
        say(1, "数据库", "跳过", cli_format('已在运行（127.0.0.1:{p0}）', p0=port))
        return url
    offset = dev_db.log_size(paths.data_dir)
    r = subprocess.run([sys.executable, str(HERE / "dev_db.py"), str(paths.data_dir), "--port", str(port)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if r.returncode != 0:
        hint = dev_db.bind_denied_hint(dev_db.log_since(paths.data_dir, offset), port, paths.env)
        if hint:
            raise Refused(cli_format('{p0}\n日志 {p1}', p0=hint, p1=dev_db.log_path(paths.data_dir)))
        raise Refused(cli_format('dev_db.py 启动失败：\n{p0}', p0=(r.stderr or r.stdout).strip()))
    info = dev_db._info(paths.data_dir)
    if info is None or not info.is_running():
        raise Refused(cli_format('dev_db.py 返回成功但 Postgres 没在运行；看日志 {p0}', p0=dev_db.log_path(paths.data_dir)))
    p = info.process
    state["db"] = {"pid": p.pid, "create_time": p.create_time(),
                   "data_dir": str(paths.data_dir.resolve()), "instance_root": str(paths.main.resolve())} if p is not None else {}
    if not existed:
        (paths.data_dir / ".evowiki-owner.json").write_text(json.dumps({"instance_root": str(paths.main.resolve())}), encoding="utf-8")
    say(1, "数据库", "已启动", cli_format('127.0.0.1:{p0}，日志 {p1}', p0=port, p1=dev_db.log_path(paths.data_dir)))
    return url


# ---------- 2. migrations ----------

def pending_migrations(conn, migrations_dir: Path) -> tuple[list[str], list[str]]:
    """(pending ids in apply order, ids applied in the DB but unknown to this checkout)."""
    have = conn.execute("select to_regclass('schema_migrations') is not null").fetchone()[0]
    applied = {r[0] for r in conn.execute("select id from schema_migrations")} if have else set()
    known = {p.stem for p in migrations_dir.glob("*.sql")}
    return sorted(known - applied), sorted(applied - known)


def pg_dump_path() -> Path:
    from pixeltable_pgserver import postgres_server
    bins = dev_db.main_bin_paths(dev_db.main_checkout(), dict(postgres_server.POSTGRES_VERSIONS))
    bin_dir = bins[postgres_server.TARGET_POSTGRES_VERSION]
    dev_db.prepare_windows_bins(bin_dir)
    return bin_dir / ("pg_dump.exe" if sys.platform == "win32" else "pg_dump")


def load_migrate(root: Path):
    """The served checkout's evowiki/store/migrate.py (its MIGRATIONS_DIR is next to it), also when
    this script runs in another checkout's venv."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_served_migrate", root / "src" / "evowiki" / "store" / "migrate.py")
    if spec is None or spec.loader is None:
        raise Refused(cli_format('{p0} 里没有 src/evowiki/store/migrate.py', p0=root))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def step_migrate(paths: Paths, url: str, backend_running: bool) -> None:
    import psycopg

    mod = load_migrate(paths.root)
    MIGRATIONS_DIR, migrate = mod.MIGRATIONS_DIR, mod.migrate
    if backend_running:
        raise Refused("后端仍在运行；先安全 down，再应用迁移")
    try:
        conn = psycopg.connect(url, connect_timeout=5, autocommit=True)
    except psycopg.OperationalError as exc:
        from evowiki.service.backup import scrub
        raise Refused(cli_format('连不上数据库：{p0}', p0=scrub(str(exc), url))) from exc
    with conn:
        pending, unknown = pending_migrations(conn, MIGRATIONS_DIR)
        if unknown:
            raise Refused(cli_format('数据库包含本代码不认识的迁移 {p0}；请使用匹配的新版代码，或恢复到新数据库，不能直接降级', p0=', '.join(unknown)))
        if not pending:
            say(2, "迁移", "跳过", "没有待应用的迁移")
            return
        paths.backups.mkdir(parents=True, exist_ok=True)
        from evowiki.service.backup import BackupFailed, backup_before_migration, scrub
        print(cli_format('迁移前备份：数据库与最新遗忘账本 → {p0}', p0=paths.backups), flush=True)
        try:
            backup = backup_before_migration({**read_env(paths.env), "EVOWIKI_ADMIN_DATABASE_URL": url,
                                              "EVOWIKI_PG_DUMP": str(pg_dump_path())}, paths.env, paths.backups)
        except (BackupFailed, OSError) as exc:
            raise Refused(cli_format('迁移前备份失败，没有应用任何迁移：{p0}', p0=scrub(str(exc), url))) from exc
        try:
            applied = migrate(conn)
        except psycopg.Error as exc:
            left, _ = pending_migrations(conn, MIGRATIONS_DIR)
            failed = left[0] if left else "?"
            raise Refused(cli_format('迁移 {p0} 失败，已回滚这一条，后面的都没应用：{p1}\n迁移前备份：{p2}（pg_restore 可恢复）', p0=failed, p1=scrub(str(exc), url), p2=backup)) from exc
    say(2, "迁移", "已应用", cli_format('{p0}；备份 {p1}', p0=', '.join(applied), p1=backup.name))


# ---------- 3. port ----------

def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def is_evowiki(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=timeout) as r:
            body = json.loads(r.read())
            info = body.get("info") if isinstance(body, dict) else None
            return isinstance(info, dict) and info.get("title") == "EvoWiki"
    except (OSError, ValueError):
        return False


def backend_identity(port: int) -> dict | None:
    try:
        status, raw = _http("GET", f"http://127.0.0.1:{port}/health")
        body = json.loads(raw) if status == 200 else None
        return body if isinstance(body, dict) else None
    except (OSError, ValueError):
        return None


def matches_identity(paths: Paths, identity: dict | None) -> bool:
    return bool(identity and identity.get("instance_root") and
                _same_path(identity["instance_root"], paths.main) and
                identity.get("commit") == git_head_full(paths.root) and identity.get("protocol_version") == 3)


def choose_port(preferred: int, free=port_free) -> int:
    """Choose a free port; occupied listeners are never adopted."""
    if preferred in RESERVED_PORTS:
        raise Refused(cli_format('端口 {p0} 属于其他应用，不能用', p0=preferred))
    for port in range(preferred, preferred + PORT_SEARCH + 1):
        if port in RESERVED_PORTS:
            continue
        if free(port):
            return port
    raise Refused(cli_format('{p0}–{p1} 都被其他程序占用；用 --port 指定一个空闲端口', p0=preferred, p1=preferred + PORT_SEARCH))


def backend_proc(state: dict) -> psutil.Process | None:
    return owned_process(state.get("backend"))


def step_port(preferred: int, state: dict, paths: Paths) -> tuple[int, bool]:
    """(port, already_running)."""
    mine = owned_backend(paths, state)
    if mine is not None and matches_identity(paths, backend_identity(state["backend"]["port"])):
        port = state["backend"]["port"]
        say(3, "端口", "跳过", cli_format('本脚本启动的后端已在 {p0} 运行', p0=port))
        return port, True
    port = choose_port(preferred)
    detail = cli_format('{p0} 空闲', p0=port) if port == preferred else cli_format('{p0} 被其他程序占用，改用 {p1}', p0=preferred, p1=port)
    say(3, "端口", "OK", detail)
    return port, False


# ---------- 4. backend ----------

def backend_cmdline(paths: Paths, port: int, uv: str, bot: bool) -> str:
    args = [uv, "run", "--no-sync", "--directory", paths.root.as_posix()]
    args += ["--env-file", env_file_arg(paths.env), "python", "-m", "evowiki.service", "serve", "--port", str(port)]
    return subprocess.list2cmdline(args)


def _esc(s: str) -> str:
    return s.replace("%", "%%")


def write_backend_cmd(paths: Paths, port: int, uv: str, bot: bool, commit: str | None = None,
                      live: bool = False, start_drained: bool = False) -> None:
    """A .cmd run by WMI: the WMI process has neither the user's PATH nor this window's env.
    `commit` → EVOWIKI_COMMIT (what /health reports, checked by the canary); `live` →
    EVOWIKI_LIVE_ROOT = the served root."""
    esc = _esc
    env = {**read_env(paths.env), **os.environ}
    device = env.get("EVOWIKI_EMBED_PROVIDERS", "cpu") or "cpu"
    if device not in ("cpu", "cuda"):
        raise Refused("EVOWIKI_EMBED_PROVIDERS 必须是 cpu 或 cuda")
    lines = ["@echo off", "chcp 65001 >nul",
             f'set "PATH={esc(os.environ.get("PATH", ""))}"',
             'set "PYTHONIOENCODING=utf-8"',
             f'set "EVOWIKI_EMBED_PROVIDERS={device}"',
             f'set "EVOWIKI_INSTANCE_ROOT={esc(str(paths.main.resolve()))}"',
             f'set "EVOWIKI_PG_DUMP={esc(env.get("EVOWIKI_PG_DUMP") or str(pg_dump_path()))}"',
             f'set "EVOWIKI_START_DRAINED={"1" if start_drained else ""}"',
             f'set "EVOWIKI_BOT={esc(env.get("EVOWIKI_BOT", "") if bot else "off")}"']
    # WMI does not inherit this shell's cache locations. Keep preparation and the detached
    # backend on the same explicit caches, without writing API keys into a command file.
    for name in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "FASTEMBED_CACHE_PATH", "PLAYWRIGHT_BROWSERS_PATH"):
        if env.get(name):
            lines.append(f'set "{name}={esc(env[name])}"')
    lines.append(f'set "EVOWIKI_COMMIT={esc(commit or git_head_full(paths.root))}"')
    if live:
        lines.append(f'set "EVOWIKI_LIVE_ROOT={esc(paths.root.as_posix())}"')
    lines += [f'cd /d "{paths.root}"',
             f'echo ==== %date% %time% start port {port} >> "{paths.backend_log}"',
             f'{esc(backend_cmdline(paths, port, uv, bot))} >> "{paths.backend_log}" 2>&1']
    paths.backend_cmd.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def log_tail(path: Path, n: int = 25) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return "(没有日志)"


def step_backend(paths: Paths, port: int, running: bool, state: dict, bot: bool,
                 commit: str | None = None, live: bool = False) -> None:
    if running:
        if not matches_identity(paths, backend_identity(port)):
            raise Refused("后端实例或构建不匹配；没有安装新版插件，请先安全 down 再 up")
        say(4, "后端", "跳过", cli_format('已在运行 http://127.0.0.1:{p0}', p0=port))
        return
    launch_backend(paths, port, state, bot=bot, commit=commit, live=live)


def launch_backend(paths: Paths, port: int, state: dict, *, bot: bool, commit: str | None = None,
                   live: bool = False, start_drained: bool = False) -> None:
    """Start the backend through WMI, record it in state.json and wait until it answers (step 4; also
    the supervisor's restart)."""
    uv = shutil.which("uv")
    if uv is None:
        raise Refused("找不到 uv（https://docs.astral.sh/uv/）")
    paths.run.mkdir(parents=True, exist_ok=True)
    dev_db.rotate_log(paths.backend_log)    # the backend's stdout is appended to it; rotated while closed
    write_backend_cmd(paths, port, uv, bot, commit=commit, live=live, start_drained=start_drained)
    rv, pid = dev_db.start_detached(f'cmd.exe /d /c "{paths.backend_cmd}"', paths.root)
    if rv != 0 or pid == 0:
        raise Refused(cli_format('WMI 启动后端失败（ReturnValue {p0}）', p0=rv))
    proc = psutil.Process(pid)
    state["backend"] = {"pid": pid, "create_time": proc.create_time(), "port": port, "vault": str(paths.vault.resolve()),
                        "head": git_head(paths.root), "root": str(paths.root), "bot": bot,
                        "instance_root": str(paths.main.resolve()), "commit": commit or git_head_full(paths.root),
                        "signature": runtime_signature(paths)}
    if live:
        state["backend"].update({"from": "live", "commit": commit})
    save_state(paths.state, state)
    deadline = time.monotonic() + READY_TIMEOUT
    while not (matches_identity(paths, backend_identity(port)) and LauncherBackend(paths, port).smoke()):
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            state.pop("backend", None)
            save_state(paths.state, state)
            raise Refused(cli_format('后端启动后退出了；日志 {p0}：\n{p1}', p0=paths.backend_log, p1=log_tail(paths.backend_log)))
        if time.monotonic() >= deadline:
            raise Refused(cli_format('后端 {p0}s 内没就绪（进程仍在，可用 down 停掉）；日志 {p1}：\n{p2}', p0=READY_TIMEOUT, p1=paths.backend_log, p2=log_tail(paths.backend_log)))
        time.sleep(1)
    say(4, "后端", "已启动", cli_format('http://127.0.0.1:{p0}（pid {p1}{p2}），日志 {p3}', p0=port, p1=pid, p2='，Bot 关闭' if not bot else '', p3=paths.backend_log))


# ---------- 5. plugin ----------

def newest_mtime(paths_: list[Path]) -> float:
    best = 0.0
    for p in paths_:
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    best = max(best, f.stat().st_mtime)
        elif p.exists():
            best = max(best, p.stat().st_mtime)
    return best


def needs_build(src: Path) -> bool:
    out = src / "main.js"
    if not out.exists():
        return True
    inputs = [src / "src", src / "styles.css", src / "manifest.json", src / "package-lock.json",
              src / "esbuild.config.mjs", src / "tsconfig.json"]
    return newest_mtime(inputs) > out.stat().st_mtime


def needs_install(src: Path) -> bool:
    marker = src / "node_modules" / ".package-lock.json"
    return not marker.exists() or (src / "package-lock.json").stat().st_mtime > marker.stat().st_mtime


def digest(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def sync_settings(data_json: Path, server_url: str, *, local_setup: dict | None = None) -> str:
    """Make settings.serverUrl in the plugin's data.json equal `server_url`, keeping everything else.
    Returns "same", "updated" or "created"."""
    if data_json.exists():
        try:
            data = json.loads(data_json.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise Refused(cli_format('{p0} 不是合法 JSON，没有改它：{p1}', p0=data_json, p1=exc)) from exc
        settings = data.setdefault("settings", {})
        if settings.get("serverUrl") == server_url and (local_setup is None or data.get("localSetup") == local_setup):
            return "same"
        settings["serverUrl"] = server_url
        how = "updated"
    else:
        data, how = {"settings": {"serverUrl": server_url}}, "created"
    if local_setup is not None:
        data["localSetup"] = local_setup
    temporary = data_json.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(data_json)
    return how


def run_npm(args: list[str], cwd: Path) -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise Refused("找不到 npm（需要 Node.js）")
    print(cli_format('插件准备：npm {p0}（首次下载可能需要几分钟）', p0=' '.join(args)), flush=True)
    try:
        r = subprocess.run([npm, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, check=False,
                           env={**os.environ, "npm_config_fetch_retries": "2", "npm_config_fetch_timeout": "60000"})
    except subprocess.TimeoutExpired as exc:
        raise Refused(cli_format('npm {p0} 超时（600秒），请检查 npm config get registry，再重试 up', p0=' '.join(args))) from exc
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-20:])
        raise Refused(cli_format('npm {p0} 失败，插件没有复制：\n{p1}', p0=' '.join(args), p1=tail))


def enabled_plugins(paths: Paths) -> tuple[Path, list[str]]:
    path = paths.vault / ".obsidian" / "community-plugins.json"
    _bounded_path(path, paths.vault)
    try:
        plugins = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError) as exc:
        raise Refused(cli_format('无法读取 {p0}；请先在 Obsidian 中检查社区插件设置', p0=path)) from exc
    if not isinstance(plugins, list) or not all(isinstance(p, str) for p in plugins):
        raise Refused(cli_format('{p0} 必须是插件名称数组；没有修改它', p0=path))
    return path, plugins


def set_plugin_enabled(paths: Paths, enabled: bool) -> None:
    path, plugins = enabled_plugins(paths)
    updated = ([*plugins, "evowiki"] if "evowiki" not in plugins else plugins) if enabled else [p for p in plugins if p != "evowiki"]
    if updated == plugins:
        return
    temporary = path.with_suffix(".json.evowiki-tmp")
    temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def enable_managed_default_plugin(paths: Paths) -> None:
    if (_same_path(paths.vault, paths.main / ".evowiki-vault") and
            _owned_marker(paths.vault / ".evowiki-owner.json", paths.main)):
        set_plugin_enabled(paths, True)


def build_plugin(paths: Paths) -> list[str]:
    did = []
    if needs_build(paths.plugin_src):
        if needs_install(paths.plugin_src):
            run_npm(["ci"], paths.plugin_src)
            did.append("npm ci")
        run_npm(["run", "build"], paths.plugin_src)
        did.append("已构建")
    return did


def step_plugin(paths: Paths, port: int) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise Refused("找不到 uv")
    local_setup = {"root": str(paths.main.resolve()), "executable": str(Path(uv).resolve()),
                   "args": ["run", "--isolated", "--no-project", "--with", "python-dotenv==1.2.4",
                            "python", str(paths.main.resolve() / "scripts" / "evowiki_run.py"), "setup"]}
    check_vault(paths.vault, paths.main)
    _bounded_path(paths.plugin_dest, paths.vault)
    marker = paths.plugin_dest / ".evowiki-install.json"
    if ((marker.exists() or any((paths.plugin_dest / name).exists() for name in PLUGIN_FILES))
            and not _owned_marker(marker, paths.main)):
        raise Refused(cli_format('插件由另一个目录安装或记录损坏：{p0}；先从原目录卸载插件，再重试 up', p0=paths.plugin_dest))
    if paths.plugin_dest.exists() and any(p.is_symlink() or p.is_junction() for p in paths.plugin_dest.rglob("*")):
        raise Refused("插件目录含外部链接；请先移走链接并保留 data.json，再重试 up")
    src = paths.plugin_src
    did = build_plugin(paths)
    paths.plugin_dest.parent.mkdir(parents=True, exist_ok=True)
    copied = [f for f in PLUGIN_FILES if digest(src / f) != digest(paths.plugin_dest / f)]
    stage = paths.plugin_dest.with_name(".evowiki-installing")
    previous = paths.plugin_dest.with_name(".evowiki-previous")
    if stage.exists() or previous.exists():
        raise Refused(cli_format('插件安装残留目录 {p0} 或 {p1}；检查并保留 data.json 后移走，再重试 up', p0=stage, p1=previous))
    if not copied and _owned_marker(marker, paths.main):
        how = sync_settings(paths.plugin_dest / "data.json", f"http://127.0.0.1:{port}", local_setup=local_setup)
        enable_managed_default_plugin(paths)
        say(5, "插件", "跳过" if how == "same" else "OK", cli_format('已是最新，serverUrl = http://127.0.0.1:{p0}（{p1}）', p0=port, p1=paths.vault))
        return
    stage.mkdir()
    try:
        if paths.plugin_dest.exists():
            for existing in paths.plugin_dest.iterdir():
                if existing.is_file() and existing.name not in PLUGIN_FILES:
                    shutil.copy2(existing, stage / existing.name)
                elif existing.is_dir():
                    shutil.copytree(existing, stage / existing.name)
        for f in PLUGIN_FILES:
            if not (src / f).is_file():
                raise Refused(cli_format('插件构建缺少 {p0}；请重新运行 up', p0=f))
            shutil.copy2(src / f, stage / f)
        how = sync_settings(stage / "data.json", f"http://127.0.0.1:{port}", local_setup=local_setup)
        (stage / ".evowiki-install.json").write_text(json.dumps({"instance_root": str(paths.main.resolve()),
                                                               "files": {f: digest(stage / f) for f in PLUGIN_FILES}}), encoding="utf-8")
        if paths.plugin_dest.exists():
            paths.plugin_dest.rename(previous)
        try:
            stage.rename(paths.plugin_dest)
        except OSError:
            if previous.exists():
                previous.rename(paths.plugin_dest)
            raise
        if previous.exists():
            shutil.rmtree(previous)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    enable_managed_default_plugin(paths)
    if copied:
        did.append(cli_format('已复制 {p0}', p0=', '.join(copied)))
    if how != "same":
        did.append(f"serverUrl {cli_text('已改为' if how == 'updated' else '已设为')} http://127.0.0.1:{port}")
    if not did:
        say(5, "插件", "跳过", cli_format('已是最新，serverUrl = http://127.0.0.1:{p0}（{p1}）', p0=port, p1=paths.vault))
        return
    hint = cli_text("；Obsidian 已开着这个库的话，重新加载插件（或重启 Obsidian）") if copied or how != "same" else ""
    say(5, "插件", "OK", cli_format("{p0}（{p1}）{p2}", p0=cli_text("，").join(did), p1=paths.vault, p2=hint))


# ---------- live: supervisor ----------

def write_supervisor_cmd(paths: Paths, port: int, uv: str) -> None:
    """The supervisor's .cmd (run by WMI). It loops while the supervisor exits with RESTART_CODE (its
    re-exec after a rebase), so the recorded cmd.exe pid stays the owner of the whole tree."""
    script = (paths.root / "scripts" / "evowiki_ctl.py").as_posix()
    run = subprocess.list2cmdline([uv, "run", "--no-sync", "--directory", paths.root.as_posix(), "--env-file",
                                   env_file_arg(paths.env), "python", script, "supervise", "--port", str(port)])
    lines = ["@echo off", "chcp 65001 >nul",
             f'set "PATH={_esc(os.environ.get("PATH", ""))}"',
             'set "PYTHONIOENCODING=utf-8"', 'set "PYTHONUNBUFFERED=1"',
             f'cd /d "{paths.root}"',
             ":loop",
             f'echo ==== %date% %time% supervise port {port} >> "{paths.supervisor_log}"',
             f'{_esc(run)} >> "{paths.supervisor_log}" 2>&1',
             f"if %errorlevel%=={RESTART_CODE} goto loop"]
    paths.supervisor_cmd.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def step_supervisor(paths: Paths, port: int, state: dict) -> None:
    sup = owned_process(state.get("supervisor"))
    if sup is not None:
        print(cli_format('监督进程：跳过 - 已在运行（pid {p0}）', p0=sup.pid), flush=True)
        return
    uv = shutil.which("uv")
    if uv is None:
        raise Refused("找不到 uv（https://docs.astral.sh/uv/）")
    paths.run.mkdir(parents=True, exist_ok=True)
    dev_db.rotate_log(paths.supervisor_log)
    write_supervisor_cmd(paths, port, uv)
    rv, pid = dev_db.start_detached(f'cmd.exe /d /c "{paths.supervisor_cmd}"', paths.root)
    if rv != 0 or pid == 0:
        raise Refused(cli_format('WMI 启动监督进程失败（ReturnValue {p0}）', p0=rv))
    state["supervisor"] = {"pid": pid, "create_time": psutil.Process(pid).create_time(), "port": port,
                           "instance_root": str(paths.main.resolve()), "root": str(paths.root.resolve())}
    save_state(paths.state, state)
    print(cli_format('监督进程：已启动（pid {p0}），日志 {p1}', p0=pid, p1=paths.supervisor_log), flush=True)


def resume_live(paths: Paths) -> None:
    state = load_state(paths.state)
    state.pop("live_paused", None)
    state.pop("canary_failures", None)
    save_state(paths.state, state)


def _http(method: str, url: str, body: dict | None = None, timeout: float = 2.0) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


class LauncherBackend:
    """`forge.patch.live.Backend` over the backend this script started. Only the owned process is
    ever killed; a backend started some other way is never restarted."""

    def __init__(self, paths: Paths, port: int, *, http=None, kill=None, launch=None):
        self.paths, self.port = paths, port
        self._http = http or _http
        self._kill = kill or kill_tree
        self._launch = launch or launch_backend
        self._ours: dict | None = None          # the last backend entry this object launched or owned

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def health(self) -> dict | None:
        try:
            status, raw = self._http("GET", self._url("/health"))
            h = json.loads(raw) if status == 200 else None
        except (OSError, ValueError):
            return None
        return h if isinstance(h, dict) else None

    def drain(self, on: bool) -> None:
        try:
            status, _ = self._http("POST", self._url("/health/drain"), {"on": on})
        except OSError as e:
            status = f"{type(e).__name__}"
        if status != 200:
            if on:
                raise Refused(cli_format('无法暂停新请求，取消本次更新：{p0}', p0=status))
            _log(cli_format('drain({p0}) 没有成功：{p1}', p0=on, p1=status))

    def smoke(self) -> bool:
        try:
            return self._http("GET", self._url("/health/ready"))[0] == 200
        except OSError:
            return False

    def restart(self) -> None:
        state = load_state(self.paths.state)
        entry = state.get("backend")
        proc = owned_backend(self.paths, state)
        if proc is not None:
            self._ours = dict(entry)
            self._kill(proc)
        elif self._ours is None or is_evowiki(self.port):
            # never started by this script (or another process serves the port now)
            raise Refused("后端不是本脚本启动的，不能自动重启")
        prev = self._ours
        self._launch(self.paths, prev.get("port", self.port), state, bot=prev.get("bot", True),
                     commit=git_head_full(self.paths.root), live=True, start_drained=True)
        self._ours = dict(state.get("backend") or prev)


def landings_allowed(pool, state: dict, env, *, forge_on=None) -> tuple[bool, str]:
    """(allowed, why not). Paused supervisor, `EVOWIKI_FORGE=off`, or the global patch switch off →
    no landings (reverts still run). A switch that cannot be read counts as off (fail closed)."""
    if state.get("live_paused"):
        return False, cli_format('暂停：{p0}', p0=state['live_paused'])
    if str(env.get("EVOWIKI_FORGE", "")).strip().lower() in FORGE_OFF:
        return False, "EVOWIKI_FORGE=off"
    try:
        on = (forge_on or _forge_patch_on)(pool) is True
    except Exception as e:  # noqa: BLE001 - fail closed
        return False, cli_format('读不到开关：{p0}', p0=type(e).__name__)
    return on, "" if on else "代码改动开关已关"


def _forge_patch_on(pool) -> bool:
    from evowiki.forge import switch
    with pool.connection() as conn:
        return switch.enabled(conn, None, "patch")


def _log(line: str) -> None:
    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {cli_text(line)}", flush=True)


def supervise_tick(paths: Paths, *, pool, backend, live, main, step, now, env=os.environ,
                   forge_on=None):
    """One poll: `step` with landings on or off, then the pause bookkeeping in state.json.
    Returns (StepResult, re-exec?): re-exec after the live branch was rebased onto a new master."""
    allowed, why = landings_allowed(pool, load_state(paths.state), env, forge_on=forge_on)
    try:
        base0 = live.merge_base("HEAD", "master")
    except Exception:  # noqa: BLE001 - only used to notice a rebase
        base0 = None
    r = step(pool, live=live, main=main, backend=backend, now=now, landings=allowed)
    state = load_state(paths.state)          # reload: `up --resume` may have run meanwhile
    if r.did == "canary_failed":
        n = int(state.get("canary_failures", 0)) + 1
        state["canary_failures"] = n
        if r.detail.startswith("fallback"):
            state["live_paused"] = PAUSE_FALLBACK
        elif n >= 2:
            state["live_paused"] = PAUSE_TWICE
    elif r.did in ("landed", "reverted", "rebased"):
        state.pop("canary_failures", None)
    save_state(paths.state, state)
    if r.did != "nothing" or r.candidate_ids:
        _log(f"{r.did} {','.join(r.candidate_ids)} {r.detail}".rstrip()
             + (cli_format('（不落地新改动：{p0}）', p0=why) if not allowed else ""))
    if state.get("live_paused") and r.did == "canary_failed":
        _log(cli_format('已暂停：{p0}；`evowiki_ctl.py up --from live --resume` 恢复', p0=state['live_paused']))
    reexec = r.did == "rebased"
    if not reexec and r.did not in ("nothing", "busy") and base0 is not None:
        try:
            reexec = live.merge_base("HEAD", "master") != base0
        except Exception:  # noqa: BLE001
            reexec = False
    return r, reexec


def _owned_live_backend(paths: Paths) -> bool:
    state = load_state(paths.state)
    b = state.get("backend") or {}
    return backend_proc(state) is not None and (not b.get("root") or _same_path(b["root"], paths.root))


def _make_tick(paths: Paths, port: int):
    from evowiki.forge.patch import live as live_mod
    from evowiki.forge.patch.git import Git
    from evowiki.store.db import database_url, make_pool

    pool = make_pool(database_url(), min_size=1, max_size=2)
    backend = LauncherBackend(paths, port)
    print(cli_format('安全停止：端口 {p0} 暂停新请求，等待正在执行的操作结束', p0=port), flush=True)
    live_git, main_git = Git(paths.root), Git(paths.main)
    return lambda: supervise_tick(paths, pool=pool, backend=backend, live=live_git, main=main_git,
                                  step=live_mod.step, now=lambda: datetime.now(UTC))


def supervise(paths: Paths, port: int, *, tick=None, sleep=time.sleep) -> int:
    """The supervisor loop. Exits 1 when the backend is not one this script started from the live
    root (it would not be allowed to restart it); returns RESTART_CODE to re-exec after a rebase."""
    if not _owned_live_backend(paths):
        print("失败：后端不是本脚本启动的，不能自动重启", file=sys.stderr, flush=True)
        return 1
    tick = tick or _make_tick(paths, port)
    _log(cli_format('监督进程开始：{p0}，端口 {p1}，每 {p2}s 检查一次', p0=paths.root, p1=port, p2=SUPERVISE_POLL_S))
    while True:
        if not _owned_live_backend(paths):
            print("失败：后端不是本脚本启动的，不能自动重启", file=sys.stderr, flush=True)
            return 1
        try:
            _, reexec = tick()
        except Exception as e:  # noqa: BLE001 - step already reset live; try again next poll
            _log(f"error {type(e).__name__}: {str(e)[:300]}")
            reexec = False
        if reexec:
            _log("live 已变基到新的 master，重启监督进程以加载新代码")
            return RESTART_CODE
        sleep(SUPERVISE_POLL_S)


def queued_count(paths: Paths) -> int | None:
    try:
        import psycopg

        from evowiki.store import forge_patch as ps
        url = read_env(paths.env).get("EVOWIKI_DATABASE_URL", "")
        with psycopg.connect(url, connect_timeout=3, autocommit=True) as conn:
            return len(ps.patches(conn, states=["queued"]))
    except Exception:  # noqa: BLE001 - status only: unknown is printed as unknown
        return None


# ---------- commands ----------

def listener_owned(proc: psutil.Process, port: int) -> bool:
    try:
        return any(c.status == psutil.CONN_LISTEN and c.laddr.port == port
                   for p in [proc, *proc.children(recursive=True)] for c in p.net_connections(kind="tcp"))
    except psutil.Error:
        return False


def stop_owned_backend(paths: Paths, state: dict) -> None:
    proc = owned_backend(paths, state)
    if proc is None:
        return
    port = state["backend"]["port"]
    if not listener_owned(proc, port):
        raise Refused(cli_format('端口 {p0} 不是记录的后端进程在监听；无法安全排空，没有停止任何后端', p0=port))
    backend = LauncherBackend(paths, port)
    backend.drain(True)
    deadline = time.monotonic() + DRAIN_TIMEOUT
    while True:
        health = backend.health()
        if health and health.get("drained") is True and health.get("in_flight") == 0:
            break
        if time.monotonic() >= deadline:
            raise Refused(cli_format('排空等待超过 {p0} 秒；后端仍保留且暂停新请求，请检查 status 和日志后重试 down', p0=DRAIN_TIMEOUT))
        time.sleep(1)
    proc = owned_backend(paths, state)
    if proc is not None:
        kill_tree(proc)
    state.pop("backend", None)
    save_state(paths.state, state)


def cmd_preflight(paths: Paths, args) -> int:
    """0: same healthy runtime, 75: safe to sync dependencies; errors fail closed."""
    check_vault(paths.vault, paths.main)
    git_head_full(paths.root)
    try:
        dev_db.configured_port(paths.env)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    if _git(paths.root, "diff", "--name-only", "--diff-filter=U").stdout.strip():
        raise Refused("工作区有未解决的 Git 冲突；先处理冲突，再重新 up")
    state = load_state(paths.state)
    proc = owned_backend(paths, state)
    if proc is not None:
        entry = state["backend"]
        health = backend_identity(entry["port"])
        if (matches_identity(paths, health) and not health.get("drained") and
                entry.get("signature") == runtime_signature(paths) and
                entry.get("bot", True) == (not getattr(args, "no_bot", False)) and
                LauncherBackend(paths, entry["port"]).smoke()):
            return 0
        cmd_down(paths, argparse.Namespace(db=False, port=entry["port"]))
        return RESTART_CODE
    entry = state.get("backend") or {}
    port = entry.get("port", getattr(args, "port", BACKEND_PORT))
    identity = backend_identity(port)
    if identity and identity.get("instance_root") and _same_path(identity["instance_root"], paths.main):
        raise Refused("本目录后端仍在运行，但 PID/创建时间不属于本启动器；请从原入口停止，不能升级或迁移")
    if entry and identity and not identity.get("instance_root"):
        raise Refused("记录的端口仍有旧版后端，实例归属无法确认；请从原入口停止，不能升级或迁移")
    return RESTART_CODE


def cmd_up(paths: Paths, args) -> int:
    if args.port in RESERVED_PORTS:
        raise Refused(cli_format('端口 {p0} 属于其他应用，不能用', p0=args.port))
    reusable = cmd_preflight(paths, args) == 0
    state = load_state(paths.state)
    if reusable:
        port = state["backend"]["port"]
        step_plugin(paths, port)
        print(cli_format('后端与插件文件已就绪：http://127.0.0.1:{p0}；在 Obsidian 打开 {p1} 并启用/重载 EvoWiki', p0=port, p1=paths.vault), flush=True)
        return 0
    url = step_db(paths, state)
    save_state(paths.state, state)
    # 0082: the backend connects as evowiki_app; migrations (and the backup) run as the owner
    admin = read_env(paths.env).get("EVOWIKI_ADMIN_DATABASE_URL") or url
    build_plugin(paths)
    step_migrate(paths, admin, backend_running=False)
    port, running = step_port(args.port, state, paths)
    if not running:
        uv = shutil.which("uv")
        if uv is None:
            raise Refused("找不到 uv")
        prepared = subprocess.run([uv, "run", "--directory", paths.root.as_posix(),
                                   *runtime_args({**read_env(paths.env), **os.environ}, paths.root),
                                   "--env-file", env_file_arg(paths.env), "python", "-m", "evowiki.service",
                                   "prepare"], check=False)
        if prepared.returncode != 0:
            raise Refused("首次准备未完成，请根据上面的错误修复后重新启动")
    live = getattr(args, "from_", "master") == "live"
    step_plugin(paths, port)
    step_backend(paths, port, running, state, bot=not args.no_bot,
                 commit=git_head_full(paths.root), live=live)
    if live:
        state = load_state(paths.state)
        if not _owned_live_backend(paths) or (state.get("backend") or {}).get("from") != "live":
            raise Refused("后端在运行，但不是本脚本从 live 启动的，监督进程不能重启它；"
                          "先 down 再 up --from live")
        step_supervisor(paths, port, state)
        if state.get("live_paused"):
            print(cli_format('自动改动：已暂停（{p0}）；up --from live --resume 恢复', p0=state['live_paused']), flush=True)
    print(cli_format('后端与插件文件已就绪：http://127.0.0.1:{p0}；在 Obsidian 打开 {p1} 并启用/重载 EvoWiki', p0=port, p1=paths.vault), flush=True)
    return 0


def prepare_live(paths: Paths, args) -> Paths:
    """`up --from live`: the sibling live worktree on rsi/live becomes the served root."""
    if args.root is not None:
        raise Refused("--from live 和 --root 不能同时用")
    how = ensure_live(paths.main, LIVE_ROOT)
    print({"existing": f"live：{LIVE_ROOT}（{LIVE_BRANCH}）",
           "branch": cli_format('live：已在 {p0} 检出已有分支 {p1}', p0=LIVE_ROOT, p1=LIVE_BRANCH),
           "created": cli_format('live：已从 master 新建 {p0}，检出在 {p1}', p0=LIVE_BRANCH, p1=LIVE_ROOT)}[how], flush=True)
    live_paths = Paths(paths.main, LIVE_ROOT, paths.vault)
    if args.resume:
        resume_live(live_paths)
        print("自动改动：已恢复", flush=True)
    return live_paths


def kill_tree(p: psutil.Process) -> None:
    procs = [*p.children(recursive=True), p]
    for q in procs:
        with contextlib.suppress(psutil.Error):
            q.kill()
    _, alive = psutil.wait_procs(procs, timeout=10)
    if alive:
        raise Refused("排空后的进程未退出；没有继续升级，请检查 status 后重试 down")


def cmd_down(paths: Paths, args) -> int:
    state = load_state(paths.state)
    sup = owned_process(state.get("supervisor"))
    if sup is not None:                     # first: it would restart the backend
        supervisor = state["supervisor"]
        root = supervisor.get("root")
        if not root or not _same_path(supervisor.get("instance_root", ""), paths.main):
            raise Refused("监督进程的目录归属无法确认，没有停止它")
        try:
            if not _same_path(sup.cwd(), root):
                raise Refused("监督进程的实际目录与记录不符，没有停止它")
        except psutil.Error as exc:
            raise Refused("无法核验监督进程的实际目录，没有停止它") from exc
        kill_tree(sup)
        print(cli_format('监督进程：已停止（pid {p0}）', p0=state['supervisor']['pid']))
    state.pop("supervisor", None)
    p = owned_backend(paths, state)
    if p is not None:
        port = state["backend"]["port"]
        stop_owned_backend(paths, state)
        print(cli_format('后端：已排空并停止（端口 {p0}）', p0=port))
    elif is_evowiki(state.get("backend", {}).get("port", args.port)):
        identity = backend_identity(state.get("backend", {}).get("port", args.port))
        if identity and identity.get("instance_root") and _same_path(identity["instance_root"], paths.main):
            raise Refused("本实例后端在运行，但 PID/创建时间无法确认；没有停止它，请从原入口停止")
        print("后端：其他实例在运行，没有停它")
    else:
        print("后端：没在运行")
    state.pop("backend", None)
    svc = dev_db.service_info(paths.data_dir) if args.db else None
    if svc is not None:
        print(cli_format('数据库：保留 Windows 服务 {p0}；服务可能供其他数据库使用，请由服务管理员单独管理', p0=svc['name']))
    elif args.db:
        info = dev_db._info(paths.data_dir)
        if info is None or not info.is_running():
            print("数据库：没在运行")
            state.pop("db", None)
        elif (owned_process(state.get("db")) is None or
              not _same_path(state["db"].get("instance_root", ""), paths.main) or
              not _same_path(state["db"].get("data_dir", ""), paths.data_dir) or
              info.process is None or info.process.pid != state["db"].get("pid")):
            print(cli_format('数据库：在运行，但不是本脚本启动的，没有停它（要停请自己执行 pg_ctl -D {p0} stop）', p0=paths.data_dir))
        else:
            pg_ctl = pg_dump_path().with_name("pg_ctl.exe" if sys.platform == "win32" else "pg_ctl")
            r = subprocess.run([str(pg_ctl), "stop", "-m", "fast", "-D", str(paths.data_dir)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            if r.returncode != 0:
                save_state(paths.state, state)
                raise Refused(cli_format('pg_ctl stop 失败：{p0}', p0=r.stderr.strip()))
            print("数据库：已停止")
            state.pop("db", None)
    save_state(paths.state, state)
    return 0


def _owned_marker(path: Path, main: Path) -> bool:
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
        return bool(isinstance(marker, dict) and marker.get("instance_root") and _same_path(marker["instance_root"], main))
    except (OSError, ValueError, TypeError):
        return False


def _bounded_path(path: Path, root: Path) -> Path:
    target, boundary = path.resolve(), root.resolve()
    if target == boundary or boundary not in target.parents or path.is_symlink() or path.is_junction():
        raise Refused(cli_format('删除目标超出本实例边界或是链接：{p0}', p0=path))
    return target


def _autostart(paths: Paths, apply: bool) -> None:
    if sys.platform != "win32":
        return
    import pythoncom
    import win32com.client

    def missing(exc) -> bool:
        code = exc.hresult
        # IDispatch wraps the underlying Win32 failure in DISP_E_EXCEPTION.
        if code & 0xFFFFFFFF == 0x80020009 and exc.excepinfo:
            code = exc.excepinfo[5]
        return isinstance(code, int) and code & 0xFFFFFFFF in {0x80070002, 0x80070003}

    initialized = False
    service = folder = task = definition = actions = action = None
    try:
        pythoncom.CoInitialize()
        initialized = True
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        try:
            folder = service.GetFolder("\\EvoWiki")
            task = folder.GetTask("Start")
        except pythoncom.com_error as exc:
            if missing(exc):
                return
            raise
        definition = task.Definition
        actions = definition.Actions
        if actions.Count == 1:
            action = actions.Item(1)
        if (action is None or action.Type != 0
                or not action.Path or not action.WorkingDirectory
                or not _same_path(action.Path, paths.main / "EvoWiki.exe")
                or not _same_path(action.WorkingDirectory, paths.main)):
            print("保留其他目录或无法确认归属的 EvoWiki 自启动任务")
            return
        if apply:
            folder.DeleteTask("Start", 0)
        print("本实例自启动任务：移除")
    except pythoncom.com_error as exc:
        raise Refused("无法检查/移除本实例的自启动任务，请检查任务计划程序后重试 uninstall") from exc
    finally:
        action = actions = definition = task = folder = service = None
        if initialized:
            pythoncom.CoUninitialize()


def _stop_tray(paths: Paths) -> None:
    executable = paths.main / "EvoWiki.exe"
    for candidate in psutil.process_iter(["pid", "exe"]):
        if candidate.pid == os.getpid() or not candidate.info.get("exe") or not _same_path(candidate.info["exe"], executable):
            continue
        try:
            process = owned_process({"pid": candidate.pid, "create_time": candidate.create_time()})
            if process is None or not _same_path(process.exe(), executable):
                continue
            if process.children(recursive=True):
                raise Refused("托盘仍在执行启动或控制命令；请等待操作结束后重试卸载")
            process.terminate()
            process.wait(timeout=10)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            raise Refused("无法停止本实例托盘；请退出托盘后重试卸载") from exc


def cmd_uninstall(paths: Paths, args) -> int:
    """Preview is the default; remove only resolved, specifically owned files."""
    check_vault(paths.vault, paths.main)
    files, directories = [], []
    disable_plugin = False
    marker = paths.plugin_dest / ".evowiki-install.json"
    if _owned_marker(marker, paths.main):
        owned = json.loads(marker.read_text(encoding="utf-8")).get("files", {})
        if isinstance(owned, dict) and all(not (paths.plugin_dest / f).exists() or digest(paths.plugin_dest / f) == owned.get(f) for f in PLUGIN_FILES):
            files.extend(p for f in PLUGIN_FILES if (p := paths.plugin_dest / f).is_file())
            files.append(marker)
            enabled_file, plugins = enabled_plugins(paths)
            disable_plugin = "evowiki" in plugins
            if disable_plugin:
                print(cli_format('修改：{p0}（仅从已启用插件中移除 evowiki，其余插件保留）', p0=enabled_file.resolve()))
        else:
            print(cli_format('插件文件已被外部替换，保留插件文件、安装标记和启用状态：{p0}', p0=paths.plugin_dest))
    elif paths.plugin_dest.exists():
        print(cli_format('插件缺少本实例安装记录，保留：{p0}', p0=paths.plugin_dest))
    files.extend(p for name in ("backend.cmd", "supervisor.cmd") if (p := paths.run / name).exists())
    if args.remove_runtime:
        directories.extend(p for p in (paths.main / ".venv", paths.plugin_src / "node_modules") if p.exists())
    if args.delete_data:
        if not args.confirm_delete_root or not _same_path(args.confirm_delete_root, paths.main):
            raise Refused("删除数据必须同时提供 --confirm-delete-root 本实例的绝对仓库路径；先执行不带 --apply 的预览")
        if dev_db.service_info(paths.data_dir) is not None:
            raise Refused("数据库由 Windows 服务管理，可能被共用；拒绝删除集群，请由服务管理员单独备份和处理")
        if paths.data_dir.exists():
            if not _owned_marker(paths.data_dir / ".evowiki-owner.json", paths.main):
                raise Refused("数据库缺少本实例创建记录，拒绝删除")
            info = dev_db._info(paths.data_dir)
            if info is not None and info.is_running():
                raise Refused("数据库仍在运行；先执行 down --db，再重新预览卸载")
            directories.append(paths.data_dir)
        default_vault = paths.main / ".evowiki-vault"
        if default_vault.exists():
            if not _owned_marker(default_vault / ".evowiki-owner.json", paths.main):
                raise Refused("默认笔记库缺少本实例创建记录，拒绝删除")
            directories.append(default_vault)
        if paths.env.exists():
            files.append(paths.env)
    for path in files:
        # The only writable area outside the checkout is this vault's own plugin.
        boundary = paths.vault if paths.plugin_dest in path.parents else paths.main
        _bounded_path(path, boundary)
    for path in directories:
        _bounded_path(path, paths.main)
    print(cli_format('卸载实例：{p0}', p0=paths.main.resolve()))
    for path in [*files, *directories]:
        print(cli_format('删除：{p0}', p0=path.resolve()))
    print("保留：用户指定笔记库、插件 data.json、备份、最新遗忘账本、共享下载缓存；未选择删除时保留数据库、.env 和默认库")
    _autostart(paths, False)
    if not args.apply:
        print("以上仅预览；确认这些路径后添加 --apply 执行")
        return 0
    cmd_down(paths, argparse.Namespace(db=args.remove_runtime, port=args.port))
    _stop_tray(paths)
    if args.remove_runtime:
        runtime = (paths.main / ".venv").resolve()
        for process in psutil.process_iter(["pid", "exe"]):
            executable = process.info.get("exe")
            if process.pid != os.getpid() and executable and runtime in Path(executable).resolve().parents:
                raise Refused(cli_format('运行环境仍被进程 {p0} 使用；没有删除运行环境，请先从原入口停止后重试卸载', p0=process.pid))
    _autostart(paths, True)
    if disable_plugin:
        set_plugin_enabled(paths, False)
    for path in files:
        path.unlink(missing_ok=True)
    for path in directories:
        if path != paths.main / ".venv":
            shutil.rmtree(_bounded_path(path, paths.main))
        else:
            print(cli_format('运行环境：等待控制命令退出后由独立启动器删除 {p0}', p0=path.resolve()))
    print("卸载完成；重新安装可继续使用保留的数据")
    return 0


def cmd_status(paths: Paths, args) -> int:
    state = load_state(paths.state)
    info = dev_db._info(paths.data_dir)
    db_up = info is not None and info.is_running()
    svc = dev_db.service_info(paths.data_dir)
    how = (cli_format('（Windows 服务 {p0}，{p1}）', p0=svc['name'], p1=svc['status']) if svc is not None else
           "（本脚本启动）" if db_up and owned_process(state.get("db")) else "")
    print(cli_format('数据库：{p0}{p1}', p0=cli_format('运行中 127.0.0.1:{p0}', p0=info.port) if db_up else '没在运行', p1=how))
    print(cli_format('仓库：{p0}；配置：{p1}；数据库：{p2}', p0=paths.root.resolve(), p1=paths.env.resolve(), p2=paths.data_dir.resolve()))
    print(cli_format('笔记库：{p0}；运行日志：{p1}；迁移备份：{p2}', p0=paths.vault.resolve(), p1=paths.run.resolve(), p2=paths.backups.resolve()))
    b = state.get("backend") or {}
    if backend_proc(state) is not None:
        print(cli_format('后端：运行中 http://127.0.0.1:{p0}（本脚本启动，pid {p1}，代码 {p2}）', p0=b['port'], p1=b['pid'], p2=b.get('head')))
    elif is_evowiki(args.port):
        print(cli_format('后端：运行中 http://127.0.0.1:{p0}（不是本脚本启动的）', p0=args.port))
    else:
        print("后端：没在运行")
    health = backend_identity(b.get("port", args.port))
    if health:
        print(cli_format('后端构建：{p0}；实例：{p1}；排空：{p2}；进行中：{p3}', p0=health.get('commit') or '未验证', p1=health.get('instance_root') or '未验证', p2=health.get('drained'), p3=health.get('in_flight', '未知')))
        try:
            code, _raw = _http("GET", f"http://127.0.0.1:{b.get('port', args.port)}/health/ready")
            print(cli_format('核心就绪：{p0}', p0='通过' if code == 200 else '未通过；检查后端日志'))
        except OSError:
            print("核心就绪：未验证（连接失败）")
    dest = paths.plugin_dest
    same = all(digest(paths.plugin_src / f) == digest(dest / f) for f in PLUGIN_FILES)
    print(cli_format('插件：{p0} {p1}{p2}', p0=dest, p1='与构建产物一致' if same else '与构建产物不同（up 会复制）', p2='，源码比 main.js 新（up 会重建）' if needs_build(paths.plugin_src) else ''))
    if b.get("from") == "live" or "supervisor" in state:
        root = Path(b.get("root") or LIVE_ROOT)
        r = _git(root, "rev-list", "--count", "master..HEAD", check=False)
        ahead = r.stdout.strip() if r.returncode == 0 else "?"
        print(cli_format('代码来源：live（{p0} @ {p1}，比 master 多 {p2} 个自动改动）', p0=LIVE_BRANCH, p1=git_head(root), p2=ahead))
        q = queued_count(paths)
        pause = state.get("live_paused")
        print(cli_format('自动改动：排队 {p0}，{p1}', p0=q if q is not None else '未知', p1=cli_format('暂停 {p0}', p0=pause) if pause else '没有暂停'))
        print(cli_format('监督进程：{p0}', p0='运行中' if owned_process(state.get('supervisor')) else '没在运行'))
    return 0


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            s.reconfigure(errors="replace")
    args = parse_launcher_args(argv)
    main_root = dev_db.main_checkout()
    configured_vault = read_env(main_root / ".env").get("EVOWIKI_VAULT")
    vault = getattr(args, "vault", None) or configured_vault or main_root / ".evowiki-vault"
    default_root = LIVE_ROOT if args.cmd == "supervise" else main_root
    vault = Path(vault)
    vault = vault if vault.is_absolute() else main_root / vault
    paths = Paths(main_root, (args.root or default_root).resolve(), vault)
    try:
        if args.cmd in ("up", "preflight") and args.from_ == "live":
            paths = prepare_live(paths, args)
        elif args.cmd in ("up", "preflight") and args.resume:
            raise Refused("--resume 只能和 --from live 一起用")
        if args.cmd == "supervise":
            if live_worktree_branch(paths.main, paths.root) != LIVE_BRANCH:
                raise Refused(cli_format('{p0} 不是 {p1} 的工作树', p0=paths.root, p1=LIVE_BRANCH))
            rc = supervise(paths, args.port)
            if rc == RESTART_CODE and sys.platform != "win32":
                os.execv(sys.executable, [sys.executable, *sys.argv])
            return rc
        return {"up": cmd_up, "preflight": cmd_preflight, "down": cmd_down, "status": cmd_status,
                "uninstall": cmd_uninstall}[args.cmd](paths, args)
    except Refused as exc:
        print(cli_format('失败：{p0}', p0=exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
