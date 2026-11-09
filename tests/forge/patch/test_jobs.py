"""The L2 Job Object runner, for real (plan P3 T4): limits, tree kill, secret-free env, network off,
and the worktree's `src` winning over the editable install. Windows + pywin32 only."""

import importlib.util
import sys
import time
from pathlib import Path

import psutil
import pytest

from evowiki.forge import limits
from evowiki.forge.patch import junit
from evowiki.forge.patch.commands import child_env, parse
from evowiki.forge.sandbox import SandboxUnavailable

real = pytest.mark.skipif(sys.platform != "win32" or importlib.util.find_spec("win32job") is None,
                          reason="needs Windows Job Objects (pywin32)")


def _env(tmp: Path) -> dict[str, str]:
    (tmp / "t").mkdir(exist_ok=True)
    return child_env(worktree=tmp, tmp=tmp / "t", test_db_url="postgresql://127.0.0.1:1/x",
                     python=sys.executable)


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.fixture
def runner(tmp_path):
    from evowiki.forge.sandbox.jobs import JobRunner
    return JobRunner(tmp_root=tmp_path / "jobs")


def test_not_windows_is_unavailable(monkeypatch):
    from evowiki.forge.sandbox import jobs
    monkeypatch.setattr(jobs.sys, "platform", "linux")
    with pytest.raises(SandboxUnavailable):
        jobs.JobRunner()


def test_policy_name():
    from evowiki.forge.sandbox.jobs import JobRunner
    assert JobRunner.policy == "workspace-write"


@real
def test_runs_and_captures(runner, tmp_path):
    out = runner.run(_py("print('hi')"), cwd=tmp_path, env=_env(tmp_path))
    assert out.exit_code == 0 and "hi" in out.output and out.stats.exit == "ok"
    assert out.peak_memory and out.peak_memory > 0


@real
def test_error_exit(runner, tmp_path):
    out = runner.run(_py("import sys; print('bad'); sys.exit(3)"), cwd=tmp_path, env=_env(tmp_path))
    assert out.exit_code == 3 and out.stats.exit == "error" and "bad" in out.output


@real
def test_timeout_kills(runner, tmp_path):
    t0 = time.monotonic()
    out = runner.run(_py("import time; time.sleep(30)"), cwd=tmp_path, env=_env(tmp_path), wall_s=1)
    assert out.exit_code is None and out.stats.exit == "timeout"
    assert out.stats.wall_s < 5 and time.monotonic() - t0 < 10


@real
def test_grandchild_dies_with_the_job(runner, tmp_path):
    code = ("import subprocess, sys; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print('PID', p.pid, flush=True)")
    out = runner.run(_py(code), cwd=tmp_path, env=_env(tmp_path), wall_s=30)
    assert out.exit_code == 0, out.output
    pid = int(out.output.split("PID", 1)[1].split()[0])
    deadline = time.monotonic() + 5
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not psutil.pid_exists(pid)


@real
def test_timeout_kills_the_whole_tree(runner, tmp_path):
    code = ("import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print('PID', p.pid, flush=True); time.sleep(60)")
    out = runner.run(_py(code), cwd=tmp_path, env=_env(tmp_path), wall_s=3)
    assert out.stats.exit == "timeout"
    pid = int(out.output.split("PID", 1)[1].split()[0])
    deadline = time.monotonic() + 5
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not psutil.pid_exists(pid)


@real
def test_memory_limit(runner, tmp_path):
    out = runner.run(_py("b = bytearray(3 << 30); print(len(b))"), cwd=tmp_path, env=_env(tmp_path),
                     wall_s=60)
    assert out.exit_code != 0
    assert out.peak_memory is not None and out.peak_memory >= limits.L2.memory_bytes
    assert out.stats.exit == "memory"


@real
def test_output_cap(runner, tmp_path):
    out = runner.run(_py("import sys; sys.stdout.write('x' * (1 << 20) + 'END')"), cwd=tmp_path,
                     env=_env(tmp_path))
    assert out.stats.exit == "output_cap"
    assert len(out.output) <= limits.L2.output_bytes and out.output.endswith("END")   # the tail


@real
def test_process_cap(runner, tmp_path):
    code = ("import subprocess, sys\n"
            "ps = []\n"
            "try:\n"
            "    for _ in range(40):\n"
            "        ps.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']))\n"
            "except OSError as e:\n"
            "    print('REFUSED', len(ps), e)\n"
            "    sys.exit(7)\n"
            "print('ALL', len(ps))\n")
    t0 = time.monotonic()
    out = runner.run(_py(code), cwd=tmp_path, env=_env(tmp_path), wall_s=60)
    assert "REFUSED" in out.output and out.exit_code == 7, out.output
    assert time.monotonic() - t0 < 30                                  # the runner does not hang


@real
def test_child_sees_no_parent_secrets(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("EVOWIKI_DATABASE_URL", "postgresql://dev-db-should-never-leak")
    code = "import os; print('\\n'.join(f'{k}={v}' for k, v in sorted(os.environ.items())))"
    out = runner.run(_py(code), cwd=tmp_path, env=_env(tmp_path))
    assert out.exit_code == 0
    assert "should-never-leak" not in out.output and "DEEPSEEK_API_KEY" not in out.output
    assert "EVOWIKI_TEST_DATABASE_URL=postgresql://127.0.0.1:1/x" in out.output


@real
def test_stdin_is_empty(runner, tmp_path):
    out = runner.run(_py("import sys; print(repr(sys.stdin.read()))"), cwd=tmp_path, env=_env(tmp_path))
    assert out.exit_code == 0 and "''" in out.output


@real
def test_worktree_src_wins_over_the_editable_install(runner, tmp_path):
    pkg = tmp_path / "src" / "evowiki"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('MARK = "wt"\n')
    out = runner.run(_py("import evowiki; print(evowiki.MARK, evowiki.__file__)"), cwd=tmp_path,
                     env=_env(tmp_path))
    assert out.exit_code == 0, out.output
    assert "wt" in out.output and str(pkg / "__init__.py") in out.output


@real
def test_sandboxed_pytest_has_no_network_and_reports_by_junit(runner, tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_net.py").write_text(
        "import socket\n\n"
        "def test_outside_is_blocked():\n"
        "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    try:\n"
        "        s.connect(('93.184.216.34', 80))\n"
        "    finally:\n"
        "        s.close()\n\n"
        "def test_plain():\n"
        "    assert 1 + 1 == 2\n")
    cmd = parse(["pytest", "tests/test_net.py"], root=tmp_path, python=sys.executable,
                junit_dir=tmp_path / ".junit")
    out = runner.run(cmd.argv, cwd=tmp_path, env=_env(tmp_path), wall_s=120)
    assert cmd.junit is not None
    got = junit.read(cmd.junit)
    assert got == {"tests/test_net.py::test_outside_is_blocked": "failed",
                   "tests/test_net.py::test_plain": "passed"}, out.output
    assert "SocketBlockedError" in out.output or "Socket" in out.output
