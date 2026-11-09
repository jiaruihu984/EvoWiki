import os

import pytest

from evowiki.forge import limits
from evowiki.forge.patch.git import Git
from evowiki.forge.patch.paths import simulator_files
from evowiki.forge.patch.workspace import PatchWorkspace, PatchWorkspaceError


@pytest.fixture
def ws(git_repo):
    g = Git(git_repo)
    base = g.head()
    return PatchWorkspace(root=git_repo, git=g, base_commit=base, base_files=g.ls_files(base),
                          sim_files=simulator_files(git_repo))


def test_edit_allowed_file(ws):
    ws.edit("src/evowiki/deep/mathx.py", "a - b", "a + b")
    assert "a + b" in (ws.root / "src/evowiki/deep/mathx.py").read_text()
    assert [c.path for c in ws.changes()] == ["src/evowiki/deep/mathx.py"]


@pytest.mark.parametrize("path", ["src/evowiki/store/db.py", "tests/test_old.py",
                                  "src/evowiki/content/shared.py", "pyproject.toml",
                                  "../escape.py", "C:/x.py", ".env", "src/evowiki/forge/x.py"])
def test_write_refused(ws, path):
    with pytest.raises(PatchWorkspaceError):
        ws.write(path, "x = 1\n")


@pytest.mark.parametrize("path", ["/abs.py", "src\\evowiki\\deep\\x.py", "src/evowiki/deep/../store/db.py",
                                  "tests/TEST_OLD.py", "src/evowiki/deep/.git/x.py", ""])
def test_write_refused_odd_paths(ws, path):
    with pytest.raises(PatchWorkspaceError):
        ws.write(path, "x = 1\n")
    assert ws.changes() == []


def test_edit_refused_on_denied_file_leaves_it(ws):
    before = (ws.root / "src/evowiki/store/db.py").read_text()
    with pytest.raises(PatchWorkspaceError):
        ws.edit("src/evowiki/store/db.py", "X = 1", "X = 2")
    assert (ws.root / "src/evowiki/store/db.py").read_text() == before


def test_new_test_file_allowed(ws):
    ws.write("tests/forge_new/test_add.py", "def test_add():\n    assert 1\n")
    assert (ws.root / "tests/forge_new/test_add.py").exists()


def test_new_file_then_overwrite_and_read(ws):
    ws.write("src/evowiki/deep/newmod.py", "A = 1\n")
    ws.write("src/evowiki/deep/newmod.py", "A = 2\n")
    assert "A = 2" in ws.read("src/evowiki/deep/newmod.py")
    assert {c.path for c in ws.changes()} == {"src/evowiki/deep/newmod.py"}


def test_syntax_error_refused_file_unchanged(ws):
    before = (ws.root / "src/evowiki/deep/mathx.py").read_text()
    with pytest.raises(PatchWorkspaceError) as e:
        ws.edit("src/evowiki/deep/mathx.py", "return a - b", "return (a - b")
    assert "语法错误" in str(e.value)
    assert (ws.root / "src/evowiki/deep/mathx.py").read_text() == before


def test_syntax_error_on_new_file_not_created(ws):
    with pytest.raises(PatchWorkspaceError):
        ws.write("src/evowiki/deep/bad.py", "def (:\n")
    assert not (ws.root / "src/evowiki/deep/bad.py").exists()


def test_edit_uniqueness_rules(ws):
    ws.write("src/evowiki/deep/dup.py", "x = 1\nx = 1\n")
    with pytest.raises(PatchWorkspaceError, match="出现了 2 处"):
        ws.edit("src/evowiki/deep/dup.py", "x = 1", "x = 2")
    with pytest.raises(PatchWorkspaceError, match="找不到"):
        ws.edit("src/evowiki/deep/dup.py", "y = 1", "x = 2")
    with pytest.raises(PatchWorkspaceError, match="相同"):
        ws.edit("src/evowiki/deep/dup.py", "x = 1", "x = 1")
    with pytest.raises(PatchWorkspaceError, match="没有文件"):
        ws.edit("src/evowiki/deep/none.py", "x", "y")
    ws.edit("src/evowiki/deep/dup.py", "x = 1", "x = 2", replace_all=True)
    assert (ws.root / "src/evowiki/deep/dup.py").read_text() == "x = 2\nx = 2\n"


