import random
from math import log

import pytest

from evowiki.teach.metrics import brier, log_loss, paired_bootstrap, seed_of


def test_log_loss_soft_label():
    assert log_loss(0.8, 1.0) == pytest.approx(-log(0.8))
    assert log_loss(0.8, 0.0) == pytest.approx(-log(0.2))
    assert log_loss(0.5, 0.5) == pytest.approx(log(2))
    assert log_loss(0.7, 0.5) == pytest.approx(-(0.5 * log(0.7) + 0.5 * log(0.3)))
    assert log_loss(0.0, 1.0) < 30            # clamped, finite


def test_brier():
    assert brier(0.8, 1.0) == pytest.approx(0.04)
    assert brier(0.3, 0.5) == pytest.approx(0.04)


def test_seed_of_is_deterministic():
    assert seed_of("abc") == seed_of("abc")
    assert seed_of("abc") != seed_of("abd")
    assert isinstance(seed_of(""), int) and seed_of("x") >= 0


def test_paired_bootstrap_reproducible_and_contains_mean():
    rng = random.Random(7)
    diffs = [rng.gauss(0.05, 0.2) for _ in range(300)]
    a = paired_bootstrap(diffs, seed=seed_of("t"), n=1000)
    b = paired_bootstrap(diffs, seed=seed_of("t"), n=1000)
    assert a == b
    mean, lo, hi = a
    assert mean == pytest.approx(sum(diffs) / len(diffs))
    assert lo < mean < hi
    assert hi - lo < 0.1
    assert paired_bootstrap(diffs, seed=seed_of("u"), n=1000) != a     # the seed matters


def test_paired_bootstrap_alpha_widens_interval():
    rng = random.Random(1)
    diffs = [rng.gauss(0.0, 1.0) for _ in range(100)]
    _, lo95, hi95 = paired_bootstrap(diffs, seed=1, alpha=0.05)
    _, lo80, hi80 = paired_bootstrap(diffs, seed=1, alpha=0.2)
    assert lo95 <= lo80 and hi80 <= hi95


def test_paired_bootstrap_constant_and_errors():
    assert paired_bootstrap([0.1] * 10, seed=0, n=50) == pytest.approx((0.1, 0.1, 0.1))
    with pytest.raises(ValueError):
        paired_bootstrap([], seed=0)
    with pytest.raises(ValueError):
        paired_bootstrap([1.0], seed=0, alpha=0.0)
