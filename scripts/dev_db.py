"""Start (or reuse) a persistent local Postgres+pgvector for development and print its URL.

  uv run python scripts/dev_db.py .pgdata [--port N]

The dev server's port has one source: the port of EVOWIKI_DATABASE_URL in the main checkout's .env
(`configured_port`; only host:port is read), else DEFAULT_PORT (15432). pixeltable-pgserver picks a
random free port on Windows at every cold start; this script pins it.

DEFAULT_PORT sits above the dynamic port range on this machine (`netsh int ipv4 show dynamicport tcp`:
1024-15000) and below Windows' default one (49152-65535). WinNAT / Hyper-V reserve 100-port blocks
inside the dynamic range at every boot (`netsh interface ipv4 show excludedportrange protocol=tcp`);
a server pinned to a reserved port fails with `could not bind ... Permission denied`, and the exit
message then names the reserved range and the fix (`bind_denied_hint`).

It always runs the MAIN checkout's Postgres binaries (<main repo>/.venv/.../pixeltable_pgserver),
even when called from a git worktree: a server started from a worktree's .venv would run that
worktree's postgres.exe, and removing the worktree would break the dev DB.

On Windows an initialised but stopped data dir is started through WMI `Win32_Process.Create`
(`start_detached`), so the postmaster is not in the caller's process tree: the Claude app (and any
parent job object) kills its whole tree on exit or auto-update, which used to take the dev DB down
with it. The server log is `<data_dir>.log`, outside the data dir (an open log inside it made crash
recovery's fsync hit sharing violations). Startup waits up to --start-timeout seconds (crash recovery).
A first-time init runs initdb directly (with that runtime on PATH); its first server starts detached like any other.

A server already running on the data dir is reused as is; a warning says so if it runs on another
port or from other binaries (stop it with `pg_ctl -D <data_dir> stop` and run this again).

Windows service (preferred, reuse audit B8): when the service EvoWikiPG (installed once by an admin
with scripts/install_pg_service.ps1) serves this data dir, it is used instead of the WMI path: a
stopped service is started with `sc start` (the install script grants the installing user start /
stop rights on it, so no admin is needed day to day) and its port must equal the .env port. The
service runs the stable copy of the binaries in <main>/.pg18 (outside .venv, libwinpthread-1.dll
next to postgres.exe) and logs through Postgres' logging_collector into <main>/.pglog (one file per
weekday, so at most 7). Without the service the WMI path is used unchanged; its `<data_dir>.log` is
rotated by size before each start (`rotate_log`).

Put the printed line into .env (gitignored). Dev-only: uses the dev dependency
pixeltable-pgserver, which nothing under src/evowiki imports. Production uses any
Postgres >= 16 with the pgvector extension."""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pixeltable_pgserver
from envfile import read_env_file
from pixeltable_pgserver import postgres_server

DEFAULT_PORT = 15432                # = DEV_PORT in src/evowiki/forge/patch/testdb.py and tests/pg.py
LOCAL_HOSTS = ("127.0.0.1", "localhost")
START_TIMEOUT = 180
HERE = Path(__file__).resolve().parent
SERVICE_NAME = "EvoWikiPG"          # = scripts/install_pg_service.ps1 -ServiceName
LOG_MAX_BYTES = 20 * 1024 * 1024
LOG_KEEP = 5
PG_UTF8_MANIFEST = b'''<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
 <assemblyIdentity type="win32" name="EvoWiki.PostgreSQL" version="1.0.0.0" processorArchitecture="*"/>
 <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
  <activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8</activeCodePage>
 </windowsSettings></application>
</assembly>
'''


def prepare_windows_bins(bin_dir: Path) -> None:
    """Declare UTF-8 for the bundled native processes, without changing Windows' locale.

    https://learn.microsoft.com/windows/apps/design/globalizing/use-utf8-code-page
    Refresh the executable timestamp when adding a manifest: Windows otherwise retains
    its cached activation context from a previous launch without that manifest.
    """
    if sys.platform != "win32":
        return
    for executable in bin_dir.glob("*.exe"):
        manifest = executable.with_name(executable.name + ".manifest")
        created = not manifest.exists()
        if not created:
            if manifest.read_bytes() != PG_UTF8_MANIFEST:
                raise RuntimeError(f"PostgreSQL 的应用清单不是本实例管理的版本：{manifest}")
        else:
            manifest.write_bytes(PG_UTF8_MANIFEST)
        if created or executable.stat().st_mtime_ns < manifest.stat().st_mtime_ns:
            executable.touch()


