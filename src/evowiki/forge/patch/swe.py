"""SWE-bench style evaluation of an L2 patch (plan P3 T7).

Follows the SWE-bench harness: the test patch (the new test files, plus any test support files the
patch adds or changes under `tests/`) is applied to the base commit in a throwaway worktree and run;
then the same tests run on the patched tree. FAIL_TO_PASS = new tests that fail, error or are
missing on the base and pass on the patch. PASS_TO_PASS = tests outside the new test files that
passed on the base (only the selected tests are run); every one must still pass on the patch.
Results come only from `--junitxml` files (`junit.read`), never from screen output or the model's
word.

Fail closed: no new tests, an unsafe test path, too wide a blast radius, a git or import-graph
failure, a killed run or a missing/unreadable junit file each refuse with a visible reason."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from evowiki.forge import limits
from evowiki.forge.models import SandboxStats
from evowiki.forge.patch import importgraph, junit
from evowiki.forge.patch.commands import child_env, pytest_argv
from evowiki.forge.patch.git import Git, GitError
from evowiki.forge.sandbox.jobs import Runner

# pytest stops the whole session on a collection error; the new test usually fails to import on the
# base, and without this flag the PASS_TO_PASS baseline would silently be empty.
_KEEP_GOING = "--continue-on-collection-errors"


@dataclass(frozen=True)
class SweReport:
    ok: bool
    fail_to_pass: tuple[str, ...]            # node ids: failed/error/missing on base, passed on patch
    new_failing_after: tuple[str, ...]       # new tests not passing on the patch
    pass_to_pass: tuple[str, ...]            # passed on base, selected
    broken: tuple[str, ...]                  # in pass_to_pass, not passed on the patch
    p2p_files: tuple[str, ...]
    stats: tuple[SandboxStats, ...]          # base run, patch run
    reason: str                              # 人话


def _no(reason: str, *, stats: tuple[SandboxStats, ...] = (),
        p2p_files: tuple[str, ...] = ()) -> SweReport:
    return SweReport(ok=False, fail_to_pass=(), new_failing_after=(), pass_to_pass=(), broken=(),
                     p2p_files=p2p_files, stats=stats, reason=reason)


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith("tests/") and name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py"))


def _safe_test_path(root: Path, rel: str) -> str | None:
    """Repo-relative POSIX path under tests/ that exists in `root`, else None."""
    if not isinstance(rel, str) or not rel or "\0" in rel:
        return None
    p = PurePosixPath(rel.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or p.parts[:1] != ("tests",) or p.suffix != ".py":
        return None
    if ":" in p.parts[0]:
        return None
    return p.as_posix() if (Path(root) / p).is_file() else None


def select_p2p(root: Path, changed: Sequence[str], new_tests: Sequence[str]) -> tuple[str, ...]:
    """Test files that import a changed module (transitively, `ast` import graph), plus every
    `limits.SMOKE_TESTS` file present in the tree; new test files are excluded. Raises
    `importgraph.GraphError` when the graph cannot be built (fail closed)."""
    root = Path(root)
    g = importgraph.graph(root)
    targets = [c.replace("\\", "/") for c in changed]
    users = importgraph.importers([t for t in targets if t in g], g)
    skip = {t.replace("\\", "/") for t in new_tests}
    smoke = {s for s in limits.SMOKE_TESTS if (root / s).is_file()}
    return tuple(sorted(f for f in (set(users) | smoke) if _is_test_file(f) and f not in skip))


def nearest(files: Sequence[str], changed: Sequence[str], limit: int) -> tuple[str, ...]:
    """At most `limit` of the selected test files, those of the changed modules' own packages first: a change to a
    module many things import selects most of the repo (455 files for the assistant's action tools, 2026-11-05),
    and refusing it outright meant such a patch could never reach the learner, who reviews every patch anyway.
    The report says how many ran."""
    if len(files) <= limit:
        return tuple(files)
    packages = {Path(c).parts[2] for c in changed if len(Path(c).parts) > 2}

    def rank(f: str) -> tuple[bool, str]:
        return (not (len(Path(f).parts) > 1 and Path(f).parts[1] in packages), f)
    return tuple(sorted(sorted(files, key=rank)[:limit]))


def _pytest(runner: Runner, root: Path, targets: Sequence[str], python: str, test_db_url: str,
            tmp: Path) -> tuple[dict[str, junit.Outcome] | None, SandboxStats]:
    path = tmp / f"swe-{uuid4().hex}.xml"
    argv = pytest_argv([_KEEP_GOING, *targets], python=python, junit=path)
    env = child_env(worktree=root, tmp=tmp, test_db_url=test_db_url, python=python)
    res = runner.run(argv, cwd=root, env=env, wall_s=limits.L2_CMD_WALL_S)
    if res.exit_code is None:                     # killed (timeout / memory / output cap)
        return None, res.stats
    try:
        return junit.read(path), res.stats
    except junit.JunitError:
        return None, res.stats


def _test_patch(patched_root: Path, base_commit: str, new_tests: Sequence[str]) -> list[str]:
    """The SWE-bench "test patch": the new test files plus every test-side file (under tests/)
    the patch adds or changes, e.g. a new package `__init__.py` or conftest."""
    changes = Git(patched_root).diff_raw(base_commit)
    extra = [c.path for c in changes if c.status != "D" and c.path.startswith("tests/")
             and (Path(patched_root) / c.path).is_file()]
    return sorted(set(new_tests) | set(extra))


def evaluate(*, git: Git, patched_root: Path, base_commit: str, new_tests: Sequence[str],
             changed_src: Sequence[str], runner: Runner, python: str, test_db_url: str,
             tmp: Path) -> SweReport:
    if not new_tests:
        return _no("没有新增测试：必须先写一个能复现问题的测试（改动前失败、改动后通过）")
    patched_root, tmp = Path(patched_root), Path(tmp)
    tests: list[str] = []
    for t in new_tests:
        safe = _safe_test_path(patched_root, t)
        if safe is None:
            return _no(f"新增测试必须是 tests/ 下已存在的 .py 文件：{t!r}")
        tests.append(safe)
    new_set = set(tests)
    try:
        p2p_files = select_p2p(patched_root, changed_src, tests)
    except importgraph.GraphError as e:
        return _no(f"无法确定相关的已有测试：{e}")
    selected = len(p2p_files)
    p2p_files = nearest(p2p_files, changed_src, limits.L2_P2P_MAX_FILES)
    targets = [*tests, *p2p_files]
    tmp.mkdir(parents=True, exist_ok=True)
    base_dir = tmp / f"swe-base-{uuid4().hex[:8]}"
    branch = f"swe-base/{base_dir.name}"
    try:
        copy = _test_patch(patched_root, base_commit, tests)
        git.worktree_add(base_dir, branch=branch, base=base_commit)
    except GitError as e:
        git.run("branch", "-D", branch, check=False)
        return _no(f"测试没能运行（无法准备改动前的代码：{e}）", p2p_files=p2p_files)
    try:
        for t in copy:                                     # SWE-bench: test patch onto the base
            dst = base_dir / t
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((patched_root / t).read_bytes())
        before, s0 = _pytest(runner, base_dir, targets, python, test_db_url, tmp)
    finally:
        try:
            git.worktree_remove(base_dir)
        finally:
            git.run("branch", "-D", branch, check=False)
    after, s1 = _pytest(runner, patched_root, targets, python, test_db_url, tmp)
    stats = (s0, s1)
    if before is None or after is None:
        return _no("测试没能运行（没有结果文件）", stats=stats, p2p_files=p2p_files)

    def in_new(n: str) -> bool:
        return n.split("::")[0] in new_set

    def import_failed(n: str) -> bool:
        """A test file that could not even be imported before the patch (the usual reproduction of
        a missing feature) is one pseudo-case named after its module, gone once it imports."""
        file, _, name = n.partition("::")
        return name == file.removesuffix(".py").replace("/", ".")

    new_ids = sorted({n for n in (*before, *after) if in_new(n) and (n in after or not import_failed(n))})
    f2p = tuple(n for n in new_ids if before.get(n, "error") in ("failed", "error")
                and after.get(n) == "passed")
    new_bad = tuple(n for n in new_ids if after.get(n) != "passed")
    p2p = tuple(sorted(n for n, o in before.items() if o == "passed" and not in_new(n)))
    broken = tuple(n for n in p2p if after.get(n) != "passed")

    if not new_ids:
        reason = "新测试没有被运行（没有找到测试用例）"
    elif new_bad:
        reason = f"新测试没全部通过：{', '.join(new_bad[:5])}"
    elif not f2p:
        reason = "新测试在改动前就能通过（没有复现问题）"
    elif broken:
        reason = f"改坏了已有测试：{', '.join(broken[:5])}"
    else:
        reason = "复现测试改动前失败、改动后通过，相关旧测试都通过"
    ok = bool(new_ids) and not new_bad and bool(f2p) and not broken
    if selected > len(p2p_files):
        reason += f"（相关的已有测试共 {selected} 个文件，只跑了最相关的 {len(p2p_files)} 个）"
    return SweReport(ok=ok, fail_to_pass=f2p, new_failing_after=new_bad, pass_to_pass=p2p,
                     broken=broken, p2p_files=p2p_files, stats=stats, reason=reason)
