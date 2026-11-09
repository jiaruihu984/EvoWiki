"""scripts/evowiki_ctl.py: port choice, vault guard, plugin freshness, data.json sync, process ownership."""

import argparse
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import psutil
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evowiki_ctl.py"


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("evowiki_launcher", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_preferred_port_is_used_when_free(ev):
    assert ev.choose_port(8002, free=lambda p: True) == 8002


def test_a_foreign_port_is_skipped_never_8001(ev):
    seen = []
    port = ev.choose_port(8000, free=lambda p: seen.append(p) or p == 8003)
    assert port == 8003
    assert 8001 not in seen


def test_reserved_port_is_refused(ev):
    with pytest.raises(ev.Refused):
        ev.choose_port(8001, free=lambda p: True)


def test_no_free_port_fails_closed(ev):
    with pytest.raises(ev.Refused):
        ev.choose_port(8002, free=lambda p: False)


def test_foreign_managed_vault_is_refused(ev, tmp_path):
    vault = tmp_path / "wiki"
    (vault / ".obsidian").mkdir(parents=True)
    marker = vault / ".evowiki-owner.json"
    marker.write_text(json.dumps({"instance_root": str(tmp_path / "other")}))
    with pytest.raises(ev.Refused):
        ev.check_vault(vault, tmp_path)
    marker.write_text("{broken")
    with pytest.raises(ev.Refused):
        ev.check_vault(vault, tmp_path)
    marker.write_text(json.dumps({"instance_root": str(tmp_path)}))
    ev.check_vault(vault, tmp_path)
    marker.unlink()
    ev.check_vault(vault, tmp_path)  # explicitly selected ordinary Obsidian vault
    with pytest.raises(ev.Refused):
        ev.check_vault(tmp_path, tmp_path)


def test_needs_build_follows_source_mtimes(ev, tmp_path):
    (tmp_path / "src").mkdir()
    src_file = tmp_path / "src" / "main.ts"
    src_file.write_text("x")
    assert ev.needs_build(tmp_path)                    # no main.js yet
    out = tmp_path / "main.js"
    out.write_text("built")
    now = time.time()
    os.utime(src_file, (now - 100, now - 100))
    os.utime(out, (now, now))
    assert not ev.needs_build(tmp_path)
    os.utime(src_file, (now + 100, now + 100))
    assert ev.needs_build(tmp_path)


def test_sync_settings_changes_only_server_url(ev, tmp_path):
    data = tmp_path / "data.json"
    assert ev.sync_settings(data, "http://127.0.0.1:8002") == "created"
    data.write_text(json.dumps({"settings": {"serverUrl": "http://127.0.0.1:8000", "userId": "u"},
                                "sessions": {"a": 1}}))
    assert ev.sync_settings(data, "http://127.0.0.1:8002") == "updated"
    assert json.loads(data.read_text()) == {"settings": {"serverUrl": "http://127.0.0.1:8002", "userId": "u"},
                                            "sessions": {"a": 1}}
    assert ev.sync_settings(data, "http://127.0.0.1:8002") == "same"
    data.write_text("{broken")
    with pytest.raises(ev.Refused):
        ev.sync_settings(data, "http://127.0.0.1:8002")
    assert data.read_text() == "{broken"               # left as it was


def test_owned_process_checks_start_time(ev):
    me = psutil.Process()
    assert ev.owned_process({"pid": me.pid, "create_time": me.create_time()}).pid == me.pid
    assert ev.owned_process({"pid": me.pid, "create_time": me.create_time() - 3600}) is None  # pid reused
    assert ev.owned_process(None) is None
    assert ev.owned_process({}) is None


def test_read_env_parses_without_exporting(ev, tmp_path):
    env = tmp_path / ".env"
    env.write_text('# c\nEVOWIKI_DATABASE_URL="postgresql://u@127.0.0.1:15432/db"\nX=1\n', encoding="utf-8")
    assert ev.read_env(env) == {"EVOWIKI_DATABASE_URL": "postgresql://u@127.0.0.1:15432/db", "X": "1"}
    assert "X" not in os.environ


# ---------- up --from live / supervise (plan P3 T11): tmp repos and fakes only ----------

def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


def test_live_constants_mirror_forge_limits(ev):
    from evowiki.forge import limits
    assert ev.LIVE_ROOT == Path(limits.LIVE_ROOT) and ev.LIVE_BRANCH == limits.LIVE_BRANCH
    assert ev.SUPERVISE_POLL_S == limits.SUPERVISE_POLL_S


def test_live_backend_cmd_sets_commit(ev, tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "git_head_full", lambda root: "f" * 40)
    paths = ev.Paths(tmp_path, tmp_path / "live", tmp_path / "vault")
    paths.run.mkdir(parents=True)
    ev.write_backend_cmd(paths, 8002, "uv", bot=True, commit="abc", live=True)
    text = paths.backend_cmd.read_text(encoding="utf-8")
    assert 'set "EVOWIKI_COMMIT=abc"' in text and "EVOWIKI_LIVE_ROOT" in text
    ev.write_backend_cmd(paths, 8002, "uv", bot=True)
    assert 'set "EVOWIKI_COMMIT=' + "f" * 40 in paths.backend_cmd.read_text(encoding="utf-8")
    assert "EVOWIKI_LIVE_ROOT" not in paths.backend_cmd.read_text(encoding="utf-8")


def test_supervisor_cmd_runs_supervise_from_live_and_loops_on_restart_code(ev, tmp_path):
    paths = ev.Paths(tmp_path, tmp_path / "live", tmp_path / "vault")
    paths.run.mkdir(parents=True)
    ev.write_supervisor_cmd(paths, 8003, "uv")
    text = paths.supervisor_cmd.read_text(encoding="utf-8")
    assert "supervise --port 8003" in text and (tmp_path / "live").as_posix() in text
    assert str(paths.supervisor_log) in text and f"=={ev.RESTART_CODE}" in text


def test_ensure_live_creates_branch_from_master_once(ev, git_repo, tmp_path):
    live = tmp_path / "rsi-live"
    ev.ensure_live(git_repo, live)
    assert (live / ".git").exists()
    assert _git(live, "rev-parse", "--abbrev-ref", "HEAD") == ev.LIVE_BRANCH
    head1 = ev.git_head(live)
    (live / "landed.txt").write_text("forge", encoding="utf-8")
    _git(live, "add", "-A")
    _git(live, "commit", "-q", "-m", "landed")
    head2 = ev.git_head_full(live)
    ev.ensure_live(git_repo, live)                       # idempotent, never resets
    assert ev.git_head_full(live) == head2 and ev.git_head(live) != head1


def test_ensure_live_reuses_existing_branch_without_reset(ev, git_repo, tmp_path):
    _git(git_repo, "branch", ev.LIVE_BRANCH)
    wt = tmp_path / "wt"
    _git(git_repo, "worktree", "add", "-q", str(wt), ev.LIVE_BRANCH)
    (wt / "x.txt").write_text("x", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "on live")
    tip = ev.git_head_full(wt)
    _git(git_repo, "worktree", "remove", str(wt))
    live = tmp_path / "rsi-live"
    ev.ensure_live(git_repo, live)
    assert ev.git_head_full(live) == tip                 # the branch kept its commits


def test_ensure_live_refuses_foreign_dir(ev, git_repo, tmp_path):
    (tmp_path / "rsi-live").mkdir()
    (tmp_path / "rsi-live" / "x.txt").write_text("mine")
    with pytest.raises(ev.Refused):
        ev.ensure_live(git_repo, tmp_path / "rsi-live")
    assert (tmp_path / "rsi-live" / "x.txt").read_text() == "mine"


def test_ensure_live_refuses_worktree_on_another_branch(ev, git_repo, tmp_path):
    other = tmp_path / "rsi-live"
    _git(git_repo, "worktree", "add", "-q", "-b", "other", str(other), "master")
    with pytest.raises(ev.Refused):
        ev.ensure_live(git_repo, other)


def _live_paths(ev, tmp_path, state=None):
    paths = ev.Paths(tmp_path, tmp_path / "live", tmp_path / "vault")
    paths.run.mkdir(parents=True, exist_ok=True)
    if state is not None:
        ev.save_state(paths.state, state)
    return paths


def test_launcher_backend_only_restarts_owned(ev, tmp_path, monkeypatch):
    paths = _live_paths(ev, tmp_path, {})
    killed, launched = [], []
    b = ev.LauncherBackend(paths, 8002, kill=killed.append,
                           launch=lambda *a, **k: launched.append((a, k)))
    with pytest.raises(ev.Refused, match="不是本脚本启动的"):
        b.restart()
    assert killed == [] and launched == []
    me = psutil.Process()
    ev.save_state(paths.state, {"backend": {"pid": me.pid, "create_time": me.create_time(),
                                            "port": 8002, "bot": False, "root": str(paths.root),
                                            "instance_root": str(paths.main)}})
    monkeypatch.setattr(psutil.Process, "cwd", lambda self: str(paths.root))
    monkeypatch.setattr(ev, "git_head_full", lambda root: "f" * 40)
    b.restart()
    assert [p.pid for p in killed] == [me.pid]
    (_args, kw), = launched
    assert kw["commit"] == "f" * 40 and kw["live"] is True and kw["bot"] is False


def test_launcher_backend_http_fails_closed(ev, tmp_path):
    def boom(*a, **k):
        raise OSError("refused")
    b = ev.LauncherBackend(_live_paths(ev, tmp_path, {}), 8002, http=boom)
    assert b.health() is None and b.smoke() is False


def test_canary_reads_the_real_app_database(ev, tmp_path, pool):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from evowiki.service.app import create_app

    client = TestClient(create_app(SimpleNamespace(pool=pool, durable=None)), base_url="http://127.0.0.1")
    seen = []

    def http(method, url, *args):
        from urllib.parse import urlsplit
        path = urlsplit(url).path
        seen.append(path)
        response = client.request(method, path)
        return response.status_code, response.content

    backend = ev.LauncherBackend(_live_paths(ev, tmp_path, {}), 8002, http=http)
    assert backend.smoke() is True
    assert seen == ["/health/ready"]
    assert client.get("/versions").status_code == 404


def test_failed_drain_prevents_automatic_restart(ev, tmp_path):
    backend = ev.LauncherBackend(_live_paths(ev, tmp_path, {}), 8002,
                                http=lambda *a: (500, b""))
    with pytest.raises(ev.Refused, match="取消本次更新"):
        backend.drain(True)


def test_gpu_backend_uses_gpu_extra_and_starts_drained_on_restart(ev, tmp_path, monkeypatch):
    paths = _live_paths(ev, tmp_path, {})
    paths.env.write_text("EVOWIKI_EMBED_PROVIDERS=cuda\n", encoding="utf-8")
    monkeypatch.delenv("EVOWIKI_EMBED_PROVIDERS", raising=False)
    monkeypatch.setattr(ev, "git_head_full", lambda root: "f" * 40)
    ev.write_backend_cmd(paths, 8002, "uv", True, live=True, start_drained=True)
    command = paths.backend_cmd.read_text(encoding="utf-8")
    assert "--no-sync" in command and "--reinstall-package" not in command
    assert 'set "EVOWIKI_START_DRAINED=1"' in command
    assert 'set "EVOWIKI_EMBED_PROVIDERS=cuda"' in command


class _FakeLive:
    def __init__(self, bases):
        self.bases = list(bases)

    def merge_base(self, a, b):
        return self.bases.pop(0) if len(self.bases) > 1 else self.bases[0]


def _tick(ev, paths, step, *, env=None, pool=None, bases=("m1",)):
    return ev.supervise_tick(paths, pool=pool, backend=object(), live=_FakeLive(bases), main=object(),
                             step=step, now=lambda: None, env=env if env is not None else {},
                             forge_on=lambda pool: True)


def test_supervise_kill_switch_still_reverts(ev, tmp_path):
    from evowiki.forge.patch.live import StepResult
    seen = []

    def step(pool, **kw):
        seen.append(kw["landings"])
        return StepResult("reverted", ("fc_1",))

    paths = _live_paths(ev, tmp_path, {})
    r, reexec = _tick(ev, paths, step, env={"EVOWIKI_FORGE": "off"})
    assert seen == [False] and r.did == "reverted" and not reexec       # step still ran: undo works
    r, _ = ev.supervise_tick(paths, pool=None, backend=object(), live=_FakeLive(["m1"]), main=object(),
                             step=step, now=lambda: None, env={}, forge_on=lambda pool: False)
    assert seen == [False, False]
    _tick(ev, paths, step)
    assert seen == [False, False, True]


def test_forge_switch_error_fails_closed(ev):
    def broken(pool):
        raise RuntimeError("db down")
    assert ev.landings_allowed(None, {}, {}, forge_on=broken)[0] is False


def test_supervise_pauses_after_fallback_and_resume_clears(ev, tmp_path):
    from evowiki.forge.patch.live import StepResult
    seen = []
    results = [StepResult("canary_failed", ("fc_1",), "fallback:pre-tick；服务没有响应"),
               StepResult("nothing")]

    def step(pool, **kw):
        seen.append(kw["landings"])
        return results.pop(0)

    paths = _live_paths(ev, tmp_path, {})
    _tick(ev, paths, step)
    state = ev.load_state(paths.state)
    assert "启动失败" in state["live_paused"]
    _tick(ev, paths, step)
    assert seen == [True, False]                       # paused: only reverts are served
    ev.resume_live(paths)
    assert "live_paused" not in ev.load_state(paths.state)


def test_supervise_pauses_after_two_canary_failures_in_a_row(ev, tmp_path):
    from evowiki.forge.patch.live import StepResult
    results = [StepResult("canary_failed", ("a",), "x"), StepResult("landed", ("b",)),
               StepResult("canary_failed", ("c",), "x"), StepResult("canary_failed", ("d",), "x")]
    paths = _live_paths(ev, tmp_path, {})
    for _ in range(3):
        _tick(ev, paths, lambda pool, **kw: results.pop(0))
        assert "live_paused" not in ev.load_state(paths.state)
    _tick(ev, paths, lambda pool, **kw: results.pop(0))
    assert ev.load_state(paths.state)["live_paused"]


def test_supervise_reexecs_after_rebase(ev, tmp_path):
    from evowiki.forge.patch.live import StepResult
    paths = _live_paths(ev, tmp_path, {})
    _, reexec = _tick(ev, paths, lambda pool, **kw: StepResult("rebased"))
    assert reexec
    _, reexec = _tick(ev, paths, lambda pool, **kw: StepResult("landed", ("a",)), bases=("m1", "m2"))
    assert reexec                                      # rebase + landing in one tick
    _, reexec = _tick(ev, paths, lambda pool, **kw: StepResult("landed", ("a",)), bases=("m1", "m1"))
    assert not reexec


def test_supervise_exits_when_backend_not_owned(ev, tmp_path):
    paths = _live_paths(ev, tmp_path, {})
    assert ev.supervise(paths, 8002, tick=lambda: pytest.fail("ticked"), sleep=lambda s: None) == 1


def test_supervise_loop_reexecs_with_restart_code(ev, tmp_path, monkeypatch):
    from evowiki.forge.patch.live import StepResult
    paths = _live_paths(ev, tmp_path, {})
    monkeypatch.setattr(ev, "backend_proc", lambda state: psutil.Process())
    ticks = [(StepResult("nothing"), False), (StepResult("rebased"), True)]
    slept = []
    rc = ev.supervise(paths, 8002, tick=lambda: ticks.pop(0), sleep=slept.append)
    assert rc == ev.RESTART_CODE and slept == [ev.SUPERVISE_POLL_S]


def test_down_stops_owned_supervisor_before_backend(ev, tmp_path, monkeypatch):
    order = []
    from types import SimpleNamespace

    sup, back = SimpleNamespace(cwd=lambda: str(tmp_path)), object()
    state = {"supervisor": {"pid": 1, "create_time": 0, "instance_root": str(tmp_path), "root": str(tmp_path)},
             "backend": {"pid": 2, "create_time": 0, "port": 8002}}
    paths = _live_paths(ev, tmp_path, state)
    monkeypatch.setattr(ev, "owned_process",
                        lambda e: {1: sup, 2: back}.get((e or {}).get("pid")))
    monkeypatch.setattr(ev, "kill_tree", lambda p: order.append("supervisor" if p is sup else "backend"))
    monkeypatch.setattr(ev, "owned_backend", lambda p, s: back)
    monkeypatch.setattr(ev, "stop_owned_backend", lambda p, s: order.append("backend"))
    ev.cmd_down(paths, argparse.Namespace(db=False, port=8002))
    assert order == ["supervisor", "backend"]
    assert "supervisor" not in ev.load_state(paths.state)


def test_step_db_explains_a_port_windows_reserved(ev, monkeypatch, tmp_path):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    paths.env.write_text("EVOWIKI_DATABASE_URL=postgresql://postgres:@127.0.0.1:9800/postgres\n", encoding="utf-8")
    log = ev.dev_db.log_path(paths.data_dir)
    log.write_text("an older run\n")
    started = []

    def fake_run(cmd, **kw):
        started.append(cmd)
        log.write_text('an older run\nLOG:  could not bind IPv4 address "127.0.0.1": Permission denied\n')
        return subprocess.CompletedProcess(cmd, 1, "", "pg_ctl: could not start server")
    monkeypatch.setattr(ev.dev_db, "_info", lambda d: None)
    monkeypatch.setattr(ev.dev_db, "excluded_ranges", lambda text=None: [(9767, 9866)])
    monkeypatch.setattr(ev.subprocess, "run", fake_run)
    with pytest.raises(ev.Refused) as e:
        ev.step_db(paths, {})
    assert started[0][-2:] == ["--port", "9800"]                       # the port in .env
    msg = str(e.value)
    assert "9767–9866" in msg and "net stop winnat" in msg and str(paths.env) in msg


def test_step_db_refuses_a_remote_env_url(ev, tmp_path):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    paths.env.write_text("EVOWIKI_DATABASE_URL=postgresql://u:secret@db.example:5432/x\n", encoding="utf-8")
    with pytest.raises(ev.Refused, match="主机") as e:
        ev.step_db(paths, {})
    assert "secret" not in str(e.value)


SVC = {"name": "EvoWikiPG", "status": "stopped", "port": 15432}


def _env_15432(ev, tmp_path):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    paths.env.write_text("EVOWIKI_DATABASE_URL=postgresql://postgres:@127.0.0.1:15432/postgres\n", encoding="utf-8")
    return paths


def test_step_db_uses_the_windows_service_when_it_serves_the_data_dir(ev, monkeypatch, tmp_path):
    paths = _env_15432(ev, tmp_path)
    monkeypatch.setattr(ev.dev_db, "service_info", lambda d: SVC)
    monkeypatch.setattr(ev.dev_db, "ensure_service", lambda d, port: "started")
    monkeypatch.setattr(ev.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("dev_db.py run")))
    state = {}
    assert ev.step_db(paths, state).endswith(":15432/postgres")
    assert state["db"] == {"service": "EvoWikiPG"}


