"""Hostile patches: what a patch may touch on the real tree, what its added lines may reach, and what its test can do
in the real child environment under the real Job Object runner. No model."""

import subprocess
import sys
from pathlib import Path

import pytest

from evowiki.forge.patch import commands
from evowiki.forge.patch.paths import Change, check_change, reach_hits, simulator_files
from evowiki.forge.sandbox.jobs import JobRunner

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def tree() -> tuple[frozenset[str], frozenset[str]]:
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           check=True).stdout.splitlines()
    return frozenset(files), simulator_files(ROOT)


@pytest.mark.parametrize("path", [
    "src/evowiki/harness/keys.py", "src/evowiki/harness/anchors.py", "src/evowiki/harness/judge_prompt.py",
    "src/evowiki/harness/improver_prompt.py", "src/evowiki/harness/gate.py", "src/evowiki/teach/anchors.py",
    "src/evowiki/store/migrations/0200_x.sql", "src/evowiki/experiments/x.py", "src/evowiki/rsib/x.py",
    "CLAUDE.md", ".claude/settings.json", ".env", "pyproject.toml",
    "src/evowiki/forge/sandbox/jobs.py", "src/evowiki/forge/sandbox/site/sitecustomize.py",
    "src/evowiki/forge/patch/swe.py", "src/evowiki/forge/policy.py", "src/evowiki/forge/limits.py",
    "src/evowiki/evaluation/page.py", "src/evowiki/simulator/x.py", "src/evowiki/llm/gateway.py",
    "tests/llm_cases/evalset/grade.py", "tests/llm_cases/evalset/conftest.py", "tests/llm_cases/evalset/cases/x.yaml",
    "tests/conftest.py", "tests/deep/conftest.py", "tests/deep/test_writer.py", "tests/forge_x/conftest.py",
])
def test_protected_paths_are_refused(tree, path):
    base, sim = tree
    assert check_change([Change("A", path)], base_files=base, sim_files=sim)
    assert check_change([Change("M", path)], base_files=base, sim_files=sim)


def test_files_the_simulator_imports_are_refused(tree):
    base, sim = tree
    non_sim = [f for f in sim if not f.startswith(("src/evowiki/simulator/", "simulator/"))]
    assert non_sim
    for f in non_sim:
        assert check_change([Change("M", f)], base_files=base, sim_files=sim), f


def test_an_ordinary_patch_is_allowed(tree):
    base, sim = tree
    assert check_change([Change("M", "src/evowiki/assistant/loop.py"), Change("A", "tests/forge_cap/test_cap.py"),
                         Change("A", "tests/forge_cap/__init__.py")], base_files=base, sim_files=sim) == ()


@pytest.mark.parametrize("line", [
    "from evowiki.harness.gate import decide", "import evowiki.harness.judge as j",
    "from evowiki.evaluation import page", "JUDGE_CALIBRATED = True", "p = 'tests/llm_cases/evalset/grade.py'",
    "open('D:/Example/.env').read()", "os.environ['ANTHROPIC_API_KEY']",
    "import socket", "from urllib.request import urlopen", "import httpx", "import subprocess", "os.system('x')",
])
def test_added_lines_that_reach_too_far(line):
    assert reach_hits(f"+++ b/src/evowiki/assistant/x.py\n+{line}\n")


def test_an_ordinary_line_reaches_nothing():
    assert reach_hits("+from evowiki.store import forge\n-import socket\n+x = 1\n") == ()


HOSTILE = r'''
import os, shutil, socket, subprocess, sys, urllib.request

SECRET, OUT, OUTSIDE = os.environ["H_SECRET"], os.environ["H_OUT"], os.environ["H_OUTSIDE"]
glob = SECRET[:-1] + "*"
TRIES = {
    "open": lambda: open(SECRET, encoding="utf-8").read(),
    "os.open": lambda: os.read(os.open(SECRET, os.O_RDONLY), 99).decode(),
    "copy": lambda: shutil.copy(SECRET, "stolen.txt") and open("stolen.txt", encoding="utf-8").read(),
    "link": lambda: os.link(SECRET, "stolen2.txt") or open("stolen2.txt", encoding="utf-8").read(),
    "cmd": lambda: subprocess.run(["cmd", "/c", "type", glob], capture_output=True, text=True).stdout,
    "shell": lambda: subprocess.run("type " + glob, shell=True, capture_output=True, text=True).stdout,
    "powershell": lambda: subprocess.run(["powershell", "-c", "gc " + glob], capture_output=True, text=True).stdout,
    "python -I": lambda: subprocess.run(
        [sys.executable, "-I", "-c", f"print(open({SECRET!r}).read())"], capture_output=True, text=True).stdout,
    "python -S": lambda: subprocess.run(
        [sys.executable, "-S", "-c", f"print(open({SECRET!r}).read())"], capture_output=True, text=True).stdout,
    "python env": lambda: subprocess.run(
        [sys.executable, "-c", f"print(open({SECRET!r}).read())"], env={}, capture_output=True, text=True).stdout,
    "python child": lambda: subprocess.run(
        [sys.executable, "-c", f"print(open({SECRET!r}).read())"], capture_output=True, text=True).stdout,
    "keys": lambda: " ".join(k for k in os.environ if "KEY" in k or "DATABASE_URL" in k and "TEST" not in k),
    "socket": lambda: socket.create_connection(("1.1.1.1", 53), timeout=3) and "MARKER connected",
    "dns": lambda: socket.gethostbyname("example.com") and "MARKER resolved",
    "http child": lambda: subprocess.run(
        [sys.executable, "-c", "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3); print('MARKER')"],
        capture_output=True, text=True).stdout,
    "write out": lambda: open(OUTSIDE, "w").write("MARKER") and "MARKER wrote",
    "delete out": lambda: os.remove(OUTSIDE) or "MARKER removed",
}


def test_try_everything():
    got = {}
    for name, fn in TRIES.items():
        try:
            got[name] = str(fn() or "")
        except Exception as e:
            got[name] = f"refused {type(e).__name__}"
    # the channel back: a file in the temp folder the runner allows
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(f"{k}\t{v!r}" for k, v in got.items()))
'''


@pytest.mark.skipif(sys.platform != "win32", reason="the L2 runner is Windows only")
def test_a_hostile_test_gets_nothing_under_the_real_runner(tmp_path: Path, monkeypatch):
    # a throwaway worktree on purpose: the test under attack must not see the repo's own conftest
    wt, tmp = tmp_path / "wt", tmp_path / "tmp"
    (wt / "tests").mkdir(parents=True)
    tmp.mkdir()
    (wt / "tests" / "test_hostile.py").write_text(HOSTILE, encoding="utf-8")
    secret = tmp_path / ".env"
    secret.write_text("KEY=MARKER", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("still here", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "MARKER")
    env = {**commands.child_env(worktree=wt, tmp=tmp, test_db_url="postgresql://x@127.0.0.1:1/x", python=sys.executable),
           "H_SECRET": str(secret), "H_OUT": str(tmp / "out.txt"), "H_OUTSIDE": str(outside)}
    res = JobRunner(tmp_root=tmp_path / "jobs").run(
                           [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-o", "addopts=",
                           "--rootdir", str(wt), "tests/test_hostile.py"], cwd=wt, env=env, wall_s=120.0)
    assert res.exit_code == 0, res.output[-1500:]
    got = (tmp / "out.txt").read_text(encoding="utf-8")
    assert "MARKER" not in got, got
    assert outside.read_text(encoding="utf-8") == "still here"
    assert not list(wt.rglob("stolen*")) or "MARKER" not in got
    assert len(got.splitlines()) == 17