def test_crlf_preserved(ws):
    p = ws.root / "src/evowiki/deep/crlf.py"
    ws.write("src/evowiki/deep/crlf.py", "a = 1\r\nb = 2\r\n")
    ws.edit("src/evowiki/deep/crlf.py", "b = 2", "b = 3")
    assert p.read_bytes() == b"a = 1\r\nb = 3\r\n"


def test_gitignored_path_refused(ws):
    """Ignored files are invisible to `git diff` (so to the build check): never write one."""
    (ws.root / ".gitignore").write_text("*.pyc\nbuild/\n")
    for p in ("src/evowiki/deep/mathx.pyc", "src/evowiki/deep/build/x.py"):
        with pytest.raises(PatchWorkspaceError, match="忽略"):
            ws.write(p, "x = 1\n")
        assert not (ws.root / p).exists()


def test_too_many_changed_files_refused(ws):
    for i in range(limits.L2_MAX_CHANGED_FILES):
        ws.write(f"src/evowiki/deep/m{i}.py", "x = 1\n")
    with pytest.raises(PatchWorkspaceError, match="上限"):
        ws.write("src/evowiki/deep/one_more.py", "x = 1\n")
    ws.write("src/evowiki/deep/m0.py", "x = 2\n")          # re-editing a changed file is fine


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_write_through_junction_refused(ws, tmp_path):
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(ws.root / "src/evowiki/deep/jn"))
    with pytest.raises(PatchWorkspaceError):
        ws.write("src/evowiki/deep/jn/x.py", "x = 1\n")
    assert not (outside / "x.py").exists()
    with pytest.raises(PatchWorkspaceError):
        ws.read("src/evowiki/deep/jn/x.py")


def test_read_untracked_secret_refused(ws):
    (ws.root / ".env").write_text("K=v\n")
    with pytest.raises(PatchWorkspaceError):
        ws.read(".env")


@pytest.mark.parametrize("path", [".git/config", "../repo/src/evowiki/store/db.py", "C:/Windows/win.ini"])
def test_read_outside_refused(ws, path):
    with pytest.raises(PatchWorkspaceError):
        ws.read(path)


def test_read_window(ws):
    out = ws.read("src/evowiki/deep/mathx.py", offset=2, limit=1)
    assert out == "     2\t    return a - b   # planted bug"
    with pytest.raises(PatchWorkspaceError):
        ws.read("src/evowiki/deep/mathx.py", offset=9)


def test_read_tracked_denied_file_allowed(ws):
    assert "X = 1" in ws.read("src/evowiki/store/db.py")


def test_list(ws):
    ws.write("src/evowiki/deep/newmod.py", "A = 1\n")
    out = ws.list("src/evowiki/deep/*.py").splitlines()
    assert "src/evowiki/deep/mathx.py" in out and "src/evowiki/deep/newmod.py" in out
    (ws.root / "stray.py").write_text("x = 1\n")
    assert "stray.py" not in ws.list("*.py")
    assert ws.list("*.nothing") == "没有结果"


def test_grep_and_empty(ws):
    assert "mathx.py:2:" in ws.grep("a - b")
    assert ws.grep("zzz_nothing") == "没有结果"


def test_grep_skips_untracked_secrets(ws):
    (ws.root / ".env").write_text("DEEPSEEK_API_KEY=supersecretvalue\n")
    assert ws.grep("supersecret") == "没有结果"
    ws.write("src/evowiki/deep/newmod.py", "MARK = 'supersecret'\n")
    assert "newmod.py:1:" in ws.grep("supersecret")


def test_grep_bad_regex(ws):
    with pytest.raises(PatchWorkspaceError, match="正则"):
        ws.grep("a(")


def test_grep_capped(ws):
    ws.write("src/evowiki/deep/many.py", "".join(f"v{i} = 'hit'\n" for i in range(80)))
    out = ws.grep("hit").splitlines()
    assert len(out) == 51 and "80" in out[-1]


def test_outline(ws):
    assert "1: def add" in ws.outline("src/evowiki/deep/mathx.py")
    ws.write("src/evowiki/deep/k.py", "class K:\n    def m(self, x):\n        pass\n\n"
                                       "async def g():\n    pass\n")
    out = ws.outline("src/evowiki/deep/k.py")
    assert "1: class K" in out and "    2: def m(self, x)" in out and "5: async def g()" in out