def test_step_db_service_failure_fails_closed(ev, monkeypatch, tmp_path):
    paths = _env_15432(ev, tmp_path)
    monkeypatch.setattr(ev.dev_db, "service_info", lambda d: SVC)

    def denied(d, port):
        raise RuntimeError("sc start EvoWikiPG 失败（错误 5）")
    monkeypatch.setattr(ev.dev_db, "ensure_service", denied)
    with pytest.raises(ev.Refused, match="错误 5"):
        ev.step_db(paths, {})


def test_down_db_preserves_the_shared_service(ev, monkeypatch, tmp_path):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    stopped = []
    monkeypatch.setattr(ev.dev_db, "service_info", lambda d: {**SVC, "status": "running"})
    monkeypatch.setattr(ev.dev_db, "stop_service", lambda name: stopped.append(name))
    monkeypatch.setattr(ev, "is_evowiki", lambda port, timeout=2.0: False)
    ev.cmd_down(paths, argparse.Namespace(db=True, port=8002))
    assert stopped == []


def test_backend_log_is_rotated_before_a_launch(ev, monkeypatch, tmp_path):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    paths.run.mkdir()
    paths.backend_log.write_bytes(b"x" * (ev.dev_db.LOG_MAX_BYTES + 1))
    monkeypatch.setattr(ev.shutil, "which", lambda n: "uv")
    monkeypatch.setattr(ev, "git_head_full", lambda root: "f" * 40)
    monkeypatch.setattr(ev.dev_db, "start_detached", lambda cmd, cwd: (9, 0))
    with pytest.raises(ev.Refused, match="WMI"):
        ev.launch_backend(paths, 8002, {}, bot=False)
    assert (paths.run / "backend.log.1").stat().st_size == ev.dev_db.LOG_MAX_BYTES + 1
    assert not paths.backend_log.exists()


