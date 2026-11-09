"""Find the per-run test clusters killed pytest runs left behind (%TEMP%\\evowiki-pg-*) and, with
--apply, stop and remove the ones nobody uses.

  uv run python scripts/clean_test_pg.py [--dir DIR ...] [--apply]

Dry run by default: prints one line per cluster (remove / keep and why) and changes nothing.
Before tests/pg.py moved to one long-lived test cluster (reuse audit B8), every pytest run made a
pgserver cluster in %TEMP%\\evowiki-pg-XXXX and removed it at exit; a killed run left it, often
with its postmaster still running. A cluster is removed only when ALL of these hold:
  - no pid in its .handle_pids.json (pgserver's list of the processes using it: the pytest runs)
    is still that run: a live Python / uv process started before the dir counts as alive; a pid
    now held by another program, or by a process started after the dir, is a reused pid;
  - its postmaster is not running, or it runs and we can connect and see no client connection
    other than our own;
  - a dir that never finished initdb (no PG_VERSION) is also older than an hour.
Anything uncertain is kept: an unreadable pid list, an owner process we may not inspect, a running
postmaster we cannot connect to, a postmaster.pid naming another dir. Stopping is `pg_ctl stop -m
fast`, then the dir is deleted. Only `evowiki-pg-*` entries directly inside the given dirs are
looked at: never the long-lived test cluster, never the dev data dir.

Postmasters whose `-D` is an evowiki-pg-* dir that no longer exists are listed too; with --apply
they are terminated when they accept a connection and have no client."""

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dev_db

PREFIX = "evowiki-pg-"
HALF_INIT_AGE_S = 3600


@dataclass
class Verdict:
    path: Path
    remove: bool
    why: str
    postmaster: int | None = None     # pid of a running postmaster to stop first
    port: int | None = None


def _alive(pid: int, born: float) -> bool:
    """Whether owner `pid` may still be the run that made a cluster dir created at `born`: a live
    Python process started before the dir. Another program on that pid, or one started after the
    dir, is a reused pid. Unreadable → alive (kept)."""
    try:
        p = psutil.Process(pid)
        name = p.name().lower()
        return name.startswith(("python", "pytest", "uv")) and p.create_time() <= born + 5
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True


def read_postmaster(data: Path) -> tuple[int, Path, int | None] | None:
    """(pid, data dir, port) from postmaster.pid; None when absent; ValueError when unreadable."""
    f = data / "postmaster.pid"
    if not f.exists():
        return None
    lines = f.read_text(errors="replace").splitlines()
    if len(lines) < 4:
        raise ValueError("postmaster.pid is incomplete")
    return int(lines[0]), Path(lines[1].strip()), int(lines[3]) if lines[3].strip() else None


def is_postgres(pid: int) -> bool:
    try:
        return psutil.Process(pid).name().lower().startswith("postgres")
    except psutil.Error:
        return False


def client_count(port: int) -> int | None:
    """Client connections other than ours; None when we cannot ask (kept, to be safe)."""
    import psycopg
    try:
        with psycopg.connect(f"postgresql://postgres@127.0.0.1:{port}/postgres", connect_timeout=3) as conn:
            return conn.execute("select count(*) from pg_stat_activity where backend_type = 'client backend' "
                                "and pid <> pg_backend_pid()").fetchone()[0]
    except Exception:  # noqa: BLE001 - any failure means "unknown": keep it
        return None


def judge(path: Path, now: float, *, alive=_alive, postgres=is_postgres, clients=client_count) -> Verdict:
    owners_file = path / ".handle_pids.json"
    try:
        owners = json.loads(owners_file.read_text()) if owners_file.exists() else []
        owners = [int(p) for p in owners]
        born = path.stat().st_ctime         # Windows: creation time
    except (OSError, ValueError, TypeError):
        return Verdict(path, False, "读不了 .handle_pids.json")
    live = [p for p in owners if alive(p, born)]
    if live:
        return Verdict(path, False, f"使用它的进程还在：{', '.join(map(str, live))}")
    try:
        pm = read_postmaster(path)
    except (OSError, ValueError) as e:
        return Verdict(path, False, f"读不了 postmaster.pid（{e}）")
    if pm is not None and postgres(pm[0]):
        pid, pm_dir, port = pm
        if not dev_db._same_dir(pm_dir, path):
            return Verdict(path, False, f"postmaster.pid 指向别的目录 {pm_dir}")
        if port is None:
            return Verdict(path, False, f"postmaster {pid} 在运行，但不知道端口")
        n = clients(port)
        if n is None:
            return Verdict(path, False, f"postmaster {pid} 在运行（端口 {port}），但连不上，无法确认没人用")
        if n:
            return Verdict(path, False, f"postmaster {pid}（端口 {port}）还有 {n} 个连接")
        return Verdict(path, True, f"所有者已退出；postmaster {pid}（端口 {port}）没有连接，先停再删", pid, port)
    if not (path / "PG_VERSION").exists():
        try:
            age = now - path.stat().st_mtime
        except OSError:
            return Verdict(path, False, "读不了目录")
        if age < HALF_INIT_AGE_S:
            return Verdict(path, False, "initdb 没完成，但不到 1 小时（可能正在初始化）")
        return Verdict(path, True, "initdb 没完成的残留，所有者已退出")
    return Verdict(path, True, "所有者已退出，postmaster 没在运行")


