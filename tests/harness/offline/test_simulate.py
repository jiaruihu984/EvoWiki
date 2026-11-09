"""The simulation's headline claims hold at a small Monte Carlo size (fixed seeds)."""

from dataclasses import replace

from evowiki.harness.offline.simulate import FULL, OLD, Scenario, accept_rate, calibration_pass

NULL = Scenario("null", 0.0, 0.0)
GOOD = Scenario("+0.6", 0.6, 0.6)
OVERFIT = Scenario("dev only", 0.6, -0.2)


def test_the_old_gate_accepts_nothing_changes_far_more_often_than_the_new_rule():
    assert accept_rate(OLD, NULL, sims=400) > 0.04
    assert accept_rate(FULL, NULL, sims=400) < 0.02


def test_the_new_rule_finds_a_clear_improvement_and_the_old_gate_mostly_misses_it():
    assert accept_rate(FULL, GOOD, sims=400) > 0.7
    assert accept_rate(OLD, GOOD, sims=400) < 0.4


def test_the_held_out_step_is_what_stops_a_change_that_only_fits_the_dev_topics():
    assert accept_rate(FULL, OVERFIT, sims=400) < 0.05
    assert accept_rate(replace(FULL, held_low=None), OVERFIT, sims=400) > 0.4


def test_calibration_on_40_pairs_separates_a_weak_judge_from_a_good_one():
    assert calibration_pass(0.6) < 0.05 and calibration_pass(0.9) > 0.99


def test_fit_recovers_a_noise_model_from_its_own_aa_shares():
    from evowiki.harness.offline.simulate import Model, aa_shares, fit
    m = Model(judge_noise=0.8, tie_band=0.5, position=0.3)
    got = fit(*aa_shares(m))
    assert abs(got.tie_band - 0.5) <= 0.2 and abs(got.position - 0.3) <= 0.2
    assert abs(got.judge_noise - 0.8) <= 0.4


def test_the_length_rule_voiding_most_wins_leaves_no_power():
    from evowiki.harness.offline.simulate import MEASURED, MEASURED_CUT
    assert accept_rate(FULL, Scenario("+0.6", 0.6, 0.6, MEASURED), sims=400) > 0.7
    assert accept_rate(FULL, Scenario("+0.6", 0.6, 0.6, MEASURED_CUT), sims=400) < 0.05