def test_title_only_server_is_never_reused(ev, monkeypatch):
    monkeypatch.setattr(ev, "is_evowiki", lambda port, **kw: True)
    assert ev.choose_port(19000, free=lambda port: port == 19001) == 19001


def test_identity_requires_exact_commit_instance_and_contract(ev, tmp_path, monkeypatch):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    monkeypatch.setattr(ev, "git_head_full", lambda root: "new-version")
    health = {"commit": "new-version", "instance_root": str(tmp_path), "protocol_version": 3}
    assert ev.matches_identity(paths, health)
    assert not ev.matches_identity(paths, {**health, "commit": "old-version"})
    assert not ev.matches_identity(paths, {**health, "instance_root": str(tmp_path / "other")})
    assert not ev.matches_identity(paths, {"commit": "new-version"})


def test_safe_stop_waits_for_drain_and_work_before_killing(ev, tmp_path, monkeypatch):
    paths = _live_paths(ev, tmp_path, {"backend": {"port": 19000}})
    proc = object()
    events = []
    health = [{"drained": True, "in_flight": 1}, {"drained": True, "in_flight": 0}]
    monkeypatch.setattr(ev, "owned_backend", lambda p, s: proc)
    monkeypatch.setattr(ev, "listener_owned", lambda p, port: True)
    monkeypatch.setattr(ev.LauncherBackend, "drain", lambda self, on: events.append("drain"))
    monkeypatch.setattr(ev.LauncherBackend, "health", lambda self: health.pop(0))
    monkeypatch.setattr(ev.time, "sleep", lambda seconds: events.append("wait"))
    monkeypatch.setattr(ev, "kill_tree", lambda p: events.append("kill"))
    ev.stop_owned_backend(paths, ev.load_state(paths.state))
    assert events == ["drain", "wait", "kill"]
    assert "backend" not in ev.load_state(paths.state)


