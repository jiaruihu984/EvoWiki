from pathlib import Path

from evowiki.forge import limits
from evowiki.forge.models import SandboxStats
from evowiki.forge.patch import swe
from evowiki.forge.patch.git import Git
from evowiki.forge.sandbox.jobs import ProcOutcome
from tests.forge.patch.fakes import FakeRunner

OK = ProcOutcome(exit_code=0, output="", stats=SandboxStats(wall_s=1.0, exit="ok"), peak_memory=None)
BAD = ProcOutcome(exit_code=1, output="", stats=SandboxStats(wall_s=1.0, exit="error"),
                  peak_memory=None)
NEW = "tests/forge_fc1/test_add.py::test_add"
OLD = "tests/test_old.py::test_old"
DB = "postgresql://127.0.0.1:1/x"


def _setup(git_repo: Path):
    g = Git(git_repo)
    base = g.head()
    (git_repo / "tests/forge_fc1").mkdir(parents=True)
    (git_repo / "tests/forge_fc1/__init__.py").write_text("")
    (git_repo / "tests/forge_fc1/test_add.py").write_text(
        "from evowiki.deep.mathx import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (git_repo / "src/evowiki/deep/mathx.py").write_text("def add(a, b):\n    return a + b\n")
    return g, base


def _run(git_repo, tmp_path, base_res, patch_res, new_tests=("tests/forge_fc1/test_add.py",),
         base_outcome=BAD):
    g, base = _setup(git_repo)
    runner = FakeRunner(script=[                  # the base run happens in a separate worktree
        (lambda a, cwd: "swe-base" in cwd.name, base_outcome, base_res),
        (lambda a, cwd: True, OK, patch_res)])
    r = swe.evaluate(git=g, patched_root=git_repo, base_commit=base, new_tests=list(new_tests),
                     changed_src=["src/evowiki/deep/mathx.py"], runner=runner,
                     python="py", test_db_url=DB, tmp=tmp_path)
    return r, runner


def test_fail_to_pass_ok(git_repo, tmp_path):
    r, _ = _run(git_repo, tmp_path, {NEW: "failed", OLD: "passed"}, {NEW: "passed", OLD: "passed"})
    assert r.ok and r.fail_to_pass == (NEW,) and OLD in r.pass_to_pass and r.broken == ()
    assert len(r.stats) == 2 and r.reason


def test_new_test_already_passing_on_base_is_not_f2p(git_repo, tmp_path):
    r, _ = _run(git_repo, tmp_path, {NEW: "passed", OLD: "passed"}, {NEW: "passed", OLD: "passed"})
    assert not r.ok and "改动前就能通过" in r.reason


def test_broken_old_test(git_repo, tmp_path):
    r, _ = _run(git_repo, tmp_path, {NEW: "failed", OLD: "passed"}, {NEW: "passed", OLD: "failed"})
    assert not r.ok and r.broken == (OLD,)


def test_old_test_missing_on_patch_is_broken(git_repo, tmp_path):
    r, _ = _run(git_repo, tmp_path, {NEW: "failed", OLD: "passed"}, {NEW: "passed"})
    assert not r.ok and r.broken == (OLD,)


def test_preexisting_failure_is_not_p2p(git_repo, tmp_path):
    r, _ = _run(git_repo, tmp_path, {NEW: "failed", OLD: "failed"}, {NEW: "passed", OLD: "failed"})
    assert r.ok and OLD not in r.pass_to_pass


def test_new_test_missing_on_base_counts_as_failing(git_repo, tmp_path):
    r, _ = _run(git_repo, tmp_path, {OLD: "passed"}, {NEW: "passed", OLD: "passed"})
    assert r.ok and r.fail_to_pass == (NEW,)


def test_new_test_not_passing_after(git_repo, tmp_path):
    r, _ = _run(git_repo, tmp_path, {NEW: "failed"}, {NEW: "error"})
    assert not r.ok and r.new_failing_after == (NEW,) and "新测试没全部通过" in r.reason


def test_new_test_not_run_after(git_repo, tmp_path):
    r, _ = _run(git_repo, tmp_path, {NEW: "failed"}, {OLD: "passed"})
    assert not r.ok and r.fail_to_pass == () and r.new_failing_after == (NEW,)


def test_missing_junit_fails_closed(git_repo, tmp_path):
    r, _ = _run(git_repo, tmp_path, None, {NEW: "passed"})
    assert not r.ok and "测试没能运行" in r.reason


def test_killed_run_fails_closed(git_repo, tmp_path):
    killed = ProcOutcome(exit_code=None, output="",
                         stats=SandboxStats(wall_s=600.0, exit="timeout"), peak_memory=None)
    r, _ = _run(git_repo, tmp_path, {NEW: "failed", OLD: "passed"}, {NEW: "passed", OLD: "passed"},
                base_outcome=killed)
    assert not r.ok and "测试没能运行" in r.reason


def test_base_worktree_removed(git_repo, tmp_path):
    _run(git_repo, tmp_path, {NEW: "failed", OLD: "passed"}, {NEW: "passed", OLD: "passed"})
    g = Git(git_repo)
    assert "swe-base" not in g.run("worktree", "list")
    assert "swe-base" not in g.run("branch", "--list")


def test_base_run_gets_the_test_patch_and_the_old_source(git_repo, tmp_path):
    seen = {}

    def base_match(argv, cwd):
        if "swe-base" not in cwd.name:
            return False
        seen["test"] = (cwd / "tests/forge_fc1/test_add.py").exists()
        seen["init"] = (cwd / "tests/forge_fc1/__init__.py").exists()
        seen["src"] = (cwd / "src/evowiki/deep/mathx.py").read_text()
        return True

    g, base = _setup(git_repo)
    runner = FakeRunner(script=[(base_match, BAD, {NEW: "failed", OLD: "passed"}),
                                (lambda a, cwd: True, OK, {NEW: "passed", OLD: "passed"})])
    r = swe.evaluate(git=g, patched_root=git_repo, base_commit=base,
                     new_tests=["tests/forge_fc1/test_add.py"],
                     changed_src=["src/evowiki/deep/mathx.py"], runner=runner, python="py",
                     test_db_url=DB, tmp=tmp_path)
    assert r.ok and seen["test"] and seen["init"] and "planted bug" in seen["src"]
    argv, cwd, env = runner.calls[1]
    assert cwd == git_repo and "tests/forge_fc1/test_add.py" in argv
    assert any(a.startswith("--junitxml=") for a in argv)
    assert env["EVOWIKI_TEST_DATABASE_URL"] == DB


def test_no_new_tests_refused_without_running(git_repo, tmp_path):
    r, runner = _run(git_repo, tmp_path, {}, {}, new_tests=())
    assert not r.ok and "没有新增测试" in r.reason and runner.calls == []


def test_new_test_outside_tests_refused(git_repo, tmp_path):
    r, runner = _run(git_repo, tmp_path, {}, {}, new_tests=("../evil/test_x.py",))
    assert not r.ok and runner.calls == []


def test_too_many_selected_files_narrow_to_the_changed_packages():
    files = ["tests/assistant/test_a.py", "tests/bot/test_b.py", "tests/wiki/test_c.py", "tests/zz/test_d.py"]
    got = swe.nearest(files, ["src/evowiki/bot/actions.py"], 2)
    assert "tests/bot/test_b.py" in got and len(got) == 2
    assert swe.nearest(files, ["src/evowiki/bot/actions.py"], 9) == tuple(files)


def test_select_p2p_includes_importers_and_smoke(git_repo):
    (git_repo / "tests/test_uses.py").write_text("from evowiki.deep import mathx\n")
    sel = swe.select_p2p(git_repo, ["src/evowiki/deep/mathx.py"], [])
    assert "tests/test_uses.py" in sel and "tests/test_old.py" not in sel


def test_select_p2p_adds_existing_smoke_tests_and_skips_new_tests(git_repo):
    smoke = git_repo / limits.SMOKE_TESTS[0]
    smoke.parent.mkdir(parents=True, exist_ok=True)
    smoke.write_text("def test_s():\n    pass\n")
    (git_repo / "tests/test_uses.py").write_text("from evowiki.deep import mathx\n")
    sel = swe.select_p2p(git_repo, ["src/evowiki/deep/mathx.py"], ["tests/test_uses.py"])
    assert limits.SMOKE_TESTS[0] in sel and "tests/test_uses.py" not in sel


def test_a_test_that_could_not_import_before_the_patch_counts_as_failing(git_repo, tmp_path):
    r, _ = _run(git_repo, tmp_path, {"tests/forge_fc1/test_add.py::tests.forge_fc1.test_add": "error", OLD: "passed"},
             {NEW: "passed", OLD: "passed"})
    assert r.ok and r.fail_to_pass == (NEW,)