def main_checkout(start: Path = HERE) -> Path:
    """The main working tree of this repo (the parent of the common .git dir), also from a worktree."""
    out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                         cwd=start, capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
    return Path(out).parent


def main_bin_paths(main: Path, versions: dict[int, Path], prefix: Path = Path(sys.prefix)) -> dict[int, Path]:
    """pgserver's per-version bin dirs moved from this interpreter's venv to <main>/.venv.
    Fails closed when the main checkout has no such binaries (run `uv sync` there first)."""
    out = {}
    for version, path in versions.items():
        moved = main / ".venv" / Path(path).resolve().relative_to(prefix.resolve())
        out[version] = moved
    return out


def preferred_bin_dir(main: Path | None = None) -> Path:
    """Binaries for long-lived servers other than the dev one (the test cluster, clean_test_pg.py):
    the main checkout's stable copy <main>/.pg18/bin (install_pg_service.ps1) when present, else the
    main checkout's .venv, else this interpreter's."""
    exe = ".exe" if sys.platform == "win32" else ""
    own = Path(postgres_server.POSTGRES_VERSIONS[postgres_server.TARGET_POSTGRES_VERSION])
    try:
        main = main or main_checkout()
    except (OSError, subprocess.CalledProcessError):
        return own
    stable = main / ".pg18" / "bin"
    if (stable / f"pg_ctl{exe}").exists():
        return stable
    moved = main_bin_paths(main, {0: own})[0]
    return moved if (moved / f"pg_ctl{exe}").exists() else own


def read_env(path: Path) -> dict[str, str]:
    return read_env_file(path, missing_ok=True)


def configured_port(env_path: Path) -> int:
    """The dev server's port: EVOWIKI_DATABASE_URL's port in `env_path`, else DEFAULT_PORT. Only the
    host and port are read (never shown with the rest of the URL). ValueError for a URL that names no
    port, an invalid one or another host: this script only runs a local server."""
    url = read_env(env_path).get("EVOWIKI_DATABASE_URL", "")
    if not url:
        return DEFAULT_PORT
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"{env_path} 的 EVOWIKI_DATABASE_URL 端口不是有效数字") from None
    if parts.hostname not in LOCAL_HOSTS:
        raise ValueError(f"{env_path} 的 EVOWIKI_DATABASE_URL 主机是 {parts.hostname}，开发库只在 127.0.0.1")
    if port is None:
        raise ValueError(f"{env_path} 的 EVOWIKI_DATABASE_URL 没写端口（开发库默认 {DEFAULT_PORT}）")
    return port