def test_drain_timeout_preserves_process_and_state(ev, tmp_path, monkeypatch):
    paths = _live_paths(ev, tmp_path, {"backend": {"port": 19000}})
    monkeypatch.setattr(ev, "owned_backend", lambda p, s: object())
    monkeypatch.setattr(ev, "listener_owned", lambda p, port: True)
    monkeypatch.setattr(ev.LauncherBackend, "drain", lambda self, on: None)
    monkeypatch.setattr(ev.LauncherBackend, "health", lambda self: {"drained": True, "in_flight": 1})
    monkeypatch.setattr(ev, "DRAIN_TIMEOUT", 0)
    monkeypatch.setattr(ev, "kill_tree", lambda p: pytest.fail("must not kill in-flight work"))
    with pytest.raises(ev.Refused, match="排空等待"):
        ev.stop_owned_backend(paths, ev.load_state(paths.state))
    assert "backend" in ev.load_state(paths.state)


def test_pid_creation_time_alone_does_not_authorize_another_root(ev, tmp_path):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    me = psutil.Process()
    state = {"backend": {"pid": me.pid, "create_time": me.create_time(), "root": str(tmp_path / "foreign")}}
    with pytest.raises(ev.Refused, match="归属"):
        ev.owned_backend(paths, state)


def test_failed_plugin_copy_keeps_whole_previous_install(ev, tmp_path, monkeypatch):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    paths.plugin_src.mkdir()
    paths.plugin_dest.mkdir(parents=True)
    for name in ev.PLUGIN_FILES:
        (paths.plugin_src / name).write_text("new", encoding="utf-8")
        (paths.plugin_dest / name).write_text("old", encoding="utf-8")
    (paths.plugin_dest / "data.json").write_text('{"sessions":{"mine":1}}', encoding="utf-8")
    (paths.plugin_dest / ".evowiki-install.json").write_text(
        json.dumps({"instance_root": str(paths.main)}), encoding="utf-8")
    monkeypatch.setattr(ev, "needs_build", lambda src: False)
    original = ev.shutil.copy2
    def copy(source, target):
        if Path(source).name == "manifest.json":
            raise OSError("simulated disk error")
        return original(source, target)
    monkeypatch.setattr(ev.shutil, "copy2", copy)
    with pytest.raises(OSError, match="disk"):
        ev.step_plugin(paths, 19000)
    assert all((paths.plugin_dest / name).read_text(encoding="utf-8") == "old" for name in ev.PLUGIN_FILES)
    assert json.loads((paths.plugin_dest / "data.json").read_text(encoding="utf-8"))["sessions"] == {"mine": 1}
    assert not paths.plugin_dest.with_name(".evowiki-installing").exists()


