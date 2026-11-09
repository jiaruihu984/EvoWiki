import re

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.forge import limits
from evowiki.forge.patch.git import Git
from evowiki.forge.patch.paths import simulator_files
from evowiki.forge.patch.tools import PatchHolder, build_tools
from evowiki.forge.patch.workspace import PatchWorkspace
from evowiki.forge.sandbox import SandboxUnavailable
from tests.forge.patch.fakes import FakeRunner, outcome

SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL", re.IGNORECASE)
CTX = ToolContext(user_id="u", session_id="s")


def _holder(git_repo, tmp_path, script=()):
    g = Git(git_repo)
    base = g.head()
    ws = PatchWorkspace(root=git_repo, git=g, base_commit=base, base_files=g.ls_files(base),
                        sim_files=simulator_files(git_repo))
    tmp = tmp_path / "forge_tmp"
    tmp.mkdir()
    return PatchHolder(ws=ws, runner=FakeRunner(list(script)), python="C:/py/python.exe",
                       test_db_url="postgresql://forge@127.0.0.1:9811/forge_test", tmp=tmp)


def _tools(holder):
    return {t.spec.name: t for t in build_tools(holder)}


def test_tool_names_and_categories(git_repo, tmp_path):
    tools = _tools(_holder(git_repo, tmp_path))
    assert set(tools) == {"list_files", "read_file", "grep", "outline", "edit_file", "write_file",
                          "run_cmd", "submit"}
    for name in ("list_files", "read_file", "grep", "outline", "run_cmd"):
        assert tools[name].spec.category == "read", name
    for name in ("edit_file", "write_file"):
        assert tools[name].spec.category == "action", name


@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": ".env"}),
    ("read_file", {"path": "../x.py"}),
    ("read_file", {}),
    ("grep", {"pattern": "a("}),
    ("outline", {"path": "nope.py"}),
    ("edit_file", {"path": "src/evowiki/store/db.py", "old_string": "X = 1", "new_string": "X = 2"}),
    ("edit_file", {"path": "src/evowiki/deep/mathx.py", "old_string": "return a - b",
                   "new_string": "return (a - b"}),
    ("write_file", {"path": "tests/test_old.py", "content": "x = 1\n"}),
    ("write_file", {"path": "src/evowiki/forge/x.py", "content": "x = 1\n"}),
    ("run_cmd", {"argv": ["python", "-c", "1"]}),
    ("run_cmd", {"argv": []}),
    ("submit", {"summary": ""}),
])
def test_refused_input_is_an_error_result(git_repo, tmp_path, name, args):
    h = _holder(git_repo, tmp_path)
    r = _tools(h)[name].run(args, CTX)
    assert r.error, r
    assert h.ws.changes() == []
    assert h.runner.calls == []


def test_read_grep_outline_list_edit(git_repo, tmp_path):
    h = _holder(git_repo, tmp_path)
    t = _tools(h)
    assert "def add" in t["read_file"].run({"path": "src/evowiki/deep/mathx.py"}, CTX).content
    assert "mathx.py:2:" in t["grep"].run({"pattern": "a - b"}, CTX).content
    assert t["grep"].run({"pattern": "zzz_none"}, CTX).content == "没有结果"
    assert "1: def add" in t["outline"].run({"path": "src/evowiki/deep/mathx.py"}, CTX).content
    assert "mathx.py" in t["list_files"].run({"pattern": "src/evowiki/deep/*.py"}, CTX).content
    r = t["edit_file"].run({"path": "src/evowiki/deep/mathx.py", "old_string": "a - b",
                            "new_string": "a + b"}, CTX)
    assert not r.error
    r = t["write_file"].run({"path": "tests/forge_new/test_add.py",
                             "content": "def test_add():\n    assert 1\n"}, CTX)
    assert not r.error
    assert {c.path for c in h.ws.changes()} == {"src/evowiki/deep/mathx.py",
                                                "tests/forge_new/test_add.py"}


