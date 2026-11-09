"""Explicitly enabled model regression gates for proposed harness changes.

Candidates run against isolated test databases. Comparable baselines and complete traces
are required; missing evidence vetoes the change. Confirmed regressions cannot be
outvoted by a model judge. Paired trials and the existing statistical thresholds
remain unchanged. These checks establish regression evidence, not learning efficacy.

EVOWIKI_EVALSET_PYTHON explicitly opts into the paid runner. An installed interpreter
alone never enables it. EVOWIKI_EVALSET_ROOT selects the checkout; data and sealed
locations follow evalset_paths. EVOWIKI_EVALSET_ONLY may narrow relevant cases.
"""

import json
import math
import os
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evowiki.harness.diff import KeyOp, PackDiff, diff_to_json
from evowiki.harness.evalset_paths import data_dir
from evowiki.harness.evalset_relevant import Feedback, relevant
from evowiki.harness.pack import HarnessPack
from evowiki.teach.contracts import TeachingSpec

RUN_TIMEOUT_S = 3600
RETRY_S = 20
NOT_FINISHED = "红线用例没跑完"
NO_BASELINE = "红线用例没有可比的基线"
BASELINE_NOTE = "baseline"
MOVE_TRIALS = 2
REPLICATE = 2
P_BAR = 0.05


def _python(env: Mapping[str, str]) -> str | None:
    return env.get("EVOWIKI_EVALSET_PYTHON") or None


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def new_failures(run: Mapping[str, Any], base: Mapping[str, Any]) -> list[str]:
    """The cases that passed every trial in `base` and not in `run`; a no-verdict trial is neither."""
    out = []
    for cid, c in run["cases"].items():
        old = base["cases"].get(cid) or {}
        if c.get("trials") and old.get("trials") and all(old["trials"]) and not all(c["trials"]):
            out.append(cid)
    return out


def verdict(run: Mapping[str, Any], baselines: Sequence[Mapping[str, Any]]) -> list[str]:
    """`run` against the newest baseline of the same case set and grader: what vetoes the change."""
    if not (run.get("trace") or {}).get("complete"):
        return [NOT_FINISHED]
    same = [b for b in baselines if b["case_hash"] == run["case_hash"]
            and b["grader_hash"] == run["grader_hash"] and (b.get("note") or "").startswith(BASELINE_NOTE)]
    return new_failures(run, same[-1]) if same else [NO_BASELINE]


def _only(only: Sequence[str] | None, env: Mapping[str, str]) -> tuple[str, ...]:
    picked = env.get("EVOWIKI_EVALSET_ONLY") or ",".join(only or ())
    return ("--only", picked) if picked else ()


def _runner(python: str, root: str, env: Mapping[str, str]) -> Callable[..., dict[str, Any] | None]:
    history = data_dir(env) / "history"
    def run(diff_json: Mapping[str, Any], *extra: str) -> dict[str, Any] | None:
        """The run's own row (a run that wrote none, like one refused while the last run's backend
        still holds the port, gets one more try; never the row of an earlier run)."""
        note = f"harness-{uuid.uuid4().hex[:12]}"       # several rigs may be appending to the history
        for attempt in range(2):
            if attempt:
                time.sleep(RETRY_S)
            with tempfile.TemporaryDirectory() as tmp:
                file = Path(tmp) / "candidate.json"
                file.write_text(json.dumps(diff_json, ensure_ascii=False), encoding="utf-8")
                subprocess.run(
                    [python, "-m", "tests.llm_cases.evalset.run", "run", "--candidate", str(file),
                     "--lane", env.get("EVOWIKI_EVALSET_LANE", "redline"), "--port", env.get("EVOWIKI_EVALSET_PORT", "8311"),
                     "--note", note, *extra],
                    cwd=root, capture_output=True, text=True, errors="replace",
                    env={**env, "PYTHONPATH": os.pathsep.join((str(Path(root) / "src"), root))},
                    timeout=RUN_TIMEOUT_S, check=False)
            rows = [r for r in _rows(history / "candidates.jsonl") if r.get("note") == note]
            if rows:
                return rows[-1]
        return None
    return run