@pytest.mark.skipif(os.name != "nt", reason="real Windows task scheduler query")
def test_autostart_preview_accepts_no_task_for_this_instance(ev, tmp_path):
    # Read-only: the fresh temporary instance has never registered a startup task.
    # A filtered Get-ScheduledTask query reports this normal absence as exit 1.
    ev._autostart(ev.Paths(tmp_path, tmp_path, tmp_path / "vault"), False)


def test_uninstall_preview_and_apply_preserve_notes_config_and_plugin_data(ev, tmp_path, monkeypatch):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    paths.plugin_dest.mkdir(parents=True)
    paths.env.write_text("DEEPSEEK_API_KEY=local-only", encoding="utf-8")
    note = paths.vault / "my note.md"
    note.write_text("personal", encoding="utf-8")
    (paths.plugin_dest / "data.json").write_text('{"sessions":{"mine":1}}', encoding="utf-8")
    (paths.plugin_dest / ".evowiki-install.json").write_text(
        json.dumps({"instance_root": str(paths.main)}), encoding="utf-8")
    enabled = paths.vault / ".obsidian" / "community-plugins.json"
    enabled.write_text('["evowiki","other-plugin"]', encoding="utf-8")
    for name in ev.PLUGIN_FILES:
        (paths.plugin_dest / name).write_text("owned", encoding="utf-8")
    marker = paths.plugin_dest / ".evowiki-install.json"
    marker.write_text(json.dumps({"instance_root": str(tmp_path), "files": {name: ev.digest(paths.plugin_dest / name) for name in ev.PLUGIN_FILES}}), encoding="utf-8")
    monkeypatch.setattr(ev, "_autostart", lambda p, apply: None)
    monkeypatch.setattr(ev, "cmd_down", lambda p, a: 0)
    args = argparse.Namespace(port=19000, apply=False, remove_runtime=False, delete_data=False)
    assert ev.cmd_uninstall(paths, args) == 0
    assert marker.exists() and (paths.plugin_dest / "main.js").exists()
    assert json.loads(enabled.read_text(encoding="utf-8")) == ["evowiki", "other-plugin"]
    args.apply = True
    assert ev.cmd_uninstall(paths, args) == 0
    assert not marker.exists() and not (paths.plugin_dest / "main.js").exists()
    assert note.read_text(encoding="utf-8") == "personal" and paths.env.exists()
    assert (paths.plugin_dest / "data.json").exists()
    assert json.loads(enabled.read_text(encoding="utf-8")) == ["other-plugin"]


