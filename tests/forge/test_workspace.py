import pytest

from evowiki.forge.workspace import Workspace, WorkspaceError


def test_edit_requires_a_unique_match():
    ws = Workspace.of({"tool.py": "a = 1\na = 1\n"})
    with pytest.raises(WorkspaceError, match="2 处"):
        ws.edit("tool.py", "a = 1", "a = 2")
    assert ws.edit("tool.py", "a = 1", "a = 2", replace_all=True).files["tool.py"] == "a = 2\na = 2\n"


def test_edit_missing_text_is_an_error():
    with pytest.raises(WorkspaceError, match="找不到"):
        Workspace.of({"tool.py": "x"}).edit("tool.py", "y", "z")


def test_syntax_breaking_edit_is_refused_and_file_unchanged():
    ws = Workspace.of({"tool.py": "def run(a):\n    return {}\n"})
    with pytest.raises(WorkspaceError, match="语法"):
        ws.edit("tool.py", "return {}", "return {")
    assert ws.files["tool.py"].endswith("return {}\n")


def test_names_and_caps():
    with pytest.raises(WorkspaceError):
        Workspace.of({}).write("../x.py", "")
    with pytest.raises(WorkspaceError):
        Workspace.of({}).write("data.bin", "")
    with pytest.raises(WorkspaceError, match="1 MB"):
        Workspace.of({}).write("tool.py", "#" * ((1 << 20) + 1))


def test_file_count_cap(monkeypatch):
    monkeypatch.setattr("evowiki.forge.limits.WS_MAX_FILES", 2)
    ws = Workspace.of({"a.py": "", "b.py": ""})
    with pytest.raises(WorkspaceError, match="文件数"):
        ws.write("c.py", "")
    assert ws.write("a.py", "x = 1").files["a.py"] == "x = 1"


def test_workspace_is_immutable():
    ws = Workspace.of({"a.py": "x = 1"})
    ws2 = ws.write("b.py", "")
    assert "b.py" not in ws.files and "b.py" in ws2.files
    with pytest.raises(TypeError):
        ws.files["c.py"] = ""  # type: ignore[index]


def test_read_window_has_line_numbers():
    ws = Workspace.of({"tool.py": "\n".join(f"l{i}" for i in range(1, 11))})
    assert ws.read("tool.py", offset=3, limit=2) == "     3\tl3\n     4\tl4"


def test_read_cuts_long_lines_and_missing_file_errors():
    ws = Workspace.of({"tool.py": "x" * 900})
    assert len(ws.read("tool.py").split("\t")[1]) == 500
    with pytest.raises(WorkspaceError):
        ws.read("nope.py")


def test_list_and_grep():
    ws = Workspace.of({"a.py": "x = 1\n", "schema.json": "{}"})
    assert ws.list("*.py") == "a.py"
    assert ws.list("*.md") == "没有结果"
    assert ws.grep("x") == "a.py:1: x = 1"
    with pytest.raises(WorkspaceError, match="正则"):
        ws.grep("(")


def test_grep_caps_and_says_empty():
    ws = Workspace.of({"a.py": "x = 1\n" * 200})
    assert ws.grep("x").count("\n") <= 50
    assert ws.grep("nothing") == "没有结果"