def harness_redlines(env: Mapping[str, str] = os.environ
                     ) -> Callable[[str, PackDiff, HarnessPack, TeachingSpec], list[str]] | None:
    python = _python(env)
    if not python:
        return None
    run = _runner(python, env.get("EVOWIKI_EVALSET_ROOT") or os.getcwd(), env)

    def check(user_id: str, diff: PackDiff, pack: HarnessPack, spec: TeachingSpec) -> list[str]:
        cand = diff_to_json(diff, {})
        first = run(cand)
        if first is None:
            return [NOT_FINISHED]
        bad = verdict(first, _rows(data_dir(env) / "history/runs.jsonl"))
        if not bad or bad in ([NOT_FINISHED], [NO_BASELINE]):
            return bad
        again = run(cand, "--only", ",".join(bad))
        return [NOT_FINISHED] if again is None else confirmed(bad, again)
    return check


def harness_patch_regression(env: Mapping[str, str] = os.environ) -> "Callable[..., Regression] | None":
    """The same comparison for a code patch: the cases on the running code, then on a worktree of the patch's branch
    (`forge/<cid>`), the checkout the set runs from. The patch's code runs as the product does, with the product's
    keys, on a rig database: the build's static checks are all that stood before it, and it runs before anyone
    approved it. No effect shown means the patch stays unproven, so this is what a learner's approval rests on."""
    python = _python(env)
    if python is None:
        return None
    root = env.get("EVOWIKI_EVALSET_ROOT") or os.getcwd()

    def check(cid: str, main_root: Path, pack: HarnessPack, only_ids: Sequence[str] | None = None) -> "Regression":
        only = _only(only_ids, env)
        now = diff_to_json(PackDiff(tuple(KeyOp(k, v) for k, v in pack.values.items())), {})
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "patched"
            subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt), f"forge/{cid}"], cwd=main_root, check=True,
                           capture_output=True)
            try:
                run, base = _runner(python, str(wt), env), _runner(python, root, env)
                before = base(now, *only)
                after = run(now, *only)
                if before is None or after is None:
                    return Regression(error=NOT_FINISHED)
                arms = {"before": base, "after": run}
                out = replicated(before, after, lambda arm, ids: arms[arm](now, "--only", ",".join(ids)))
                if not out.broken or out.error:
                    return out
                again = run(now, "--only", ",".join(out.broken))
                return Regression(error=NOT_FINISHED) if again is None else Regression(
                    out.fixed, tuple(confirmed(out.broken, again)), out.gained, out.lost, out.p)
            finally:
                subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=main_root, check=False, capture_output=True)
    return check


def harness_relevant(env: Mapping[str, str] = os.environ) -> Callable[[Feedback], tuple[str, ...]] | None:
    """The round's choice of cases; None without the evaluation set. `EVOWIKI_EVALSET_ONLY` is an experiment's
    fixed list and wins."""
    if _python(env) is None:
        return None
    fixed = tuple(i for i in env.get("EVOWIKI_EVALSET_ONLY", "").split(",") if i)
    root = Path(env.get("EVOWIKI_EVALSET_ROOT") or os.getcwd())
    return lambda fb: fixed or relevant(root, fb)


def harness_evals(env: Mapping[str, str] = os.environ
                  ) -> Callable[..., dict[str, Any] | None] | None:
    """The evaluation set as the improver's own measure (`ImproverDeps.evals`): real turns under a pack + diff, graded
    by code: `cases` (trials, failures, answers) and `cost` (product/judge USD, wall seconds, calls).
    None = a run that
    did not finish. Reading a score on dev cases and trying again is what the loops that work do (GEPA's feedback
    function, DGM's benchmark logs); the gate's evidence and the sealed set stay out of its reach."""
    python = _python(env)
    if not python:
        return None
    run = _runner(python, env.get("EVOWIKI_EVALSET_ROOT") or os.getcwd(), env)

    def evals(diff: PackDiff, pack: HarnessPack, only: Sequence[str] | None = None) -> dict[str, Any] | None:
        changed = {o.name for o in diff.ops}
        now = tuple(KeyOp(k, v) for k, v in pack.values.items() if k not in changed)
        row = run(diff_to_json(PackDiff((*now, *diff.ops), diff.spec_ops), {}), *_only(only, env))
        return None if row is None else {"cases": row["cases"], "cost": row["cost"]}
    return evals


def confirmed(new: Sequence[str], again: Mapping[str, Any]) -> list[str]:
    """The new failures that failed a trial again (a case the second run has no verdict on stays)."""
    out = []
    for cid in new:
        trials = (again["cases"].get(cid) or {}).get("trials")
        if not trials or not all(trials):
            out.append(cid)
    return out