def test_uninstall_keeps_externally_replaced_plugin_and_enabled_list(ev, tmp_path, monkeypatch):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    paths.plugin_dest.mkdir(parents=True)
    for name in ev.PLUGIN_FILES:
        (paths.plugin_dest / name).write_text("original", encoding="utf-8")
    marker = paths.plugin_dest / ".evowiki-install.json"
    marker.write_text(json.dumps({"instance_root": str(tmp_path), "files": {name: ev.digest(paths.plugin_dest / name) for name in ev.PLUGIN_FILES}}), encoding="utf-8")
    (paths.plugin_dest / "main.js").write_text("externally replaced", encoding="utf-8")
    enabled = paths.vault / ".obsidian" / "community-plugins.json"
    enabled.write_text('["evowiki","other-plugin"]', encoding="utf-8")
    monkeypatch.setattr(ev, "_autostart", lambda *a: None)
    monkeypatch.setattr(ev, "cmd_down", lambda *a: 0)
    args = argparse.Namespace(port=19000, apply=True, remove_runtime=False, delete_data=False)
    assert ev.cmd_uninstall(paths, args) == 0
    assert all((paths.plugin_dest / name).exists() for name in ev.PLUGIN_FILES) and marker.exists()
    assert json.loads(enabled.read_text(encoding="utf-8")) == ["evowiki", "other-plugin"]


def test_reinstall_reenables_only_the_owned_default_vault_plugin(ev, tmp_path, monkeypatch):
    from evowiki_run import initialize

    (tmp_path / ".env.example").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    initialize(tmp_path)
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / ".evowiki-vault")
    paths.plugin_src.mkdir()
    for name in ev.PLUGIN_FILES:
        (paths.plugin_src / name).write_text("owned", encoding="utf-8")
    enabled = paths.vault / ".obsidian" / "community-plugins.json"
    enabled.write_text('["other-plugin"]', encoding="utf-8")
    monkeypatch.setattr(ev, "needs_build", lambda *a: False)
    monkeypatch.setattr(ev, "_autostart", lambda *a: None)
    monkeypatch.setattr(ev, "cmd_down", lambda *a: 0)
    ev.step_plugin(paths, 19000)
    assert json.loads(enabled.read_text(encoding="utf-8")) == ["other-plugin", "evowiki"]
    args = argparse.Namespace(port=19000, apply=True, remove_runtime=False, delete_data=False)
    ev.cmd_uninstall(paths, args)
    assert json.loads(enabled.read_text(encoding="utf-8")) == ["other-plugin"]
    ev.step_plugin(paths, 19000)
    assert json.loads(enabled.read_text(encoding="utf-8")) == ["other-plugin", "evowiki"]


def test_runtime_uninstall_stops_owned_db_and_refuses_other_runtime_users(ev, tmp_path, monkeypatch):
    from types import SimpleNamespace

    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    (paths.vault / ".obsidian").mkdir(parents=True)
    runtime = tmp_path / ".venv"
    runtime.mkdir()
    calls = []
    monkeypatch.setattr(ev, "_autostart", lambda *a: None)
    monkeypatch.setattr(ev, "cmd_down", lambda p, a: calls.append(a.db))
    other = SimpleNamespace(pid=os.getpid() + 10000, info={"exe": str(runtime / "postgres.exe")})
    monkeypatch.setattr(ev.psutil, "process_iter", lambda attrs: [other])
    args = argparse.Namespace(port=19000, apply=True, remove_runtime=True, delete_data=False)
    with pytest.raises(ev.Refused, match="仍被进程"):
        ev.cmd_uninstall(paths, args)
    assert calls == [True] and runtime.exists()


def test_unknown_migration_refuses_old_code_before_backup_or_mutation(ev, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import psycopg

    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: Connection())
    monkeypatch.setattr(ev, "load_migrate", lambda root: SimpleNamespace(MIGRATIONS_DIR=tmp_path, migrate=lambda conn: pytest.fail("old code must not migrate")))
    monkeypatch.setattr(ev, "pending_migrations", lambda conn, path: ([], ["9999_future"]))
    with pytest.raises(ev.Refused, match="不能直接降级"):
        ev.step_migrate(paths, "postgresql://local/db", backend_running=False)
    assert not paths.backups.exists()