def excluded_ranges(text: str | None = None) -> list[tuple[int, int]]:
    """Windows' reserved TCP port ranges (`netsh interface ipv4 show excludedportrange protocol=tcp`);
    [] when netsh is unavailable."""
    if text is None:
        try:
            text = subprocess.run(["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
                                  capture_output=True, text=True, errors="replace", check=False).stdout
        except OSError:
            return []
    return [(int(m[1]), int(m[2])) for m in re.finditer(r"^\s*(\d+)\s+(\d+)", text or "", re.MULTILINE)]


def bind_denied_hint(log: str, port: int, env_path: Path, ranges: list[tuple[int, int]] | None = None) -> str | None:
    """A Chinese explanation when the server log shows Windows refusing the port bind, else None."""
    if not re.search(r"could not bind .*Permission denied", log):
        return None
    hit = [r for r in (excluded_ranges() if ranges is None else ranges) if r[0] <= port <= r[1]]
    where = (f"Windows 的 WinNAT / Hyper-V 保留了 {hit[0][0]}–{hit[0][1]}（这些范围每次开机都会变）" if hit else
             "多半被 Windows 保留了（`netsh interface ipv4 show excludedportrange protocol=tcp` 查看保留范围）")
    return (f"开发库端口 {port} 绑定被拒（Permission denied）：{where}。\n解决（二选一）：\n"
            f"  1. 把 {env_path} 里 EVOWIKI_DATABASE_URL 的端口改成不在动态端口范围里的端口"
            f"（默认 {DEFAULT_PORT}），再运行一次；\n"
            "  2. 以管理员身份运行 `net stop winnat`，再 `net start winnat`，然后再运行一次。")


def log_path(data_dir: Path) -> Path:
    """The server log: a sibling of the data dir, never inside it."""
    return data_dir.with_name(data_dir.name + ".log")


def dll_dirs() -> list[str]:
    """Only the official Git distribution's runtime enters the database child process PATH."""
    from git_runtime import git_runtime_dir

    if sys.platform != "win32":
        return []
    runtime = git_runtime_dir()
    if runtime is None:
        raise RuntimeError("PostgreSQL 缺少 Git for Windows 运行库；请重新安装 README 链接的完整官方 Git，"
                           "重新打开终端并运行 doctor。")
    return [str(runtime)]


def pg_ctl_start_cmdline(pg_ctl: Path, data_dir: Path, port: int, timeout: int, extra_path: list[str] | None = None) -> str:
    args = [str(pg_ctl), "start", "-w", "-t", str(timeout), "-D", str(data_dir), "-l", str(log_path(data_dir)),
            "-o", f"-h 127.0.0.1 -p {port}"]
    line = subprocess.list2cmdline(args)
    if extra_path:
        line = f'set "PATH={os.pathsep.join(extra_path)};%PATH%" && {line}'
    return f'cmd.exe /d /s /c "{line}"'


def start_detached(cmdline: str, cwd: Path) -> tuple[int, int]:
    """Run `cmdline` via WMI Win32_Process.Create (parent WmiPrvSE, outside the caller's job object).
    Returns (ReturnValue, ProcessId); ReturnValue 0 means the process was created."""
    import pythoncom
    import win32com.client

    # Use the existing Windows COM dependency directly; no intermediate script host.
    pythoncom.CoInitialize()
    service = startup = process = arguments = result = None
    try:
        service = win32com.client.GetObject("winmgmts:root\\cimv2")
        startup = service.Get("Win32_ProcessStartup").SpawnInstance_()
        startup.Properties_.Item("ShowWindow").Value = 0
        process = service.Get("Win32_Process")
        arguments = process.Methods_.Item("Create").InParameters.SpawnInstance_()
        arguments.Properties_.Item("CommandLine").Value = cmdline
        arguments.Properties_.Item("CurrentDirectory").Value = str(cwd)
        arguments.Properties_.Item("ProcessStartupInformation").Value = startup
        result = process.ExecMethod_("Create", arguments)
        return int(result.Properties_.Item("ReturnValue").Value), int(result.Properties_.Item("ProcessId").Value or 0)
    finally:
        result = arguments = process = startup = service = None
        pythoncom.CoUninitialize()


def _info(data_dir: Path):
    try:
        return postgres_server.PostmasterInfo.read_from_pgdata(data_dir)
    except (AssertionError, ValueError, OSError):    # postmaster.pid half-written during startup
        return None


def _log_tail(data_dir: Path, n: int = 30) -> str:
    try:
        return "\n".join(log_path(data_dir).read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log)"


def log_size(data_dir: Path) -> int:
    try:
        return log_path(data_dir).stat().st_size
    except OSError:
        return 0


def log_since(data_dir: Path, offset: int) -> str:
    """What the server log gained after `offset` (the log is appended to across starts)."""
    try:
        with log_path(data_dir).open("rb") as f:
            f.seek(offset)
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def _fail(data_dir: Path, port: int, offset: int, env_path: Path, head: str):
    hint = bind_denied_hint(log_since(data_dir, offset), port, env_path)
    sys.exit((hint + "\n\n" if hint else "") + f"{head}; log {log_path(data_dir)}:\n{_log_tail(data_dir)}")


def ensure_detached(data_dir: Path, pg_ctl: Path, port: int, timeout: int = START_TIMEOUT,
                    env_path: Path = Path(".env")) -> None:
    """Windows: start an initialised, stopped data dir outside the caller's process tree and wait
    until it is ready. Exits non-zero with the log tail on failure, led by `bind_denied_hint` when
    Windows refused the port. A running server is left alone."""
    if not (data_dir / "PG_VERSION").exists():
        return
    info = _info(data_dir)
    if info is not None and info.is_running():
        return
    rotate_log(log_path(data_dir))
    offset = log_size(data_dir)
    rv, _pid = start_detached(pg_ctl_start_cmdline(pg_ctl, data_dir, port, timeout, dll_dirs()), data_dir.parent)
    if rv != 0:
        _fail(data_dir, port, offset, env_path, f"could not start pg_ctl via WMI (ReturnValue {rv})")
    deadline = time.monotonic() + timeout
    while True:
        info = _info(data_dir)
        if info is not None and info.is_running() and info.status == "ready":
            return
        if time.monotonic() >= deadline:
            _fail(data_dir, port, offset, env_path, f"Postgres on {data_dir} not ready after {timeout}s")
        time.sleep(1)


def rotate_log(path: Path, max_bytes: int = LOG_MAX_BYTES, keep: int = LOG_KEEP) -> bool:
    """Size-based rotation of a log some process appends to, run before that process starts:
    path -> path.1 -> ... -> path.<keep>, the oldest dropped. True when it rotated. A file still held
    open by a running process cannot be renamed on Windows: it is left alone with one warning."""
    try:
        if path.stat().st_size < max_bytes:
            return False
    except OSError:
        return False
    try:
        path.with_name(f"{path.name}.{keep}").unlink(missing_ok=True)
        for i in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            if src.exists():
                src.replace(path.with_name(f"{path.name}.{i + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError as e:
        print(f"注意：日志 {path} 没能轮转（{type(e).__name__}），继续追加", file=sys.stderr)
        return False
    return True


# ---------- Windows service (scripts/install_pg_service.ps1) ----------

def _same_dir(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def parse_service_binpath(binpath: str) -> tuple[Path | None, int | None]:
    """(data dir, port) of a `pg_ctl register` ImagePath, e.g.
    "<checkout>\\.pg18\\bin\\pg_ctl.exe" runservice -N "EvoWikiPG" -D "<checkout>\\.pgdata" -w -o "-h 127.0.0.1 -p 15432"."""
    d = re.search(r'\s-D\s+(?:"([^"]+)"|(\S+))', binpath)
    p = re.search(r"\s-p\s+(\d+)", binpath)
    return (Path(d[1] or d[2]) if d else None), (int(p[1]) if p else None)


def service_info(data_dir: Path, name: str = SERVICE_NAME) -> dict | None:
    """The Windows service `name` when it serves `data_dir` ({name, status, port, binpath}), else None:
    no such service, not Windows, or a service for another data dir (a test's tmp dir, say)."""
    if sys.platform != "win32":
        return None
    import psutil
    try:
        d = psutil.win_service_get(name).as_dict()
    except (psutil.Error, OSError):
        return None
    sdir, port = parse_service_binpath(d.get("binpath") or "")
    if sdir is None or not _same_dir(sdir, data_dir):
        return None
    return {"name": name, "status": d.get("status"), "port": port, "binpath": d.get("binpath")}


SC_HINTS = {5: "没有启停这个服务的权限：以管理员身份重新运行 scripts\\install_pg_service.ps1（它会授予你启停权限），"
               "或在管理员 PowerShell 里执行 {verb}-Service {name}",
            1058: "服务 {name} 被禁用了：在管理员 PowerShell 里执行 Set-Service {name} -StartupType Automatic",
            1060: "没有服务 {name}"}
SC_OK = {"start": (0, 1056), "stop": (0, 1062)}     # 1056 already running, 1062 not started


def _sc(verb: str, name: str) -> None:
    r = subprocess.run(["sc.exe", verb, name], capture_output=True, text=True, errors="replace", check=False)
    if r.returncode in SC_OK[verb]:
        return
    hint = SC_HINTS.get(r.returncode, "").format(name=name, verb=verb.capitalize())
    raise RuntimeError(f"sc {verb} {name} 失败（错误 {r.returncode}）："
                       + (hint or (r.stdout or r.stderr).strip()[-300:]))


def wait_ready(data_dir: Path, timeout: int, port: int | None = None) -> bool:
    """Wait until postmaster.pid shows a ready server (on `port` when given)."""
    deadline = time.monotonic() + timeout
    while True:
        info = _info(data_dir)
        if info is not None and info.is_running() and info.status == "ready" and port in (None, info.port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def ensure_service(data_dir: Path, port: int, timeout: int = START_TIMEOUT, name: str = SERVICE_NAME) -> str:
    """Start the service when it is stopped and wait until Postgres is ready on `port`: "running" or
    "started"; RuntimeError (Chinese, with the fix) otherwise. The service's port must equal `port`
    (the .env port): a mismatch is refused, never silently used."""
    svc = service_info(data_dir, name)
    if svc is None:
        raise RuntimeError(f"没有为 {data_dir} 注册的服务 {name}")
    if svc["port"] != port:
        raise RuntimeError(f"服务 {name} 用端口 {svc['port']}，.env 里是 {port}：把 .env 里 EVOWIKI_DATABASE_URL "
                           f"的端口改成 {svc['port']}，或重新运行 scripts\\install_pg_service.ps1")
    how = "running"
    if svc["status"] != "running":
        _sc("start", name)
        how = "started"
    if not wait_ready(data_dir, timeout, port):
        raise RuntimeError(f"服务 {name} 已启动，但 {timeout}s 内 Postgres 没就绪；看 {data_dir.parent / '.pglog'} "
                           "里的日志和 Windows 事件查看器（Windows 日志 → 应用程序，来源 PostgreSQL）")
    return how


def stop_service(name: str = SERVICE_NAME) -> None:
    _sc("stop", name)


def pinned_server(data_dir: Path, port: int = DEFAULT_PORT,
                  bin_paths: dict[int, Path] | None = None) -> "pixeltable_pgserver.PostgresServer":
    """get_server with pgserver's port choice (`find_suitable_port`, a random free port) pinned to
    `port` and, when given, its binaries taken from `bin_paths`, for this call only. A busy port makes
    pg_ctl fail and pgserver raise (no fallback port)."""
    original_port = postgres_server.find_suitable_port
    original_bins = dict(postgres_server.POSTGRES_VERSIONS)
    postgres_server.find_suitable_port = lambda address=None: port
    if bin_paths is not None:
        postgres_server.POSTGRES_VERSIONS.update(bin_paths)
    try:
        return pixeltable_pgserver.get_server(data_dir, cleanup_mode=None)
    finally:
        postgres_server.find_suitable_port = original_port
        postgres_server.POSTGRES_VERSIONS.clear()
        postgres_server.POSTGRES_VERSIONS.update(original_bins)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--port", type=int, help=f"default: the port in the main checkout's .env, else {DEFAULT_PORT}")
    ap.add_argument("--start-timeout", type=int, default=START_TIMEOUT)
    args = ap.parse_args(argv)
    main = main_checkout()
    env_path = main / ".env"
    if args.port is None:
        try:
            args.port = configured_port(env_path)
        except ValueError as e:
            sys.exit(str(e))
    bins = main_bin_paths(main, dict(postgres_server.POSTGRES_VERSIONS))
    want = bins[postgres_server.TARGET_POSTGRES_VERSION]
    if not (want / "pg_ctl.exe").exists() and not (want / "pg_ctl").exists():
        sys.exit(f"no Postgres binaries at {want}: run `uv sync` in the main checkout first")
    if service_info(args.data_dir) is not None:
        try:
            how = ensure_service(args.data_dir, args.port, args.start_timeout)
        except RuntimeError as e:
            sys.exit(str(e))
        print(f"note: Windows service {SERVICE_NAME} {how} on port {args.port}", file=sys.stderr)
        print(f"EVOWIKI_DATABASE_URL=postgresql://postgres:@127.0.0.1:{args.port}/postgres")
        return
    fresh = not (args.data_dir / "PG_VERSION").exists()
    if sys.platform == "win32":
        prepare_windows_bins(want)
        pg_ctl = want / "pg_ctl.exe"
        if fresh:
            r = subprocess.run([str(want / "initdb.exe"), "--auth=trust", "--auth-local=trust", "--encoding=utf8",
                                "-U", "postgres", "-D", str(args.data_dir)], capture_output=True, text=True, errors="replace",
                               env={**os.environ, "PATH": os.pathsep.join([*dll_dirs(), os.environ.get("PATH", "")])},
                               check=False)
            if r.returncode != 0:
                sys.exit("initdb failed:\n" + r.stderr)
        ensure_detached(args.data_dir, pg_ctl, args.port, args.start_timeout, env_path)
    srv = pinned_server(args.data_dir, args.port, bins)
    info = srv.get_postmaster_info()
    if info.port != args.port:
        print(f"warning: reused a server already running on port {info.port}, not {args.port}; "
              "stop it (pg_ctl -D <data_dir> stop) and run this again", file=sys.stderr)
    exe = Path(info.process.exe()).resolve() if info.process is not None else None
    if exe is not None and exe.parent != want.resolve():
        print(f"warning: reused a server running {exe}, not the main checkout's binaries in {want}; "
              "stop it (pg_ctl -D <data_dir> stop) and run this again", file=sys.stderr)
    print(f"EVOWIKI_DATABASE_URL={srv.get_uri()}")


if __name__ == "__main__":
    main()
