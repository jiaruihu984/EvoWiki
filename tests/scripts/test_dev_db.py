"""scripts/dev_db.py pins the dev server's port (pgserver alone picks a random one on Windows)."""

import importlib.util
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pixeltable_pgserver import postgres_server

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev_db.py"


def load():
    spec = importlib.util.spec_from_file_location("dev_db", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_native_utf8_manifests_preserve_vendor_bytes_and_are_idempotent(tmp_path, monkeypatch):
    dev_db = load()
    monkeypatch.setattr(dev_db.sys, "platform", "win32")
    for name in ("initdb.exe", "postgres.exe", "pg_dump.exe", "pg_restore.exe"):
        executable = tmp_path / name
        executable.write_bytes(b"vendor-binary-unchanged")
        os.utime(executable, (1, 1))
    # Packaged executables can have a future build timestamp. Adding the manifest
    # still has to invalidate a prior Windows activation context for that file.
    future = 4_000_000_000
    os.utime(tmp_path / "initdb.exe", (future, future))
    dev_db.prepare_windows_bins(tmp_path)
    before = {path: path.stat().st_mtime_ns for path in tmp_path.iterdir()}
    for executable in tmp_path.glob("*.exe"):
        assert executable.read_bytes() == b"vendor-binary-unchanged"
        assert executable.stat().st_mtime_ns > 1_000_000_000
        assert executable.stat().st_mtime_ns != future * 1_000_000_000
        document = ET.fromstring(executable.with_name(executable.name + ".manifest").read_bytes())
        assert document.find(".//{http://schemas.microsoft.com/SMI/2019/WindowsSettings}activeCodePage").text == "UTF-8"
    dev_db.prepare_windows_bins(tmp_path)
    assert {path: path.stat().st_mtime_ns for path in tmp_path.iterdir()} == before


def test_native_utf8_setup_preserves_an_unrecognized_manifest(tmp_path, monkeypatch):
    dev_db = load()
    monkeypatch.setattr(dev_db.sys, "platform", "win32")
    (tmp_path / "postgres.exe").write_bytes(b"vendor")
    manifest = tmp_path / "postgres.exe.manifest"
    manifest.write_bytes(b"vendor-manifest")
    with pytest.raises(RuntimeError, match="应用清单"):
        dev_db.prepare_windows_bins(tmp_path)
    assert manifest.read_bytes() == b"vendor-manifest"


@pytest.mark.skipif(os.name != "nt", reason="native Windows executable manifest")
def test_real_initdb_supports_paths_outside_the_system_code_page(tmp_path):
    import shutil

    dev_db = load()
    source = Path(postgres_server.POSTGRES_VERSIONS[postgres_server.TARGET_POSTGRES_VERSION]).parent
    runtime = tmp_path / "安装 🧪" / "runtime"
    shutil.copytree(source, runtime)
    dev_db.prepare_windows_bins(runtime / "bin")
    data = tmp_path / "数据库 🧪"
    result = subprocess.run([str(runtime / "bin" / "initdb.exe"), "--encoding=utf8", "--locale=C",
                             "--auth=trust", "-U", "postgres", "-D", str(data)],
                            env={**os.environ, "PATH": os.pathsep.join([*dev_db.dll_dirs(), os.environ.get("PATH", "")])},
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert (data / "PG_VERSION").read_text(encoding="utf-8").strip() == str(postgres_server.TARGET_POSTGRES_VERSION)


def test_checkout_path_is_utf8_even_without_python_utf8_mode(tmp_path):
    checkout = tmp_path / "中文 checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    code = (f"import sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); "
            "import dev_db; print(dev_db.main_checkout(sys.argv[1]))")
    result = subprocess.run([sys.executable, "-c", code, str(checkout)],
                            env={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"},
                            capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(checkout.resolve())


def test_the_port_choice_is_pinned_during_get_server_only(monkeypatch, tmp_path):
    dev_db = load()
    seen = []
    monkeypatch.setattr(dev_db.pixeltable_pgserver, "get_server",
                        lambda data_dir, cleanup_mode: seen.append(
                            (data_dir, cleanup_mode, postgres_server.find_suitable_port("127.0.0.1"))))
    original = postgres_server.find_suitable_port
    dev_db.pinned_server(tmp_path)
    assert seen == [(tmp_path, None, 15432)]                 # never stopped by this script's exit
    assert postgres_server.find_suitable_port is original   # restored afterwards
    dev_db.pinned_server(tmp_path, 9801)
    assert seen[-1][2] == 9801


def test_restored_even_when_get_server_raises(monkeypatch, tmp_path):
    dev_db = load()
    original = postgres_server.find_suitable_port

    def busy(data_dir, cleanup_mode):
        raise RuntimeError("pg_ctl: could not start server")
    monkeypatch.setattr(dev_db.pixeltable_pgserver, "get_server", busy)
    try:
        dev_db.pinned_server(tmp_path)
    except RuntimeError:
        pass
    else:
        raise AssertionError("a failed start must raise")
    assert postgres_server.find_suitable_port is original


def test_binaries_come_from_the_main_checkout_during_get_server_only(monkeypatch, tmp_path):
    dev_db = load()
    seen = []
    monkeypatch.setattr(dev_db.pixeltable_pgserver, "get_server",
                        lambda data_dir, cleanup_mode: seen.append(dict(postgres_server.POSTGRES_VERSIONS)))
    before = dict(postgres_server.POSTGRES_VERSIONS)
    main_bins = {18: tmp_path / "main" / ".venv" / "pg18" / "bin"}
    dev_db.pinned_server(tmp_path, bin_paths=main_bins)
    assert seen[0][18] == main_bins[18]
    assert postgres_server.POSTGRES_VERSIONS == before        # restored afterwards


def test_bin_paths_move_from_this_venv_to_the_main_checkouts(tmp_path):
    dev_db = load()
    venv = tmp_path / "wt" / ".venv"
    got = dev_db.main_bin_paths(tmp_path / "main",
                                {18: venv / "Lib" / "site-packages" / "pixeltable_pgserver" / "pginstall18" / "bin"},
                                prefix=venv)
    assert got == {18: tmp_path / "main" / ".venv" / "Lib" / "site-packages" / "pixeltable_pgserver"
                   / "pginstall18" / "bin"}


def test_the_main_checkout_is_found_from_a_worktree():
    dev_db = load()
    main = dev_db.main_checkout()
    assert (main / ".git").is_dir()                             # the main working tree, never a worktree's .git file


class FakeInfo:
    def __init__(self, running=True, status="ready"):
        self.running, self.status = running, status

    def is_running(self):
        return self.running


def _stopped_dir(tmp_path):
    data = tmp_path / "pg data"
    data.mkdir()
    (data / "PG_VERSION").write_text("18")
    return data


def _clock(monkeypatch, dev_db):
    t = [0.0]
    monkeypatch.setattr(dev_db.time, "monotonic", lambda: t[0])
    monkeypatch.setattr(dev_db.time, "sleep", lambda s: t.__setitem__(0, t[0] + s))


def test_the_log_is_a_sibling_of_the_data_dir():
    dev_db = load()
    assert dev_db.log_path(Path("D:/Example/.pgdata")) == Path("D:/Example/.pgdata.log")


def test_a_stopped_data_dir_is_started_detached_then_waited_for(monkeypatch, tmp_path):
    dev_db = load()
    _clock(monkeypatch, dev_db)
    data = _stopped_dir(tmp_path)
    launched, reads = [], iter([None, FakeInfo(True, "starting"), FakeInfo(True, "ready")])
    monkeypatch.setattr(dev_db, "start_detached", lambda cmd, cwd: launched.append((cmd, cwd)) or (0, 42))
    monkeypatch.setattr(dev_db, "_info", lambda d: next(reads))
    dev_db.ensure_detached(data, Path("C:/pg/bin/pg_ctl.exe"), 9801, timeout=30)
    (cmd, cwd), = launched
    assert cwd == tmp_path
    assert f'-D "{data}"' in cmd and f'-l "{data}.log"' in cmd           # log outside the data dir, paths quoted
    assert "start -w -t 30" in cmd and '"-h 127.0.0.1 -p 9801"' in cmd
    assert cmd.startswith('cmd.exe /d /s /c "')


def test_dll_dirs_and_path_prefix(tmp_path, monkeypatch):
    dev_db = load()
    runtime = tmp_path / "Git runtime"
    monkeypatch.setattr(dev_db.sys, "platform", "win32")
    monkeypatch.setattr("git_runtime.git_runtime_dir", lambda: runtime)
    assert dev_db.dll_dirs() == [str(runtime)]
    cmd = dev_db.pg_ctl_start_cmdline(Path("pg_ctl.exe"), tmp_path / "d", 15432, 5,
                                    dev_db.dll_dirs())
    assert f'set "PATH={runtime};%PATH%" && ' in cmd
    monkeypatch.setattr("git_runtime.git_runtime_dir", lambda: None)
    with pytest.raises(RuntimeError, match="Git for Windows.*doctor"):
        dev_db.dll_dirs()


def test_a_running_server_is_not_relaunched(monkeypatch, tmp_path):
    dev_db = load()
    data = _stopped_dir(tmp_path)
    monkeypatch.setattr(dev_db, "start_detached", lambda *a: (_ for _ in ()).throw(AssertionError("relaunched")))
    monkeypatch.setattr(dev_db, "_info", lambda d: FakeInfo(True))
    dev_db.ensure_detached(data, Path("pg_ctl.exe"), 15432)


def test_a_fresh_data_dir_is_left_to_pgserver(monkeypatch, tmp_path):
    dev_db = load()
    monkeypatch.setattr(dev_db, "start_detached", lambda *a: (_ for _ in ()).throw(AssertionError("launched")))
    dev_db.ensure_detached(tmp_path / "new", Path("pg_ctl.exe"), 15432)


def test_a_failed_launch_fails_closed_with_the_log_tail(monkeypatch, tmp_path):
    dev_db = load()
    data = _stopped_dir(tmp_path)
    (tmp_path / "pg data.log").write_text("boom: could not bind\n")
    monkeypatch.setattr(dev_db, "start_detached", lambda cmd, cwd: (9, 0))
    monkeypatch.setattr(dev_db, "_info", lambda d: None)
    with pytest.raises(SystemExit) as e:
        dev_db.ensure_detached(data, Path("pg_ctl.exe"), 15432)
    assert "ReturnValue 9" in str(e.value) and "could not bind" in str(e.value)


def test_a_readiness_timeout_fails_closed_with_the_log_tail(monkeypatch, tmp_path):
    dev_db = load()
    _clock(monkeypatch, dev_db)
    data = _stopped_dir(tmp_path)
    (tmp_path / "pg data.log").write_text("recovery in progress\n")
    monkeypatch.setattr(dev_db, "start_detached", lambda cmd, cwd: (0, 1))
    reads = iter([None])
    monkeypatch.setattr(dev_db, "_info", lambda d: next(reads, FakeInfo(True, "starting")))
    with pytest.raises(SystemExit) as e:
        dev_db.ensure_detached(data, Path("pg_ctl.exe"), 15432, timeout=5)
    assert "not ready after 5s" in str(e.value) and "recovery in progress" in str(e.value)


NETSH = """
协议 tcp 端口排除范围

开始端口    结束端口
----------    --------
      5357        5357
      9767        9866
     50000       50059     *

* - 管理的端口排除。
"""
DENIED = 'LOG:  could not bind IPv4 address "127.0.0.1": Permission denied\n'


def test_the_port_comes_from_env_else_the_default(tmp_path):
    dev_db = load()
    env = tmp_path / ".env"
    assert dev_db.configured_port(env) == dev_db.DEFAULT_PORT == 15432          # no .env
    env.write_text('X=1\nEVOWIKI_DATABASE_URL="postgresql://postgres:pw@127.0.0.1:5433/postgres"\n', encoding="utf-8")
    assert dev_db.configured_port(env) == 5433


@pytest.mark.parametrize("url,match", [
    ("postgresql://postgres:secret@db.example:5433/postgres", "主机"),
    ("postgresql://postgres:secret@127.0.0.1/postgres", "没写端口"),
    ("postgresql://postgres:secret@127.0.0.1:port/postgres", "有效数字"),
])
def test_an_unusable_env_url_is_refused_without_the_password(tmp_path, url, match):
    dev_db = load()
    env = tmp_path / ".env"
    env.write_text(f"EVOWIKI_DATABASE_URL={url}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=match) as e:
        dev_db.configured_port(env)
    assert "secret" not in str(e.value)


def test_the_mirrored_dev_ports_agree():
    from evowiki.forge.patch import testdb
    from tests import pg
    assert load().DEFAULT_PORT == testdb.DEV_PORT == pg.DEV_PORT


def test_excluded_ranges_parse_netsh():
    assert load().excluded_ranges(NETSH) == [(5357, 5357), (9767, 9866), (50000, 50059)]


def test_bind_denied_hint_names_the_range_and_both_fixes(tmp_path):
    dev_db = load()
    ranges = dev_db.excluded_ranges(NETSH)
    hint = dev_db.bind_denied_hint(DENIED, 9800, tmp_path / ".env", ranges)
    assert "9800" in hint and "9767–9866" in hint and "EVOWIKI_DATABASE_URL" in hint
    assert "net stop winnat" in hint and "net start winnat" in hint and "15432" in hint
    assert "netsh" in dev_db.bind_denied_hint(DENIED, 15432, tmp_path / ".env", ranges)   # range unknown
    assert dev_db.bind_denied_hint("FATAL: other\n", 9800, tmp_path / ".env", ranges) is None


def test_a_reserved_port_leads_the_failure_with_the_hint(monkeypatch, tmp_path):
    dev_db = load()
    data = _stopped_dir(tmp_path)
    log = tmp_path / "pg data.log"
    log.write_text("an older run\n")
    monkeypatch.setattr(dev_db, "excluded_ranges", lambda text=None: [(9767, 9866)])
    monkeypatch.setattr(dev_db, "start_detached", lambda cmd, cwd: (log.write_text("an older run\n" + DENIED), (9, 0))[1])
    monkeypatch.setattr(dev_db, "_info", lambda d: None)
    with pytest.raises(SystemExit) as e:
        dev_db.ensure_detached(data, Path("pg_ctl.exe"), 9800)
    assert str(e.value).startswith("开发库端口 9800 绑定被拒") and "9767–9866" in str(e.value)


def test_an_old_bind_failure_in_the_log_is_not_blamed(monkeypatch, tmp_path):
    dev_db = load()
    data = _stopped_dir(tmp_path)
    (tmp_path / "pg data.log").write_text(DENIED)                   # from an earlier start
    monkeypatch.setattr(dev_db, "start_detached", lambda cmd, cwd: (9, 0))
    monkeypatch.setattr(dev_db, "_info", lambda d: None)
    with pytest.raises(SystemExit) as e:
        dev_db.ensure_detached(data, Path("pg_ctl.exe"), 9800)
    assert "绑定被拒" not in str(e.value)


# ---------- Windows service (install_pg_service.ps1) and log rotation ----------

BINPATH = ('"D:/Example/.pg18/bin/pg_ctl.exe" runservice -N "EvoWikiPG" -D "D:/Example/.pgdata" -w -t 300 '
           '-o "-h 127.0.0.1 -p 15432"')


def test_the_service_binpath_names_data_dir_and_port():
    dev_db = load()
    assert dev_db.parse_service_binpath(BINPATH) == (Path("D:/Example/.pgdata"), 15432)
    assert dev_db.parse_service_binpath("C:\\x.exe -k netsvcs") == (None, None)


class FakeService:
    def __init__(self, status="running", binpath=BINPATH):
        self.d = {"status": status, "binpath": binpath}

    def as_dict(self):
        return self.d


def _service(monkeypatch, dev_db, svc):
    import psutil
    monkeypatch.setattr(dev_db.sys, "platform", "win32")
    monkeypatch.setattr(psutil, "win_service_get", lambda name: svc, raising=False)


def test_a_service_for_another_data_dir_is_not_ours(monkeypatch, tmp_path):
    dev_db = load()
    data = tmp_path / "owned service" / ".pgdata"
    binpath = (f'"{tmp_path.as_posix()}/bin/pg_ctl.exe" runservice -N "EvoWikiPG" '
               f'-D "{data.as_posix()}" -w -t 300 -o "-h 127.0.0.1 -p 15432"')
    _service(monkeypatch, dev_db, FakeService(binpath=binpath))
    assert dev_db.service_info(tmp_path / "another instance" / ".pgdata") is None
    info = dev_db.service_info(data)
    assert info["port"] == 15432 and info["status"] == "running"


def test_a_service_on_another_port_is_refused_not_used(monkeypatch):
    dev_db = load()
    _service(monkeypatch, dev_db, FakeService())
    monkeypatch.setattr(dev_db, "_sc", lambda *a: (_ for _ in ()).throw(AssertionError("started")))
    with pytest.raises(RuntimeError, match="端口 15432.*5433"):
        dev_db.ensure_service(Path("D:/Example/.pgdata"), 5433)


class PortInfo(FakeInfo):
    port = 15432


def test_a_stopped_service_is_started_and_waited_for(monkeypatch):
    dev_db = load()
    _clock(monkeypatch, dev_db)
    _service(monkeypatch, dev_db, FakeService("stopped"))
    calls, reads = [], iter([None, PortInfo(True, "starting"), PortInfo(True, "ready")])
    monkeypatch.setattr(dev_db, "_sc", lambda verb, name: calls.append((verb, name)))
    monkeypatch.setattr(dev_db, "_info", lambda d: next(reads))
    assert dev_db.ensure_service(Path("D:/Example/.pgdata"), 15432) == "started"
    assert calls == [("start", "EvoWikiPG")]


def test_sc_access_denied_names_the_fix(monkeypatch):
    import subprocess
    dev_db = load()
    monkeypatch.setattr(dev_db.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 5, "", ""))
    with pytest.raises(RuntimeError, match="install_pg_service.ps1"):
        dev_db._sc("start", "EvoWikiPG")
    monkeypatch.setattr(dev_db.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1056, "", ""))
    dev_db._sc("start", "EvoWikiPG")                                         # already running: fine


def test_rotate_log_keeps_n_old_files(tmp_path):
    dev_db = load()
    log = tmp_path / "backend.log"
    assert not dev_db.rotate_log(log, max_bytes=10, keep=2)                 # missing: nothing to do
    for i in range(4):
        log.write_text(f"run {i} " * 5)
        assert dev_db.rotate_log(log, max_bytes=10, keep=2)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backend.log.1", "backend.log.2"]
    assert (tmp_path / "backend.log.1").read_text().startswith("run 3")
    log.write_text("small")
    assert not dev_db.rotate_log(log, max_bytes=10, keep=2)