def test_up_orders_old_shutdown_before_migration_and_plugin_before_new_backend(ev, tmp_path, monkeypatch):
    paths = _live_paths(ev, tmp_path)
    events = []
    monkeypatch.setattr(ev, "cmd_preflight", lambda p, a: events.append("old_drained_stopped") or 75)
    monkeypatch.setattr(ev, "step_db", lambda p, s: events.append("database") or "postgresql://local/db")
    monkeypatch.setattr(ev, "step_migrate", lambda *a, **kw: events.append("backup_migrate"))
    monkeypatch.setattr(ev, "build_plugin", lambda *a: events.append("plugin_prebuild"))
    monkeypatch.setattr(ev, "step_port", lambda *a: (19000, False))
    monkeypatch.setattr(ev.shutil, "which", lambda name: name)
    monkeypatch.setattr(ev.subprocess, "run", lambda *a, **kw: events.append("prepare") or subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(ev, "runtime_args", lambda *a: ["--locked"])
    monkeypatch.setattr(ev, "step_plugin", lambda *a: events.append("plugin"))
    monkeypatch.setattr(ev, "step_backend", lambda *a, **kw: events.append("new_backend"))
    monkeypatch.setattr(ev, "git_head_full", lambda root: "new")
    monkeypatch.setattr(ev, "_http", lambda *a: (503, b""))
    args = argparse.Namespace(port=19000, no_bot=True, from_="master")
    assert ev.cmd_up(paths, args) == 0
    assert events == ["old_drained_stopped", "database", "plugin_prebuild", "backup_migrate", "prepare", "plugin", "new_backend"]


def test_corrupt_state_refuses_instead_of_discarding_process_ownership(ev, tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ev.Refused, match="无法核验"):
        ev.load_state(path)


def test_data_deletion_refuses_an_external_or_unmarked_vault(ev, tmp_path, monkeypatch):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    (paths.vault / ".obsidian").mkdir(parents=True)
    (tmp_path / ".evowiki-vault").mkdir()
    monkeypatch.setattr(ev.dev_db, "service_info", lambda path: None)
    args = argparse.Namespace(port=19000, apply=True, remove_runtime=False, delete_data=True, confirm_delete_root=tmp_path)
    with pytest.raises(ev.Refused, match="创建记录"):
        ev.cmd_uninstall(paths, args)
    assert (tmp_path / ".evowiki-vault").exists()


def test_detached_backend_keeps_explicit_caches_and_bot_choice_without_key_files(ev, tmp_path, monkeypatch):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    paths.run.mkdir()
    monkeypatch.setattr(ev, "git_head_full", lambda root: "f" * 40)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "cache 中文"))
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "embedding cache"))
    monkeypatch.setenv("EVOWIKI_BOT", "off")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-in-command-file")
    ev.write_backend_cmd(paths, 19000, "uv", True)
    text = paths.backend_cmd.read_text(encoding="utf-8")
    assert str(tmp_path / "cache 中文") in text and str(tmp_path / "embedding cache") in text
    assert 'set "EVOWIKI_BOT=off"' in text
    assert "never-in-command-file" not in text


@pytest.mark.parametrize("configured", [None, "D:/external tools/pg_dump.exe"])
def test_launcher_injects_an_explicit_postgres_tool_without_runtime_discovery(ev, tmp_path, monkeypatch, configured):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    paths.run.mkdir()
    monkeypatch.setattr(ev, "git_head_full", lambda root: "f" * 40)
    if configured:
        monkeypatch.setenv("EVOWIKI_PG_DUMP", configured)
        monkeypatch.setattr(ev, "pg_dump_path", lambda: pytest.fail("explicit tool must be kept"))
    else:
        monkeypatch.delenv("EVOWIKI_PG_DUMP", raising=False)
        monkeypatch.setattr(ev, "pg_dump_path", lambda: Path("D:/bundled tools/pg_dump.exe"))
    ev.write_backend_cmd(paths, 19000, "uv", True)
    expected = configured or str(Path("D:/bundled tools/pg_dump.exe"))
    assert f'set "EVOWIKI_PG_DUMP={expected}"' in paths.backend_cmd.read_text(encoding="utf-8")


def test_process_embedding_change_prevents_reusing_previous_runtime(ev, tmp_path, monkeypatch):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    monkeypatch.setenv("EVOWIKI_EMBED_PROVIDERS", "cpu")
    cpu = ev.runtime_signature(paths)
    monkeypatch.setenv("EVOWIKI_EMBED_PROVIDERS", "cuda")
    assert ev.runtime_signature(paths) != cpu


def test_cli_display_language_does_not_restart_the_business_runtime(ev, tmp_path, monkeypatch):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    monkeypatch.setenv("EVOWIKI_CLI_LANGUAGE", "zh")
    signature = ev.runtime_signature(paths)
    monkeypatch.setenv("EVOWIKI_CLI_LANGUAGE", "en")
    assert ev.runtime_signature(paths) == signature


def test_unowned_existing_plugin_is_not_overwritten(ev, tmp_path):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    paths.plugin_dest.mkdir(parents=True)
    entry = paths.plugin_dest / "main.js"
    entry.write_text("unowned", encoding="utf-8")
    with pytest.raises(ev.Refused, match="归属|另一个目录"):
        ev.step_plugin(paths, 19000)
    assert entry.read_text(encoding="utf-8") == "unowned"


