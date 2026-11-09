import os
import sys
from pathlib import Path

import pytest

from evowiki.forge.patch.commands import ALLOWED_TEXT, CmdRefused, child_env, parse, pytest_argv


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "tests" / "deep").mkdir(parents=True)
    (tmp_path / "tests" / "deep" / "test_a.py").write_text("def test_a():\n    pass\n")
    (tmp_path / "src").mkdir()
    return tmp_path


def P(root, *argv):
    return parse(list(argv), root=root, python=sys.executable, junit_dir=root / ".junit")


def test_pytest_is_wrapped(root):
    c = P(root, "pytest", "tests/deep/test_a.py::test_a", "-x", "-k", "a and not b")
    assert c.kind == "pytest" and c.argv[:3] == (sys.executable, "-m", "pytest")
    assert "--disable-socket" in c.argv and any(a.startswith("--allow-hosts=127.0.0.1") for a in c.argv)
    assert any(a.startswith("--junitxml=") for a in c.argv) and "no:cacheprovider" in c.argv
    assert c.argv[-1] == "tests/deep/test_a.py::test_a"            # targets last, flags before them
    assert c.argv[-4:-1] == ("-x", "-k", "a and not b")
    assert c.junit is not None and c.junit.parent == root / ".junit"
    assert f"--junitxml={c.junit}" in c.argv and "junit_family=xunit1" in c.argv


def test_pytest_without_targets_runs_tests_dir(root):
    c = P(root, "pytest", "-q")
    assert c.argv[-1] == "tests"


def test_each_pytest_run_gets_its_own_junit(root):
    assert P(root, "pytest").junit != P(root, "pytest").junit


@pytest.mark.parametrize("argv", [
    ["python", "-c", "print(1)"], ["cmd", "/c", "dir"], ["powershell"], ["pytest", "-p", "evil"],
    ["pytest", "--rootdir", "C:/"], ["pytest", "-c", "x.ini"], ["pytest", "-o", "addopts=x"],
    ["pytest", "../outside/test_x.py"], ["pytest", "src/evowiki/deep"], ["pytest", "C:/x/test.py"],
    ["ruff", "check", "--fix", "src"], ["ruff", "format", "src"],
    ["git", "push"], ["git", "config", "x"], ["git", "-c", "a=b", "log"], ["git", "diff", "--ext-diff"],
    ["pytest", "-k", "a\nb"], [],
    # beyond the plan's list: backslash and drive tricks, option smuggling, the junit flag itself
    ["pytest", "tests\\deep\\test_a.py"], ["pytest", "tests/../tests/deep/test_a.py"],
    ["pytest", "tests/deep/missing.py"], ["pytest", "--junitxml=C:/x.xml"], ["pytest", "-k"],
    ["pytest", "--basetemp=C:/"], ["pytest", "tests/deep/test_a.py", "--", "-p", "evil"],
    ["ruff", "check", "../x"], ["ruff", "check", "--config", "x.toml", "src"], ["ruff"],
    ["git", "log", "-n", "500"], ["git", "log", "-n", "x"], ["git", "log", "--output=x.txt"],
    ["git", "diff", "HEAD", "--output=C:/x"], ["git", "status", "--porcelain=v2", "-uall"],
    ["git"], ["git", "diff", "--no-index", "C:/a", "C:/b"], ["PYTEST"], ["pytest.exe"],
    ["uv", "run", "pytest"],
])
def test_everything_else_refused(root, argv):
    with pytest.raises(CmdRefused) as e:
        P(root, *argv)
    assert "pytest" in str(e.value) and "ruff check" in str(e.value)   # the message lists the allowlist


def test_git_read_only_subcommands(root):
    for argv in (["git", "diff"], ["git", "diff", "--stat"], ["git", "status", "--short"],
                 ["git", "log", "--oneline", "-n", "5"], ["git", "diff", "--name-only", "src"]):
        c = P(root, *argv)
        assert c.kind == "git" and c.junit is None
        assert c.argv[:4] == ("git", "--no-pager", "-c", "core.fsmonitor=false")
        assert c.argv[4] == argv[1] and "--" in c.argv
        if argv[1] == "diff":
            assert "--no-ext-diff" in c.argv and "--no-textconv" in c.argv


def test_ruff_check_only_inside_the_repo(root):
    c = P(root, "ruff", "check", "src", "tests/deep/test_a.py")
    assert c.kind == "ruff"
    assert c.argv == (sys.executable, "-m", "ruff", "check", "--no-cache", "src", "tests/deep/test_a.py")
    assert P(root, "ruff", "check").argv[-1] == "--no-cache"


def test_pytest_argv_for_swe():
    argv = pytest_argv(["tests/a.py::t"], python="py", junit=Path("j.xml"))
    assert argv[:3] == ("py", "-m", "pytest") and argv[-1] == "tests/a.py::t"
    assert "--disable-socket" in argv and "--junitxml=j.xml" in argv


def test_refusal_text_names_the_allowlist():
    assert "pytest" in ALLOWED_TEXT and "ruff check" in ALLOWED_TEXT and "git diff" in ALLOWED_TEXT


def test_child_env_has_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x" * 30)
    monkeypatch.setenv("EVOWIKI_DATABASE_URL", "postgresql://dev")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y" * 30)
    monkeypatch.setenv("TAVILY_API_KEY", "z" * 30)
    monkeypatch.setenv("GITHUB_TOKEN", "t" * 30)
    monkeypatch.setenv("PGPASSWORD", "p" * 10)
    monkeypatch.setenv("PYTHONPATH", "C:/parent/src")
    monkeypatch.setenv("VIRTUAL_ENV", "C:/parent/.venv")
    env = child_env(worktree=tmp_path, tmp=tmp_path / "t", test_db_url="postgresql://127.0.0.1:1/x",
                    python=sys.executable)
    bad = [k for k in env if any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
           or (k.upper().endswith("DATABASE_URL") and k != "EVOWIKI_TEST_DATABASE_URL")]
    assert bad == []
    assert env["PYTHONPATH"].split(";")[1] == str(tmp_path / "src")
    assert env["EVOWIKI_EMBEDDER"] == "hash" and env["PYTHONNOUSERSITE"] == "1"
    assert env["EVOWIKI_TEST_DATABASE_URL"] == "postgresql://127.0.0.1:1/x"
    assert "C:/parent" not in "".join(env.values())                 # nothing inherited but the allowlist
    for secret in ("x" * 30, "y" * 30, "z" * 30, "t" * 30, "p" * 10, "postgresql://dev"):
        assert all(secret not in v for v in env.values())


def test_child_env_keys_are_a_fixed_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMETHING_RANDOM", "1")
    env = child_env(worktree=tmp_path, tmp=tmp_path, test_db_url="postgresql://127.0.0.1:1/x",
                    python=sys.executable)
    assert "SOMETHING_RANDOM" not in env
    assert env["TEMP"] == env["TMP"] == env["USERPROFILE"] == str(tmp_path)
    path_dirs = env["PATH"].split(os.pathsep)
    assert path_dirs[0] == str(Path(sys.executable).parent)
