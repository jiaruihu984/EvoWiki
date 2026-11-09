"""The cases as Inspect AI tasks: one task per category (its own log, its own k), a solver that drives the
real backend, a scorer that applies graders.py to the trial's record. `model="none/none"`: Inspect
calls no model here, the product does. A later `inspect score` / `rescore()` re-grades the log's
records without touching the backend.

The records go in `state.store`; pass^k is the reducer over the epochs."""

from pathlib import Path

import anyio
from inspect_ai import Epochs, Task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import CORRECT, INCORRECT, Score, accuracy, score_reducer, scorer
from inspect_ai.solver import solver
from psycopg_pool import ConnectionPool

from evowiki.wikiapp.repo import PgRepo
from tests.llm_cases.evalset import driver, graders
from tests.llm_cases.evalset.spec import CATEGORIES, Case

RECORD = "evalset_record"


@score_reducer(name="pass_k")
def pass_k():
    """pass^k: a case passes only when every repetition passed (τ-bench)."""
    def reduce(scores: list[Score]) -> Score:
        ok = all(s.value == CORRECT for s in scores)
        return Score(value=CORRECT if ok else INCORRECT,
                     metadata={"trials": [s.value for s in scores],
                               "category": scores[0].metadata["category"],
                               "kind": scores[0].metadata["kind"]},
                     explanation="\n".join(s.explanation or "" for s in scores if s.value != CORRECT))
    return reduce


def trial_score(case: Case, rec: dict, judge) -> Score:
    try:
        checks = graders.grade(case, rec, judge)
    except Exception as exc:  # noqa: BLE001 - a judge outage is no verdict on the case
        checks = [graders.Check("harness", False, f"判分出错：{type(exc).__name__}: {exc}"[:400], undecided=True)]
    ok = graders.passed(checks)
    return Score(value=CORRECT if ok else INCORRECT, answer=graders.last_answer(rec)[:2000],
                 explanation="; ".join(f"{c.grader}: {c.basis}" for c in checks if not c.ok),
                 metadata={"category": case.category, "kind": case.kind, "trial": rec["trial"],
                           "checks": graders.as_dicts(checks), "user": rec["user"],
                           "undecided": any(c.undecided for c in checks)})


@scorer(metrics=[accuracy()])
def evalset_grader(cases: dict[str, Case], judge):
    async def score(state, target):
        rec = state.store.get(RECORD)
        case = cases[state.sample_id]
        return await anyio.to_thread.run_sync(trial_score, case, rec, judge)
    return score


@solver
def drive(cases: dict[str, Case], base_url: str, dsn: str, faults: Path, run_id: str):
    def one(case: Case, n: int) -> dict:
        with ConnectionPool(dsn, min_size=1, max_size=4, open=True,
                            kwargs={"autocommit": True}) as pool, pool.connection() as conn:
            return driver.Trial(base_url, PgRepo(pool), conn, faults, case, run_id, n).run()

    async def solve(state, generate):
        case = cases[state.sample_id]
        rec = await anyio.to_thread.run_sync(one, case, state.epoch)
        state.store.set(RECORD, rec)
        state.output.completion = graders.last_answer(rec)
        return state
    return solve


def task_for(category: str, cases: list[Case], *, base_url: str, dsn: str, faults: Path, run_id: str,
             judge, trials: int | None = None) -> Task:
    _, k = CATEGORIES[category]
    k = trials if trials is not None else k
    by = {c.id: c for c in cases}
    return Task(
        name=f"evalset_{category}",
        dataset=[Sample(id=c.id, input=c.steps[0].get("say") or str(c.steps[0]),
                        metadata={"kind": c.kind, "topic": c.topic}) for c in cases],
        solver=drive(by, base_url, dsn, faults, run_id),
        scorer=evalset_grader(by, judge),
        epochs=Epochs(k, pass_k()),
        message_limit=None,
    )