@pytest.mark.parametrize("scenario,apply", [
    ("missing-folder", False), ("missing-task", True), ("foreign-path", True),
    ("foreign-directory", True), ("blank-directory", True), ("unknown-action", True),
    ("multiple-actions", True), ("owned", False), ("owned", True),
    ("connect-failure", False), ("folder-failure", False), ("task-failure", True),
    ("delete-failure", True), ("initialize-failure", False),
])
def test_autostart_cleanup_matches_native_executable(ev, tmp_path, monkeypatch, scenario, apply):
    import sys
    from types import SimpleNamespace

    events = []

    class ComError(Exception):
        def __init__(self, hresult, wrapped=None):
            self.hresult = hresult
            self.excepinfo = (None, None, None, None, None, wrapped) if wrapped is not None else None

    def fail(code=0x80070005, wrapped=None):
        raise ComError(code, wrapped)

    def initialize():
        events.append("initialize")
        if scenario == "initialize-failure":
            fail()

    action = SimpleNamespace(Type=5 if scenario == "unknown-action" else 0,
                             Path=str(tmp_path / ("foreign.exe" if scenario == "foreign-path" else "EvoWiki.exe")),
                             WorkingDirectory="" if scenario == "blank-directory" else str(tmp_path / "other" if scenario == "foreign-directory" else tmp_path))
    actions = SimpleNamespace(Count=2 if scenario == "multiple-actions" else 1,
                              Item=lambda index: events.append(("action", index)) or action)
    task = SimpleNamespace(Definition=SimpleNamespace(Actions=actions))

    def get_task(name):
        events.append(("task", name))
        if scenario == "missing-task":
            fail(-2147352567, -2147024894)  # Real Schedule.Service IDispatch wrapper.
        if scenario == "task-failure":
            fail(-2147352567, -2147024891)
        return task

    def delete_task(name, flags):
        events.append(("delete", name, flags))
        if scenario == "delete-failure":
            fail()

    folder = SimpleNamespace(GetTask=get_task, DeleteTask=delete_task)

    def get_folder(name):
        events.append(("folder", name))
        if scenario == "missing-folder":
            fail(0x80070003)
        if scenario == "folder-failure":
            fail(0x80070005)
        return folder

    def connect():
        events.append("connect")
        if scenario == "connect-failure":
            fail(0x80070002)  # Not-found during Connect is still a real failure.

    service = SimpleNamespace(Connect=connect, GetFolder=get_folder)
    client = SimpleNamespace(Dispatch=lambda name: events.append(("dispatch", name)) or service)
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(CoInitialize=initialize,
                        CoUninitialize=lambda: events.append("uninitialize"), com_error=ComError))
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(ev.sys, "platform", "win32")
    monkeypatch.setattr(ev.subprocess, "run", lambda *_a, **_kw: pytest.fail("no PowerShell or enumeration"))
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    if scenario.endswith("failure"):
        with pytest.raises(ev.Refused, match="自启动"):
            ev._autostart(paths, apply)
    else:
        ev._autostart(paths, apply)
    assert events[0] == "initialize"
    assert ("uninitialize" in events) == (scenario != "initialize-failure")
    if scenario != "initialize-failure":
        assert events[-1] == "uninitialize"
    assert (("delete", "Start", 0) in events) == (apply and scenario in {"owned", "delete-failure"})
    if "connect" in events and scenario != "connect-failure":
        assert ("folder", "\\EvoWiki") in events
    assert not any(isinstance(event, tuple) and event[0] == "task" and event[1] != "Start" for event in events)

@pytest.mark.parametrize("scenario", ["owned", "foreign", "busy", "gone", "exited", "reused", "denied", "timeout"])
def test_uninstall_stops_only_idle_owned_tray(ev, tmp_path, monkeypatch, scenario):
    calls = []
    tray_pid = os.getpid() + 10000

    class Process:
        pid = tray_pid

        def __init__(self):
            self.info = {"exe": str(tmp_path / ("other" if scenario == "foreign" else "") / "EvoWiki.exe")}

        def create_time(self):
            if scenario == "gone":
                raise psutil.NoSuchProcess(self.pid)
            return 123.0

        def exe(self):
            return self.info["exe"]

        def children(self, recursive):
            calls.append("children")
            if scenario == "denied":
                raise psutil.AccessDenied(self.pid)
            return [object()] if scenario == "busy" else []

        def terminate(self):
            calls.append("terminate")
            if scenario == "exited":
                raise psutil.NoSuchProcess(self.pid)

        def wait(self, timeout):
            calls.append(("wait", timeout))
            if scenario == "timeout":
                raise psutil.TimeoutExpired(timeout, self.pid)

    process = Process()
    monkeypatch.setattr(ev.psutil, "process_iter", lambda attrs: [process])
    def current_process(pid):
        assert pid == tray_pid
        if scenario == "reused":
            from types import SimpleNamespace
            return SimpleNamespace(create_time=lambda: 999.0)
        return process
    monkeypatch.setattr(ev.psutil, "Process", current_process)
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    if scenario in {"busy", "denied", "timeout"}:
        with pytest.raises(ev.Refused, match="托盘"):
            ev._stop_tray(paths)
    else:
        ev._stop_tray(paths)
    assert ("terminate" in calls) == (scenario in {"owned", "exited", "timeout"})
    assert (("wait", 10) in calls) == (scenario in {"owned", "timeout"})


@pytest.mark.parametrize("apply,down_fails", [(False, False), (True, False), (True, True)])
def test_uninstall_stops_tray_only_after_successful_down(ev, tmp_path, monkeypatch, apply, down_fails):
    paths = ev.Paths(tmp_path, tmp_path, tmp_path / "vault")
    (paths.vault / ".obsidian").mkdir(parents=True)
    events = []
    monkeypatch.setattr(ev, "_autostart", lambda paths, apply: events.append(("autostart", apply)))
    monkeypatch.setattr(ev, "_stop_tray", lambda paths: events.append("tray"))
    def down(paths, args):
        events.append("down")
        if down_fails:
            raise ev.Refused("down failed")
        return 0
    monkeypatch.setattr(ev, "cmd_down", down)
    args = argparse.Namespace(port=19000, apply=apply, remove_runtime=False, delete_data=False)
    if down_fails:
        with pytest.raises(ev.Refused, match="down failed"):
            ev.cmd_uninstall(paths, args)
    else:
        assert ev.cmd_uninstall(paths, args) == 0
    assert ("tray" in events) == (apply and not down_fails)
    if "tray" in events:
        assert events.index("down") < events.index("tray") < events.index(("autostart", True))
