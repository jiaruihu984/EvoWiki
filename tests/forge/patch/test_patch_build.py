"""The L2 build cascade (plan P3 T9): one case per stage; each stops there and runs nothing later."""
from pathlib import Path

from evowiki.forge import limits
from evowiki.forge.models import Evidence, PatchBuildReport, PatchRequest
from evowiki.forge.patch.build import NOT_HARD, build_patch, local_names
from evowiki.forge.patch.git import Git
from evowiki.forge.patch.paths import simulator_files
from evowiki.forge.patch.workspace import PatchWorkspace
from tests.forge.patch.fakes import FakeRunner, outcome

NEW = "tests/forge_fc1/test_add.py::test_add"
OLD = "tests/test_old.py::test_old"
DB = "postgresql://forge@127.0.0.1:9811/forge_test"
REQ = PatchRequest(goal="add 结果不对", title="修好加法", failing_case="add(1,2) 返回 -1")
EVI = Evidence(citations=("signal:1",))
FIX = "def add(a, b):\n    return a + b\n"
TEST = "from evowiki.deep.mathx import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def _is_ruff(argv, cwd):
    return "ruff" in argv


def _is_base(argv, cwd):
    return "swe-base" in cwd.name


def _ws(root: Path) -> PatchWorkspace:
    g = Git(root)
    base = g.head()
    return PatchWorkspace(root=root, git=g, base_commit=base, base_files=g.ls_files(base),
                          sim_files=simulator_files(root))


def _put(root: Path, rel: str, text: str) -> None:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")          # same line endings as the git_repo fixture


def _fix_and_test(root: Path) -> None:
    _put(root, "src/evowiki/deep/mathx.py", FIX)
    _put(root, "tests/forge_fc1/__init__.py", "")
    _put(root, "tests/forge_fc1/test_add.py", TEST)


def _build(root, tmp_path, script, env_values=(), declared=frozenset(), ws=None
           ) -> tuple[PatchBuildReport, FakeRunner]:
    runner = FakeRunner(list(script))
    r = build_patch(ws or _ws(root), runner=runner, python="py", test_db_url=DB, tmp=tmp_path / "t",
                    env_values=env_values, declared=declared, cid="fc_1", request=REQ,
                    evidence=EVI)
    return r, runner


RUFF_OK = (_is_ruff, outcome(), None)
SWE_OK = [(_is_base, outcome(1, exit="error"), {NEW: "failed", OLD: "passed"}),
          (lambda a, c: True, outcome(), {NEW: "passed", OLD: "passed"})]


def _assert_failed(r: PatchBuildReport, words: str) -> None:
    assert not r.ok and words in r.reason and r.reason.endswith(NOT_HARD)
    assert r.examples_passed == r.examples_failed == 0 and r.schema_ok


def test_no_change(git_repo, tmp_path):
    r, runner = _build(git_repo, tmp_path, [])
    _assert_failed(r, "没有改动")
    assert runner.calls == []


def test_denied_path(git_repo, tmp_path):
    _put(git_repo, "src/evowiki/store/db.py", "X = 2\n")
    r, runner = _build(git_repo, tmp_path, [])
    _assert_failed(r, "改动越界")
    assert r.policy_violations and "store/db.py" in r.policy_violations[0]
    assert runner.calls == []


def test_old_test_edit_is_denied(git_repo, tmp_path):
    _put(git_repo, "tests/test_old.py", "def test_old():\n    assert 1\n")
    r, runner = _build(git_repo, tmp_path, [])
    _assert_failed(r, "改动越界")
    assert runner.calls == []


def test_syntax(git_repo, tmp_path):
    ws = _ws(git_repo)                       # sim_files are the base's (as in propose)
    _put(git_repo, "src/evowiki/deep/mathx.py", "def add(a, b:\n")
    r, runner = _build(git_repo, tmp_path, [], ws=ws)
    _assert_failed(r, "语法错误")
    assert "src/evowiki/deep/mathx.py" in r.reason and runner.calls == []


def test_diff_too_large(git_repo, tmp_path):
    _put(git_repo, "src/evowiki/deep/big.py",
         "".join(f"X{i} = {i}\n" for i in range(limits.L2_MAX_DIFF_LINES + 1)))
    r, runner = _build(git_repo, tmp_path, [])
    _assert_failed(r, "改动太大")
    assert r.diff_lines == limits.L2_MAX_DIFF_LINES + 1 and runner.calls == []


def test_ruff_fail(git_repo, tmp_path):
    _fix_and_test(git_repo)
    r, runner = _build(git_repo, tmp_path, [(_is_ruff, outcome(1, "F401 unused", exit="error"),
                                             None)])
    _assert_failed(r, "代码检查没通过")
    assert "F401 unused" in r.reason and r.lint_ok is False
    assert len(runner.calls) == 1
    argv, cwd, env = runner.calls[0]
    assert argv[1:4] == ("-m", "ruff", "check") and "src/evowiki/deep/mathx.py" in argv
    assert cwd == git_repo and "EVOWIKI_TEST_DATABASE_URL" in env


