import subprocess
from pathlib import Path

import check_merge as script
import pytest


@pytest.mark.parametrize("code", [0, 7])
def test_pytest_uses_fresh_directory_and_cleans_it(monkeypatch, code):
    calls = fake_git(monkeypatch, code=code)
    original = script.subprocess.run
    paths = []

    def run(cmd, **kwargs):
        if "pytest" in cmd:
            path = Path(next(c.split("=", 1)[1] for c in cmd if c.startswith("--basetemp=")))
            assert path.is_absolute()
            assert path.is_dir()
            (path / "leftover").write_text("test", encoding="utf-8")
            paths.append(path)
        return original(cmd, **kwargs)

    monkeypatch.setattr(script.subprocess, "run", run)
    for _ in range(2):
        assert script.main([]) == int(code != 0)
    assert calls
    assert len(set(paths)) == 2
    assert all(not path.exists() for path in paths)


def test_pytest_temp_is_cleaned_when_process_cannot_start(monkeypatch):
    fake_git(monkeypatch)
    original = script.subprocess.run
    paths = []

    def run(cmd, **kwargs):
        if "pytest" in cmd:
            paths.append(Path(next(c.split("=", 1)[1] for c in cmd if c.startswith("--basetemp="))))
            raise OSError("cannot start pytest")
        return original(cmd, **kwargs)

    monkeypatch.setattr(script.subprocess, "run", run)
    assert script.main([]) == 1
    assert paths and all(not path.exists() for path in paths)


def fake_git(monkeypatch, changed=(), base_migrations=(), code=0, untracked=()):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        out = ""
        if cmd[1:3] == ["diff", "--name-only"]:
            out = "\0".join(changed)
        elif cmd[1:3] == ["ls-tree", "-r"]:
            out = "\0".join(base_migrations)
        elif cmd[1:3] == ["ls-files", "--others"]:
            out = "\0".join(untracked)
        return subprocess.CompletedProcess(cmd, code if cmd[0] != "git" else 0, out)

    monkeypatch.setattr(script.subprocess, "run", run)
    return calls


def test_always_checks_and_default_base(monkeypatch):
    calls = fake_git(monkeypatch)
    assert script.main([]) == 0
    commands = [c for c, _ in calls]
    assert any("origin/master" in c for c in commands)
    assert any("ruff" in c for c in commands)
    assert any("tests/simulator/test_import_boundary.py" in c for c in commands)


@pytest.mark.parametrize("area", ["assistant", "goals", "wikiapp", "deep", "forge", "harness"])
def test_routes_backend_changes(monkeypatch, area):
    calls = fake_git(monkeypatch, [f"src/evowiki/{area}/contracts.py"])
    assert script.main([]) == 0
    assert any(f"tests/{area}" in c for c, _ in calls)
    assert any("--check" in c for c, _ in calls)


def test_plugin_checks_and_failure(monkeypatch):
    calls = fake_git(monkeypatch, ["obsidian-plugin/src/a.ts"], code=7)
    assert script.main([]) != 0
    assert any("vitest" in c for c, _ in calls)
    assert any("tsc" in c for c, _ in calls)
    assert all(k["env"]["npm_config_offline"] == "true" for c, k in calls if "npx" in c[0])


@pytest.mark.parametrize("path", [
    "src/evowiki/experiments/a.py", "src/evowiki/rsib/a.py", "rsib/a.py",
    "src/evowiki/simulator/a.py", "src/evowiki/content/models.py",
    "src/evowiki/evidence/models.py", "src/evowiki/timeutil.py",
])
def test_protected_changes_fail(monkeypatch, path):
    fake_git(monkeypatch, [path])
    assert script.main([]) != 0


def test_new_migrations_continue_after_base_despite_historical_gaps(monkeypatch):
    base = ["src/evowiki/store/migrations/0001_a.sql", "src/evowiki/store/migrations/0113_b.sql"]
    fake_git(monkeypatch, ["src/evowiki/store/migrations/0115_c.sql"], base)
    assert script.main([]) != 0
    fake_git(monkeypatch, ["src/evowiki/store/migrations/0114_c.sql"], base)
    assert script.main([]) == 0


def test_git_failure_is_closed(monkeypatch):
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, ""))
    assert script.main([]) != 0


def test_untracked_files_and_custom_base(monkeypatch):
    calls = fake_git(monkeypatch, untracked=["obsidian-plugin/src/new.ts"])
    assert script.main(["--base", "my-base"]) == 0
    assert any("my-base" in c for c, _ in calls)
    assert any("vitest" in c for c, _ in calls)


def test_untracked_protected_file_is_rejected(monkeypatch):
    fake_git(monkeypatch, untracked=["src/evowiki/experiments/new.py"])
    assert script.main([]) != 0


def test_missing_executable_fails_but_runs_remaining_checks(monkeypatch):
    calls = fake_git(monkeypatch, ["obsidian-plugin/src/new.ts"])
    original = script.subprocess.run

    def run(cmd, **kwargs):
        if "ruff" in cmd:
            raise FileNotFoundError("missing ruff")
        return original(cmd, **kwargs)

    monkeypatch.setattr(script.subprocess, "run", run)
    assert script.main([]) != 0
    assert any("tsc" in c for c, _ in calls)


def test_closure_follows_relative_imports_and_package_initializers(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "ROOT", tmp_path)
    files = {
        "evowiki/__init__.py": "",
        "evowiki/simulator/__init__.py": "from ..content.graph import Graph",
        "evowiki/content/__init__.py": "from .models import Graph",
        "evowiki/content/graph.py": "from .models import Graph",
        "evowiki/content/models.py": "class Graph: pass",
        "evowiki/content/unused.py": "",
    }
    for name, text in files.items():
        path = tmp_path / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert script.simulator_closure() == {f"src/{p}" for p in files if "unused" not in p}
