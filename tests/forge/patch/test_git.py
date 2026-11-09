import os
import subprocess
from pathlib import Path

import pytest

from evowiki.forge.patch import git as gitmod
from evowiki.forge.patch.git import Git, GitError
from evowiki.forge.patch.paths import Change

AUTHOR = "EvoWiki forge <forge@evowiki.local>"


def _raw(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


def test_head_rev_parse_ls_files_show(git_repo: Path):
    g = Git(git_repo)
    h = g.head()
    assert len(h) == 40 and g.rev_parse("master") == h
    files = g.ls_files()
    assert "src/evowiki/deep/mathx.py" in files and "tests/test_old.py" in files
    assert "planted bug" in (g.show(h, "src/evowiki/deep/mathx.py") or "")
    assert g.show(h, "src/evowiki/nope.py") is None


def test_worktree_add_and_diff_raw(git_repo: Path, tmp_path: Path):
    g = Git(git_repo)
    base = g.head()
    wt = g.worktree_add(tmp_path / "wt", branch="forge/c1", base=base)
    assert wt.head() == base and g.rev_parse("forge/c1") == base
    (wt.root / "src/evowiki/deep/mathx.py").write_text("def add(a, b):\n    return a + b\n",
                                                        encoding="utf-8")
    (wt.root / "tests/forge_c1").mkdir()
    (wt.root / "tests/forge_c1/test_add.py").write_text("x = 1\n", encoding="utf-8")
    ch = sorted(wt.diff_raw(base), key=lambda c: c.path)
    assert ch == [Change("M", "src/evowiki/deep/mathx.py"),
                  Change("A", "tests/forge_c1/test_add.py")]
    g.worktree_remove(wt.root)
    assert not wt.root.exists() and g.rev_parse("forge/c1") == base     # branch kept


def test_diff_raw_delete_rename_and_commit_range(git_repo: Path):
    g = Git(git_repo)
    base = g.head()
    _raw(git_repo, "mv", "src/evowiki/deep/mathx.py", "src/evowiki/deep/mathy.py")
    (git_repo / "tests/test_old.py").unlink()
    ch = {c.status: c for c in g.diff_raw(base)}
    assert ch["R"].path == "src/evowiki/deep/mathy.py"
    assert ch["R"].old_path == "src/evowiki/deep/mathx.py"
    assert ch["D"].path == "tests/test_old.py"
    g.add_all()
    sha = g.commit("move", author=AUTHOR)
    assert {c.status for c in g.diff_raw(base, sha)} == {"R", "D"}


def test_diff_raw_unicode_and_space_paths(git_repo: Path):
    g = Git(git_repo)
    base = g.head()
    p = git_repo / "src/evowiki/deep/数 学 x.py"
    p.write_text("x = 1\n", encoding="utf-8")
    assert g.diff_raw(base) == [Change("A", "src/evowiki/deep/数 学 x.py")]


def test_diff_raw_marks_untracked_symlink(git_repo: Path):
    g = Git(git_repo)
    base = g.head()
    link = git_repo / "src/evowiki/deep/l.py"
    try:
        os.symlink(git_repo / "src/evowiki/store/db.py", link)
    except OSError:
        pytest.skip("no symlink privilege")
    ch = g.diff_raw(base)
    assert [c.new_mode for c in ch if c.path == "src/evowiki/deep/l.py"] == ["120000"]


def test_diff_raw_marks_file_under_linked_dir(git_repo: Path, tmp_path: Path):
    g = Git(git_repo)
    base = g.head()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = git_repo / "src/evowiki/deep/sub"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("no symlink privilege")
    (outside / "a.py").write_text("x = 1\n", encoding="utf-8")
    ch = g.diff_raw(base)
    # either git reports the link itself, or every file below it is marked as a link: never a
    # plain 100644 add through a linked directory
    assert ch and all(c.new_mode == "120000" for c in ch if c.path.startswith("src/evowiki/deep/sub"))


def test_diff_includes_untracked_and_passes_safe_flags(git_repo: Path, monkeypatch):
    g = Git(git_repo)
    base = g.head()
    (git_repo / "src/evowiki/deep/mathx.py").write_text("def add(a, b):\n    return a + b\n",
                                                         encoding="utf-8")
    (git_repo / "src/evowiki/deep/new.py").write_text("SECRETLINE = 1\n", encoding="utf-8")
    seen: list[tuple[str, ...]] = []
    real = Git.run

    def spy(self, *args, **kw):
        seen.append(args)
        return real(self, *args, **kw)

    monkeypatch.setattr(Git, "run", spy)
    d = g.diff(base)
    assert "+    return a + b" in d and "+SECRETLINE = 1" in d
    diffs = [a for a in seen if a and a[0] == "diff"]
    assert diffs and all("--no-ext-diff" in a and "--no-textconv" in a for a in diffs)


def test_commit_uses_author_and_hooks_never_run(git_repo: Path):
    hooks = git_repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = git_repo.parent / "hook-ran"
    (hooks / "pre-commit").write_text(f"#!/bin/sh\necho x > '{marker.as_posix()}'\nexit 1\n",
                                      encoding="utf-8")
    (hooks / "pre-commit").chmod(0o755)
    g = Git(git_repo)
    (git_repo / "src/evowiki/deep/mathx.py").write_text("x = 2\n", encoding="utf-8")
    g.add_all()
    sha = g.commit("fix add\n\nwhy: it subtracted", author=AUTHOR)
    assert not marker.exists()
    assert _raw(git_repo, "log", "-1", "--format=%an <%ae>").strip() == AUTHOR
    assert sha == g.head()
    assert g.is_clean()


def test_cherry_pick_conflict_returns_false_and_tree_clean(git_repo: Path, tmp_path: Path):
    g = Git(git_repo)
    base = g.head()
    wt = g.worktree_add(tmp_path / "wt", branch="side", base=base)
    (wt.root / "src/evowiki/deep/mathx.py").write_text("side\n", encoding="utf-8")
    wt.add_all()
    side = wt.commit("side", author=AUTHOR)
    (git_repo / "src/evowiki/deep/mathx.py").write_text("main\n", encoding="utf-8")
    g.add_all()
    main = g.commit("main", author=AUTHOR)
    assert g.cherry_pick(side) is False
    assert g.is_clean() and g.head() == main


def test_cherry_pick_ok(git_repo: Path, tmp_path: Path):
    g = Git(git_repo)
    base = g.head()
    wt = g.worktree_add(tmp_path / "wt", branch="side", base=base)
    (wt.root / "src/evowiki/deep/other.py").write_text("y = 1\n", encoding="utf-8")
    wt.add_all()
    side = wt.commit("side", author=AUTHOR)
    assert g.cherry_pick(side) is True
    assert g.head() != base and (git_repo / "src/evowiki/deep/other.py").exists()


def test_revert_restores_parent_tree_and_commits_since(git_repo: Path):
    g = Git(git_repo)
    base = g.head()
    (git_repo / "src/evowiki/deep/mathx.py").write_text("x = 2\n", encoding="utf-8")
    g.add_all()
    c1 = g.commit("c1", author=AUTHOR)
    r = g.revert(c1, "撤回 c1", author=AUTHOR)
    assert r is not None
    assert _raw(git_repo, "rev-parse", f"{r}^{{tree}}") == _raw(git_repo, "rev-parse",
                                                                 f"{base}^{{tree}}")
    assert _raw(git_repo, "log", "-1", "--format=%B").strip() == "撤回 c1"
    assert _raw(git_repo, "log", "-1", "--format=%an <%ae>").strip() == AUTHOR
    assert [m.strip() for _, m in g.commits_since(base)] == ["c1", "撤回 c1"]
    assert [s for s, _ in g.commits_since(base)] == [c1, r]
    assert g.merge_base(r, base) == base


def test_revert_conflict_returns_none(git_repo: Path):
    g = Git(git_repo)
    (git_repo / "src/evowiki/deep/mathx.py").write_text("one\n", encoding="utf-8")
    g.add_all()
    c1 = g.commit("c1", author=AUTHOR)
    (git_repo / "src/evowiki/deep/mathx.py").write_text("two\n", encoding="utf-8")
    g.add_all()
    c2 = g.commit("c2", author=AUTHOR)
    assert g.revert(c1, "undo", author=AUTHOR) is None
    assert g.is_clean() and g.head() == c2


def test_reset_hard_grep_is_clean(git_repo: Path):
    g = Git(git_repo)
    base = g.head()
    (git_repo / "src/evowiki/deep/mathx.py").write_text("x\n", encoding="utf-8")
    assert not g.is_clean()
    g.reset_hard(base)
    assert g.is_clean()
    out = g.grep("planted")
    assert "src/evowiki/deep/mathx.py" in out
    assert g.grep("no-such-text-anywhere") == ""
    many = "\n".join(f"hit_{i} = 1" for i in range(100))
    (git_repo / "src/evowiki/deep/many.py").write_text(many, encoding="utf-8")
    assert len(g.grep("hit_", max_hits=5).splitlines()) <= 6


def test_git_failure_raises(git_repo: Path):
    with pytest.raises(GitError):
        Git(git_repo).rev_parse("no-such-ref")


@pytest.mark.parametrize("bad", ["-x", "--output=/tmp/x", "", "a\nb", "a\0b"])
def test_option_injection_refused(git_repo: Path, bad: str):
    g = Git(git_repo)
    with pytest.raises(GitError):
        g.rev_parse(bad)
    with pytest.raises(GitError):
        g.show(bad, "x")
    with pytest.raises(GitError):
        g.cherry_pick(bad)


def test_every_call_is_argv_without_shell(git_repo: Path, monkeypatch, tmp_path: Path):
    calls: list[tuple[object, dict]] = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        calls.append((cmd, kw))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(gitmod.subprocess, "run", spy)
    g = Git(git_repo)
    base = g.head()
    (git_repo / "src/evowiki/deep/new.py").write_text("x = 1\n", encoding="utf-8")
    g.diff_raw(base)
    g.diff(base)
    g.add_all()
    g.commit("c", author=AUTHOR)
    g.grep("x")
    g.is_clean()
    assert calls
    for cmd, kw in calls:
        assert isinstance(cmd, list) and all(isinstance(x, str) for x in cmd)
        assert cmd[0] == "git"
        assert not kw.get("shell")
        assert "core.hooksPath=" + os.devnull in cmd
        env = kw.get("env") or {}
        assert not any(k in env for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"))


def test_inherited_git_env_is_dropped(git_repo: Path, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "idx"))
    assert len(Git(git_repo).head()) == 40


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_diff_raw_marks_files_under_a_junction(git_repo: Path, tmp_path: Path):
    import _winapi

    g = Git(git_repo)
    base = g.head()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_text("x = 1\n", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(git_repo / "src/evowiki/deep/sub"))
    ch = [c for c in g.diff_raw(base) if c.path.startswith("src/evowiki/deep/sub")]
    assert ch and all(c.new_mode == "120000" for c in ch)
