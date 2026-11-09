from types import SimpleNamespace

from evowiki.harness import evalset_redlines
from evowiki.harness.diff import PackDiff
from evowiki.harness.improver import ImproverRun
from evowiki.harness.pack import EMPTY


def result(passed, dollars, seconds, calls):
    return {"cases": {"quiz": {"trials": [passed], "failures": []}},
            "cost": {"product_usd": dollars, "seconds": seconds, "calls": calls,
                     "judge": {"cost_usd": 0.02, "calls": 2}}}


def run_state():
    run = object.__new__(ImproverRun)
    run.eval_runs = {}
    run.eval_usage = {}
    run.evals_used = 0
    run.candidates = {"general": SimpleNamespace(diff=PackDiff()),
                      "specific": SimpleNamespace(diff=PackDiff())}
    rows = iter([result(False, 0.10, 5.0, 3), result(True, 0.30, 9.0, 6),
                 result(False, 0.40, 12.0, 8)])
    run.deps = SimpleNamespace(evals=lambda diff: next(rows))
    return run


def test_run_evals_shows_cost_seconds_and_calls_for_baseline_and_candidate():
    run = run_state()
    base = run.run_evals({"candidate": "current"})
    assert "$0.1200" in base and "5.0 秒" in base and "5 次调用" in base
    candidate = run.run_evals({"candidate": "general"})
    assert "$0.3200" in candidate and "9.0 秒" in candidate and "8 次调用" in candidate
    assert "quiz｜T（×）" in candidate
    assert run.run_evals({"candidate": "general"}) == candidate
    assert run.evals_used == 2


def test_pre_submission_board_shows_each_candidates_own_cost():
    board = run_state()._board()
    for dollars, seconds, calls in [("0.1200", "5.0", 5), ("0.3200", "9.0", 8), ("0.4200", "12.0", 10)]:
        assert f"${dollars}" in board and f"{seconds} 秒" in board and f"{calls} 次调用" in board
    assert "quiz｜×｜T｜×" in board


def test_harness_evals_preserves_cost_metadata_without_running_subprocess(monkeypatch):
    row = result(True, 0.3, 9, 6)
    monkeypatch.setattr(evalset_redlines, "_runner", lambda *a: lambda *a: row)
    evaluate = evalset_redlines.harness_evals({"EVOWIKI_EVALSET_PYTHON": "fake-python"})
    assert evaluate(PackDiff(), EMPTY) == row


def test_unknown_usage_is_visible_and_zero_usage_is_not_unknown():
    run = run_state()
    run.deps.evals = lambda diff: {"cases": {}, "cost": {}}
    text = run.run_evals({"candidate": "current"})
    assert "未知（产品＋裁判），未知 秒，未知 次调用" in text
    run.deps.evals = lambda diff: result(False, 0, 0, 0) | {"cost": {
        "product_usd": 0, "seconds": 0, "calls": 0, "judge": {"cost_usd": 0, "calls": 0}}}
    text = run.run_evals({"candidate": "general"})
    assert "$0.0000" in text and "0.0 秒" in text and "0 次调用" in text


def test_remeasured_candidate_replaces_its_cost_and_baseline_stays_cached():
    run = run_state()
    run.run_evals({"candidate": "general"})
    run.eval_runs.pop("general")
    text = run.run_evals({"candidate": "general"})
    assert "$0.4200" in text and "$0.3200" not in text
    assert run.eval_usage["current"]["product_usd"] == 0.1
    assert run.evals_used == 3