def find_clusters(dirs: list[Path]) -> list[Path]:
    out = []
    for d in dirs:
        try:
            out += sorted(p for p in d.iterdir() if p.name.startswith(PREFIX) and p.is_dir() and not p.is_symlink())
        except OSError:
            continue
    return out


def orphan_postmasters(dirs: list[Path]) -> list[tuple[psutil.Process, Path, int | None]]:
    """Postmasters (postgres.exe whose parent is not postgres) serving an evowiki-pg-* dir under
    `dirs` that no longer holds a data dir."""
    out = []
    for p in psutil.process_iter(["name", "cmdline", "ppid"]):
        try:
            if not (p.info["name"] or "").lower().startswith("postgres"):
                continue
            if is_postgres(p.info["ppid"] or 0):
                continue                        # a backend of some postmaster, not a postmaster
            cmd = p.info["cmdline"] or []
        except psutil.Error:
            continue
        data = next((Path(cmd[i + 1]) for i, a in enumerate(cmd[:-1]) if a == "-D"), None)
        if data is None or not data.name.startswith(PREFIX):
            continue
        if not any(dev_db._same_dir(data.parent, d) for d in dirs) or (data / "PG_VERSION").exists():
            continue
        port = next((int(cmd[i + 1]) for i, a in enumerate(cmd[:-1]) if a == "-p" and cmd[i + 1].isdigit()), None)
        out.append((p, data, port))
    return out


def stop(v: Verdict, pg_ctl: Path) -> str | None:
    """Stop (if running) and delete; the error text, or None."""
    if v.postmaster is not None:
        r = subprocess.run([str(pg_ctl), "stop", "-m", "fast", "-w", "-t", "60", "-D", str(v.path)],
                           capture_output=True, text=True, errors="replace", check=False)
        if r.returncode != 0:
            return f"pg_ctl stop 失败：{(r.stderr or r.stdout).strip()[-300:]}"
    for _ in range(10):
        try:
            shutil.rmtree(v.path)
            return None
        except FileNotFoundError:
            return None
        except OSError as e:
            err = e
            time.sleep(0.5)
    return f"删除失败：{err}"


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            s.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, action="append", help="where to look (default: the temp dir)")
    ap.add_argument("--apply", action="store_true", help="stop and remove; without it nothing changes")
    args = ap.parse_args(argv)
    dirs = args.dir or [Path(tempfile.gettempdir())]
    clusters = find_clusters(dirs)
    now = time.time()
    verdicts = [judge(p, now) for p in clusters]
    pg_ctl = dev_db.preferred_bin_dir() / ("pg_ctl.exe" if sys.platform == "win32" else "pg_ctl")
    print(f"{'清理' if args.apply else '试运行（不改动；--apply 才执行）'}：{', '.join(map(str, dirs))} 下 "
          f"{len(clusters)} 个 {PREFIX}* 集群")
    removed = failed = 0
    for v in verdicts:
        tag = "删除" if v.remove else "保留"
        line = f"  [{tag}] {v.path.name}  {v.why}"
        if args.apply and v.remove:
            err = stop(v, pg_ctl)
            if err:
                failed += 1
                line += f"  -> {err}"
            else:
                removed += 1
                line += "  -> 已删除"
        print(line)
    orphans = orphan_postmasters(dirs)
    for p, data, port in orphans:
        n = client_count(port) if port else None
        ok = n == 0
        line = (f"  [{'终止' if ok else '保留'}] 孤儿 postmaster {p.pid}（{data.name} 已不存在，端口 {port}）"
                f"{'' if ok else '：连不上或还有连接'}")
        if args.apply and ok:
            try:
                for c in p.children(recursive=True):
                    c.kill()
                p.kill()
                line += "  -> 已终止"
            except psutil.Error as e:
                line += f"  -> 终止失败：{e}"
        print(line)
    want = sum(v.remove for v in verdicts)
    total = sum(1 for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("postgres"))
    if args.apply:
        print(f"已删除 {removed} 个，失败 {failed} 个，保留 {len(verdicts) - want} 个；"
              f"现在 postgres 进程 {total} 个")
    else:
        print(f"会删除 {want} 个（其中 {sum(v.postmaster is not None for v in verdicts)} 个要先停 postmaster），"
              f"保留 {len(verdicts) - want} 个；孤儿 postmaster {len(orphans)} 个；当前 postgres 进程 {total} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
