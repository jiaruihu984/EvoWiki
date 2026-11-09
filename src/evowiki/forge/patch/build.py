"""The L2 build cascade (design §5.2; plan P3 T9). AlphaEvolve evaluation cascade, cheap checks
first, stopping at the first failing stage:

    changes -> paths (`check_change`, check 2 of 3 of §5.1) -> syntax -> diff size -> ruff
    -> secrets -> dependencies -> a new test exists -> SWE FAIL_TO_PASS / PASS_TO_PASS

The coder's claims are never trusted (DGM): everything is re-run here from the tree. `propose`
calls this on a FRESH checkout of the committed change, so stray files a test run left in the
editing worktree can neither help nor hurt the verdict. Test results come only from junit files
(`swe.evaluate`). Fail closed: a killed ruff run, a git failure or an unreadable file is a failed
stage; `SandboxUnavailable` propagates (never an unsandboxed run)."""

import ast
import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from evowiki.forge import limits
from evowiki.forge.models import Evidence, PatchBuildReport, PatchRequest, SandboxStats
from evowiki.forge.patch import commands, swe
from evowiki.forge.patch.paths import check_change, foreign_imports, reach_hits, secret_hits
from evowiki.forge.patch.workspace import PatchWorkspace
from evowiki.forge.sandbox.jobs import Runner

# operator view only (the BuildReport); never in learner text (spec §8)
NOT_HARD = "（Windows 上的测试隔离不是硬隔离：没有秘钥、只允许白名单命令。）"
RUFF_TAIL = 1500
_LOCAL_ROOTS = ("tests", "scripts")
_SKIP = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv", "node_modules"}
_NOTHING_RAN = SandboxStats(wall_s=0.0, exit="ok")


def local_names(root: Path) -> frozenset[str]:
    """Top-level names a repo test may import that are repo files, not dependencies: `tests`, and
    every module / package under `tests/` and `scripts/` (pytest's rootdir-prepend import mode and
    conftest `sys.path` inserts make e.g. `calib_common` or a sibling `base.py` importable)."""
    out: set[str] = set()
    root = Path(root)
    for top in _LOCAL_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        out.add(top)
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in _SKIP]
            out.update(d for d in dirnames if d.isidentifier())
            out.update(f[:-3] for f in filenames if f.endswith(".py") and f[:-3].isidentifier())
    return frozenset(out)


def diff_line_count(diff: str) -> int:
    """Added + removed lines of a unified diff (file headers `---` / `+++` not counted)."""
    n, in_hunk = 0, False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] in ("+", "-"):
            n += 1
    return n


def _parses(path: str, text: str) -> str:
    try:
        ast.parse(text, filename=path)
    except SyntaxError as e:
        return f"{path} 第 {e.lineno} 行：{e.msg}"
    except ValueError as e:
        return f"{path}：{e}"
    return ""


def build_patch(ws: PatchWorkspace, *, runner: Runner, python: str, test_db_url: str, tmp: Path,
                env_values: Iterable[str], declared: frozenset[str], cid: str,
                request: PatchRequest, evidence: Evidence) -> PatchBuildReport:
    """Run the cascade over `ws` (its tree against `ws.base_commit`); `commit` is left "" (the
    caller fills it). `declared` = import names of the declared dependencies; the repo's own test /
    script modules (`local_names`) are added here."""
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    root = Path(ws.root)
    state: dict = {"changed": (), "diff_lines": 0, "lint_ok": False, "sandbox": _NOTHING_RAN}

    def report(ok: bool, reason: str, **kw) -> PatchBuildReport:
        fields = {**state, **kw}
        return PatchBuildReport(ok=ok, tests_passed=fields.pop("tests_passed", 0),
                                tests_failed=fields.pop("tests_failed", 0), examples_passed=0,
                                examples_failed=0, schema_ok=True, reason=reason + NOT_HARD,
                                base_commit=ws.base_commit, **fields)

    changes = ws.changes()
    if not changes:
        return report(False, "没有改动")
    state["changed"] = tuple(c.path for c in changes)
    v = check_change(changes, base_files=ws.base_files, sim_files=ws.sim_files)
    if v:
        return report(False, "改动越界：" + "；".join(v), policy_violations=v)
    texts: dict[str, str] = {}
    for c in changes:
        if c.status != "D" and c.path.endswith(".py"):
            try:
                texts[c.path] = (root / c.path).read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError) as e:
                return report(False, f"读不了 {c.path}：{type(e).__name__}")
    syntax = [why for p, t in texts.items() if (why := _parses(p, t))]
    if syntax:
        return report(False, "语法错误：" + "；".join(syntax[:5]))
    diff = ws.git.diff(ws.base_commit)
    state["diff_lines"] = n = diff_line_count(diff)
    if n > limits.L2_MAX_DIFF_LINES:
        return report(False, f"改动太大：{n} 行，超过上限 {limits.L2_MAX_DIFF_LINES} 行")
    env = commands.child_env(worktree=root, tmp=tmp, test_db_url=test_db_url, python=python)
    if texts:
        cmd = commands.parse(["ruff", "check", *sorted(texts)], root=root, python=python,
                             junit_dir=tmp)
        ruff = runner.run(cmd.argv, cwd=root, env=env, wall_s=limits.L2_CMD_WALL_S)
        state["sandbox"] = ruff.stats
        if ruff.exit_code != 0:
            why = (f"退出码 {ruff.exit_code}" if ruff.exit_code is not None
                   else f"被终止（{ruff.stats.exit}）")
            return report(False, f"代码检查没通过（{why}）：" + ruff.output[-RUFF_TAIL:])
    state["lint_ok"] = True
    hits = secret_hits(diff, env_values)
    if hits:
        return report(False, "改动里有像秘钥的内容：" + "；".join(hits[:10]))
    reach = reach_hits(diff)
    if reach:
        return report(False, "；".join(reach), policy_violations=reach)
    foreign = foreign_imports(texts, declared=declared | local_names(root))
    if foreign:
        return report(False, "用到了没声明的依赖：" + ", ".join(foreign))
    new_tests = [c.path for c in changes if c.status == "A" and c.path.startswith("tests/")
                 and PurePosixPath(c.path).name.startswith("test_") and c.path.endswith(".py")]
    if not new_tests:
        return report(False, "没有新增测试：必须先写一个能复现问题的测试（改动前失败、改动后通过）")
    r = swe.evaluate(git=ws.git, patched_root=root, base_commit=ws.base_commit,
                     new_tests=new_tests, changed_src=[p for p in texts if p.startswith("src/")],
                     runner=runner, python=python, test_db_url=test_db_url, tmp=tmp)
    if r.stats:
        state["sandbox"] = r.stats[-1]
    return report(r.ok, r.reason, fail_to_pass=r.fail_to_pass, broken=r.broken,
                  tests_passed=len(r.fail_to_pass) + len(r.pass_to_pass) - len(r.broken),
                  tests_failed=len(r.new_failing_after) + len(r.broken))
