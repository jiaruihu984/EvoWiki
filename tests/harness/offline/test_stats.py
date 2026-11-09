"""The offline decision rule and the calibration metrics: pure functions, no model."""

import pytest

from evowiki.harness.offline.calibrate import LabelledPair, calibrate, kappa
from evowiki.harness.offline.stats import (
    EXEMPLAR_MIN,
    HELD_LOW,
    CaseResult,
    aa_check,
    ci,
    decide,
)


def r(i, pairs, exemplar=False, **kw):
    return CaseResult(f"c{i}", exemplar, tuple(pairs), **kw)


def test_ci_brackets_the_mean():
    m, lo, hi = ci([0.0, 0.5, 1.0, 1.0])
    assert lo <= m <= hi and m == pytest.approx(0.625)


def test_a_clear_dev_win_passes_and_ties_do_not():
    assert decide([r(i, "aa") for i in range(20)]).passed
    v = decide([r(i, ("tie", "tie")) for i in range(20)])
    assert not v.passed and "胜率下界" in v.reasons[0] and v.decisive == 0


def test_one_noisy_exemplar_loss_does_not_sink_a_good_change_but_a_bad_average_does():
    good = [r(i, "aa") for i in range(20)]
    assert decide([*good, r(90, "bb", exemplar=True), r(91, "aa", exemplar=True)]).passed
    v = decide([*good, r(90, "bb", exemplar=True), r(91, ("b", "tie"), exemplar=True)])
    assert not v.passed and v.exemplar_mean < EXEMPLAR_MIN


def test_held_out_asks_for_direction_not_significance():
    mixed = [r(i, ("a", "tie")) for i in range(6)] + [r(10 + i, ("tie", "tie")) for i in range(6)]
    assert decide(mixed, low_bar=HELD_LOW, mean_bar=0.5).passed
    flat = [r(i, ("tie", "tie")) for i in range(12)]
    assert not decide(flat, low_bar=HELD_LOW, mean_bar=0.5).passed


def test_rubric_drop_and_cost_flag():
    res = [r(i, "aa", rubric_cand=0.5, rubric_base=0.8, cost_cand=2.0, cost_base=1.0)
           for i in range(10)]
    v = decide(res)
    assert not v.passed and any("rubric" in x for x in v.reasons)
    assert v.cost_ratio == 2.0 and v.notes


def test_aa_check_flags_a_one_sided_pipeline():
    assert aa_check([r(i, ("a", "b")) for i in range(10)])[0]
    ok, m = aa_check([r(i, ("a", "tie")) for i in range(10)])
    assert not ok and m == 0.75


def test_kappa_and_calibration():
    assert kappa(["a", "b", "tie"], ["a", "b", "tie"]) == 1.0
    agree = [LabelledPair("a", "a", False, 100, 100) for _ in range(9)] + \
            [LabelledPair("b", "b", False, 100, 100) for _ in range(9)] + \
            [LabelledPair("tie", "tie", False, 100, 100) for _ in range(2)]
    assert calibrate(agree).passed
    longer = [LabelledPair("a", "b", False, 100, 200) for _ in range(10)] + \
             [LabelledPair("b", "b", True, 100, 200) for _ in range(10)]
    c = calibrate(longer)
    assert not c.passed and c.length_excess == pytest.approx(0.5) and c.position == 0.5
