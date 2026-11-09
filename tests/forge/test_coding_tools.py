import json

from evowiki.agent.tools import ToolContext
from evowiki.forge.coding_tools import WorkspaceHolder, build_tools
from evowiki.forge.workspace import Workspace
from tests.forge.fakes import InProcessSandbox

CTX = ToolContext(user_id="u", session_id="s")
TOOL = "def run(args):\n    return {'n': args['n'] * 2}\n"
SCHEMA = json.dumps({"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]})
TESTS = ("import tool\ndef test_ok():\n    assert tool.run({'n': 2}) == {'n': 4}\n"
         "def test_bad():\n    assert False, 'boom'\n")


def setup(files=None):
    h = WorkspaceHolder(ws=Workspace.of(files or {"tool.py": TOOL, "schema.json": SCHEMA,
                                                 "test_tool.py": TESTS}), sandbox=InProcessSandbox())
    return h, {t.spec.name: t for t in build_tools(h)}


def test_categories():
    _, tools = setup()
    cats = {n: t.spec.category for n, t in tools.items()}
    assert {cats[n] for n in ("list_files", "read_file", "grep", "edit_file", "write_file")} == {"draft"}
    assert cats["run_tests"] == cats["try_tool"] == "read"


def test_read_edit_write_roundtrip():
    h, t = setup()
    assert "return" in t["read_file"].run({"path": "tool.py"}, CTX).content
    r = t["edit_file"].run({"path": "tool.py", "old_string": "* 2", "new_string": "* 3"}, CTX)
    assert r.error is None and "* 3" in h.ws.files["tool.py"]
    t["write_file"].run({"path": "README.md", "content": "hi"}, CTX)
    assert h.ws.files["README.md"] == "hi"
    assert "tool.py" in t["list_files"].run({"pattern": "*.py"}, CTX).content
    assert t["grep"].run({"pattern": "zzz"}, CTX).content == "没有结果"


def test_edit_error_leaves_workspace_unchanged():
    h, t = setup()
    before = h.ws
    r = t["edit_file"].run({"path": "tool.py", "old_string": "return {", "new_string": "return {{"}, CTX)
    assert r.error and "语法" in r.error and h.ws is before
    r = t["edit_file"].run({"path": "tool.py", "old_string": "nope", "new_string": "x"}, CTX)
    assert r.error and "找不到" in r.error and h.ws is before


def test_run_tests_reports_failures():
    _, t = setup()
    r = t["run_tests"].run({}, CTX)
    assert "通过 1 / 失败 1" in r.content and "test_bad" in r.content and "boom" in r.content
    assert len(r.content) <= 4100


def test_try_tool_validates_then_runs():
    _, t = setup()
    bad = t["try_tool"].run({"args": {"n": "x"}}, CTX)
    assert bad.error and "n" in bad.error
    good = t["try_tool"].run({"args": {"n": 2}}, CTX)
    assert good.error is None and json.loads(good.content) == {"n": 4}
    _, t = setup({"tool.py": "def run(a):\n    raise ValueError('x')\n", "schema.json": "{}"})
    assert t["try_tool"].run({"args": {}}, CTX).error


def test_try_tool_without_schema_errors():
    _, t = setup({"tool.py": TOOL})
    assert t["try_tool"].run({"args": {}}, CTX).error


def test_submit_sets_holder():
    h, t = setup()
    t["submit"].run({"summary": "done"}, CTX)
    assert h.submitted == "done"
