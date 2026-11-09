import subprocess

import git_runtime
import pytest


@pytest.mark.parametrize("layout", ["ucrt64", "a-new-runtime-prefix"])
def test_runtime_is_discovered_from_gits_reported_distribution(tmp_path, monkeypatch, layout):
    root = tmp_path / "Git 中文 space" / layout
    runtime = root / "bin"
    runtime.mkdir(parents=True)
    (runtime / "libwinpthread-1.dll").write_bytes(b"runtime")
    calls = []
    monkeypatch.setattr(git_runtime.shutil, "which", lambda name: "official-git")

    def invoke(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, str(root / "libexec" / "git-core") + "\n")

    monkeypatch.setattr(git_runtime.subprocess, "run", invoke)
    assert git_runtime.git_runtime_dir() == runtime
    assert calls == [["official-git", "--exec-path"]]


def test_an_unrelated_runtime_on_path_is_not_used(tmp_path, monkeypatch):
    (tmp_path / "libwinpthread-1.dll").write_bytes(b"unrelated")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(git_runtime.shutil, "which", lambda name: None)
    assert git_runtime.git_runtime_dir() is None


def test_missing_git_runtime_or_failed_discovery_returns_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(git_runtime.shutil, "which", lambda name: "official-git")
    monkeypatch.setattr(git_runtime.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 0, str(tmp_path / "libexec" / "git-core")))
    assert git_runtime.git_runtime_dir() is None

    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["git", "--exec-path"])

    monkeypatch.setattr(git_runtime.subprocess, "run", failed)
    assert git_runtime.git_runtime_dir() is None
