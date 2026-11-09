"""An L2 child cannot read the sealed evaluation set or a .env through Python, by any of the ways a test
written by the coder would try. Runs the real child environment; no model."""

import os
import subprocess
import sys
from pathlib import Path

from evowiki.forge.patch import commands

PROBE = r"""
import os, subprocess, sys
target, secret = sys.argv[1], sys.argv[2]
for name, read in (
    ("open", lambda: open(os.path.join(target, "answers.jsonl"), encoding="utf-8").read()),
    ("list", lambda: os.listdir(target)),
    ("env", lambda: open(secret, encoding="utf-8").read()),
    ("child", lambda: subprocess.run(
        [sys.executable, "-c", "print(open(" + repr(os.path.join(target, "answers.jsonl")) + ").read())"],
        capture_output=True, text=True).stdout),
    ("shell", lambda: subprocess.run(["cmd", "/c", "type", os.path.join(target, "answers.jsonl")],
                                     capture_output=True, text=True).stdout),
):
    try:
        print(name, repr(read()))
    except PermissionError:
        print(name, "refused")
"""


def test_a_child_cannot_read_the_sealed_set_or_the_keys(tmp_path: Path):
    sealed = tmp_path / "evalset-sealed"
    sealed.mkdir()
    (sealed / "answers.jsonl").write_text("MARKER", encoding="utf-8")
    secret = tmp_path / ".env"
    secret.write_text("KEY=MARKER", encoding="utf-8")
    env = {**commands.child_env(worktree=tmp_path, tmp=tmp_path, test_db_url="x", python=sys.executable),
           "EVOWIKI_SEALED_ROOT": str(sealed)}
    run = subprocess.run([sys.executable, "-c", PROBE, str(sealed), str(secret)], env=env,
                         capture_output=True, text=True, encoding="utf-8", check=True)
    assert "MARKER" not in run.stdout
    assert [ln.split()[1] for ln in run.stdout.splitlines()[:3]] == ["refused"] * 3
    assert "shell refused" in run.stdout


def test_the_hook_comes_from_the_running_code_not_the_patched_worktree(tmp_path: Path):
    env = commands.child_env(worktree=tmp_path, tmp=tmp_path, test_db_url="x", python=sys.executable)
    first = env["PYTHONPATH"].split(os.pathsep)[0]
    assert not first.startswith(str(tmp_path)) and (Path(first) / "sitecustomize.py").is_file()