def test_run_cmd_refused_lists_allowlist(git_repo, tmp_path):
    r = _tools(_holder(git_repo, tmp_path))["run_cmd"].run({"argv": ["python", "-c", "1"]}, CTX)
    assert r.error and "只能运行" in r.content


def test_run_cmd_pytest_sandboxed_env_and_junit_counts(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-never-leak-0000000000")
    monkeypatch.setenv("EVOWIKI_DATABASE_URL", "postgresql://dev@127.0.0.1:15432/evowiki")
    script = [(lambda argv, cwd: "pytest" in argv,
               outcome(exit_code=1, output="screen says 99 passed"),
               {"tests/test_old.py::test_old": "passed", "tests/test_old.py::test_b": "failed",
                "tests/test_old.py::test_c": "error"})]
    h = _holder(git_repo, tmp_path, script)
    r = _tools(h)["run_cmd"].run({"argv": ["pytest", "tests/test_old.py"]}, CTX)
    assert not r.error, r.content
    assert "通过 1 / 失败 1 / 出错 1" in r.content
    (argv, cwd, env), = h.runner.calls
    assert "--disable-socket" in argv and argv[0] == "C:/py/python.exe"
    assert cwd == git_repo
    assert "DEEPSEEK_API_KEY" not in env
    assert [k for k in env if SECRET.search(k)] == ["EVOWIKI_TEST_DATABASE_URL"]
    assert env["EVOWIKI_TEST_DATABASE_URL"] == h.test_db_url
    assert all("sk-should-never-leak" not in v for v in env.values())
    assert list(h.tmp.glob("*.xml")) == []                 # the junit file is cleaned up


def test_run_cmd_missing_junit(git_repo, tmp_path):
    script = [(lambda argv, cwd: True, outcome(exit_code=4, output="ImportError boom",
                                               exit="error"), None)]
    h = _holder(git_repo, tmp_path, script)
    r = _tools(h)["run_cmd"].run({"argv": ["pytest", "tests/test_old.py"]}, CTX)
    assert "测试没能运行" in r.content and "ImportError boom" in r.content
    assert "通过" not in r.content


def test_run_cmd_output_tail_capped(git_repo, tmp_path):
    script = [(lambda argv, cwd: True, outcome(exit_code=0, output="x" * 20000 + "END"), None)]
    h = _holder(git_repo, tmp_path, script)
    r = _tools(h)["run_cmd"].run({"argv": ["git", "status"]}, CTX)
    assert not r.error and r.content.endswith("END") and len(r.content) < 4200
    (argv, _, _), = h.runner.calls
    assert argv[0] == "git" and "status" in argv


def test_run_cmd_sandbox_unavailable_fails_closed(git_repo, tmp_path):
    class Broken:
        policy = "workspace-write"

        def run(self, argv, *, cwd, env, wall_s=limits.L2_CMD_WALL_S):
            raise SandboxUnavailable("pywin32 missing")

    h = _holder(git_repo, tmp_path)
    h.runner = Broken()
    r = _tools(h)["run_cmd"].run({"argv": ["pytest", "tests/test_old.py"]}, CTX)
    assert r.error and "沙箱" in r.content


def test_run_cmd_argv_limits(git_repo, tmp_path):
    t = _tools(_holder(git_repo, tmp_path))["run_cmd"]
    assert t.run({"argv": ["pytest"] + ["-q"] * 40}, CTX).error
    assert t.run({"argv": ["pytest", "x" * 301]}, CTX).error


def test_submit_sets_holder(git_repo, tmp_path):
    h = _holder(git_repo, tmp_path)
    r = _tools(h)["submit"].run({"summary": "修好了 add，新测试证明 1+2=3"}, CTX)
    assert not r.error and h.submitted == "修好了 add，新测试证明 1+2=3"


def test_long_summary_is_metadata_and_does_not_discard_patch(git_repo, tmp_path):
    h = _holder(git_repo, tmp_path)
    summary = "改动及测试说明。" * 2000
    r = _tools(h)["submit"].run({"summary": summary}, CTX)
    assert not r.error
    assert h.submitted == summary