@dataclass(frozen=True)
class Regression:
    """The candidate against the current pack on the same cases, by code (module doc)."""

    fixed: tuple[str, ...] = ()
    broken: tuple[str, ...] = ()
    gained: int = 0                 # trials gained in the cases that moved up
    lost: int = 0                   # trials lost in the cases that moved down
    p: float = 1.0
    error: str | None = None        # a run that did not finish, or runs that cannot be compared

    @property
    def restores(self) -> bool:
        return self.error is None and bool(self.fixed) and not self.broken and self.p < P_BAR


def sign_test(gained: int, lost: int) -> float:
    """One-sided exact sign test on the discordant trials."""
    n = gained + lost
    return sum(math.comb(n, k) for k in range(gained, n + 1)) / 2 ** n if n else 1.0


def compare_runs(base: Mapping[str, Any], cand: Mapping[str, Any]) -> Regression:
    if not (base.get("trace") or {}).get("complete") or not (cand.get("trace") or {}).get("complete"):
        return Regression(error=NOT_FINISHED)
    if (base["case_hash"], base["grader_hash"]) != (cand["case_hash"], cand["grader_hash"]):
        return Regression(error=NO_BASELINE)
    fixed, gained, lost = [], 0, 0
    for cid, c in cand["cases"].items():
        old = (base["cases"].get(cid) or {}).get("trials")
        new = c.get("trials")
        if not old or not new:
            continue
        move = sum(new) - sum(old)
        if move >= MOVE_TRIALS:
            fixed.append(cid)
            gained += move
        elif move <= -MOVE_TRIALS:
            lost -= move
    return Regression(fixed=tuple(fixed), broken=tuple(new_failures(cand, base)), gained=gained, lost=lost,
                      p=sign_test(gained, lost))


def _pooled(a: Mapping[str, Any], b: Mapping[str, Any] | None) -> dict[str, Any]:
    """Run `a` with the trials of its rerun `b` added, case by case."""
    if b is None:
        return dict(a)
    cases = {i: dict(c) for i, c in a["cases"].items()}
    for i, c in b["cases"].items():
        if i in cases:
            cases[i]["trials"] = [*(cases[i].get("trials") or ()), *(c.get("trials") or ())]
    return {**a, "cases": cases}


def replicated(before: Mapping[str, Any], after: Mapping[str, Any], again: Callable[[str, Sequence[str]], Any]) -> Regression:
    """`compare_runs` with more trials for the cases that moved but do not clear the bar yet (module doc).
    `again(arm, ids)`: that arm's run of these cases, None if it did not finish, which stops here."""
    out = compare_runs(before, after)
    for _ in range(REPLICATE):
        if out.error or out.broken or out.restores or not out.fixed:
            return out
        more_before, more_after = again("before", out.fixed), again("after", out.fixed)
        if more_before is None or more_after is None:
            return out
        before, after = _pooled(before, more_before), _pooled(after, more_after)
        out = compare_runs(before, after)
    return out


def harness_regression(env: Mapping[str, str] = os.environ
                       ) -> Callable[..., Regression] | None:
    python = _python(env)
    if not python:
        return None
    run = _runner(python, env.get("EVOWIKI_EVALSET_ROOT") or os.getcwd(), env)

    def check(user_id: str, diff: PackDiff, pack: HarnessPack, spec: TeachingSpec,
              only_ids: Sequence[str] | None = None) -> "Regression":
        only = _only(only_ids, env)
        now = PackDiff(tuple(KeyOp(k, v) for k, v in pack.values.items()))
        changed = {o.name for o in diff.ops}
        cand = PackDiff((*(o for o in now.ops if o.name not in changed), *diff.ops), diff.spec_ops)
        before = run(diff_to_json(now, {}), *only)
        after = run(diff_to_json(cand, {}), *only)
        if before is None or after is None:
            return Regression(error=NOT_FINISHED)
        arms = {"before": diff_to_json(now, {}), "after": diff_to_json(cand, {})}
        out = replicated(before, after, lambda arm, ids: run(arms[arm], "--only", ",".join(ids)))
        if not out.broken or out.error:
            return out
        again = run(diff_to_json(cand, {}), "--only", ",".join(out.broken))
        return Regression(error=NOT_FINISHED) if again is None else Regression(
            out.fixed, tuple(confirmed(out.broken, again)), out.gained, out.lost, out.p)
    return check