def test_ruff_killed_fails_closed(git_repo, tmp_path):
    _fix_and_test(git_repo)
    r, _ = _build(git_repo, tmp_path, [(_is_ruff, outcome(None, exit="timeout"), None)])
    _assert_failed(r, "代码检查没通过")


def test_secret_pattern(git_repo, tmp_path):
    _fix_and_test(git_repo)
    _put(git_repo, "src/evowiki/deep/mathx.py", FIX + 'K = "sk-abcdefghijklmnopqrstuvwxyz"\n')
    r, runner = _build(git_repo, tmp_path, [RUFF_OK])
    _assert_failed(r, "像秘钥")
    assert "sk-abc" not in r.reason and len(runner.calls) == 1


def test_env_value(git_repo, tmp_path):
    _fix_and_test(git_repo)
    _put(git_repo, "src/evowiki/deep/mathx.py", FIX + 'NOTE = "plainvalue-xyz-123"\n')
    r, runner = _build(git_repo, tmp_path, [RUFF_OK], env_values=("plainvalue-xyz-123",))
    _assert_failed(r, "像秘钥")
    assert "plainvalue" not in r.reason and len(runner.calls) == 1


def test_foreign_import(git_repo, tmp_path):
    _fix_and_test(git_repo)
    _put(git_repo, "src/evowiki/deep/mathx.py", "import requests_x\n\n\n" + FIX)
    r, runner = _build(git_repo, tmp_path, [RUFF_OK], declared=frozenset({"pydantic"}))
    _assert_failed(r, "没声明的依赖")
    assert "requests_x" in r.reason and len(runner.calls) == 1


def test_declared_and_local_test_helpers_are_not_foreign(git_repo, tmp_path):
    _fix_and_test(git_repo)
    _put(git_repo, "tests/forge_fc1/helpers_fc1.py", "Z = 1\n")
    _put(git_repo, "tests/forge_fc1/test_add.py", "import helpers_fc1\nimport pydantic\n" + TEST)
    r, _ = _build(git_repo, tmp_path, [RUFF_OK, *SWE_OK], declared=frozenset({"pydantic"}))
    assert r.ok, r.reason


def test_local_names(git_repo):
    _put(git_repo, "scripts/calibration/calib_common.py", "")
    _put(git_repo, "tests/calibration/base.py", "")
    names = local_names(git_repo)
    assert {"calib_common", "base", "tests", "test_old"} <= names


def test_no_new_test(git_repo, tmp_path):
    _put(git_repo, "src/evowiki/deep/mathx.py", FIX)
    r, runner = _build(git_repo, tmp_path, [RUFF_OK])
    _assert_failed(r, "没有新增测试")
    assert len(runner.calls) == 1


def test_swe_not_ok(git_repo, tmp_path):
    _fix_and_test(git_repo)
    r, runner = _build(git_repo, tmp_path, [
        RUFF_OK, (_is_base, outcome(), {NEW: "passed", OLD: "passed"}),
        (lambda a, c: True, outcome(), {NEW: "passed", OLD: "passed"})])
    _assert_failed(r, "改动前就能通过")
    assert len(runner.calls) == 3 and r.lint_ok


def test_swe_broken_counts(git_repo, tmp_path):
    _fix_and_test(git_repo)
    r, _ = _build(git_repo, tmp_path, [
        RUFF_OK, (_is_base, outcome(1, exit="error"), {NEW: "failed", OLD: "passed"}),
        (lambda a, c: True, outcome(1, exit="error"), {NEW: "passed", OLD: "failed"})])
    _assert_failed(r, "改坏了已有测试")
    assert r.broken == (OLD,) and r.tests_passed == 1 and r.tests_failed == 1


def test_all_ok(git_repo, tmp_path):
    _fix_and_test(git_repo)
    base = Git(git_repo).head()
    r, runner = _build(git_repo, tmp_path, [RUFF_OK, *SWE_OK])
    assert r.ok and r.lint_ok and r.schema_ok, r.reason
    assert r.fail_to_pass == (NEW,) and r.broken == ()
    assert r.tests_passed == 2 and r.tests_failed == 0
    assert r.examples_passed == r.examples_failed == 0
    assert set(r.changed) == {"src/evowiki/deep/mathx.py", "tests/forge_fc1/__init__.py",
                              "tests/forge_fc1/test_add.py"}
    assert r.diff_lines == 2 + 5 and r.base_commit == base and r.commit == ""
    assert r.sandbox.exit == "ok" and r.reason.endswith(NOT_HARD)
    assert len(runner.calls) == 3
