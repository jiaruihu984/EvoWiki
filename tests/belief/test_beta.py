from datetime import datetime, timedelta
from math import exp, sqrt

import pytest
from pydantic import ValidationError

from evowiki.belief.beta import Belief, predict_correct
from evowiki.belief.theta import ThetaInfer


def test_mastery_and_uncertainty():
    b = Belief(alpha=3.0, beta=1.0, stability=10.0, last_recall_ts=None)
    assert b.mastery == pytest.approx(0.75)
    # Var = ab / ((a+b)^2 (a+b+1)) = 3 / (16*5)
    assert b.uncertainty == pytest.approx(sqrt(3 / 80))


def test_recall_without_history_is_one(t0):
    assert Belief(alpha=1, beta=1, stability=5, last_recall_ts=None).recall(t0) == 1.0


def test_recall_decays_exponentially(t0):
    b = Belief(alpha=1, beta=1, stability=10.0, last_recall_ts=t0)
    assert b.recall(t0 + timedelta(days=5)) == pytest.approx(exp(-0.5))


def test_more_evidence_lowers_uncertainty():
    assert Belief(alpha=8, beta=2, stability=5, last_recall_ts=None).uncertainty < \
        Belief(alpha=4, beta=1, stability=5, last_recall_ts=None).uncertainty


def test_predict_single_concept(t0):
    theta = ThetaInfer.default()  # slip 0.1, guess 0.2
    b = Belief(alpha=3, beta=1, stability=10, last_recall_ts=None)
    assert predict_correct([b], t0, theta) == pytest.approx(0.9 * 0.75 + 0.2 * 0.25)


def test_predict_conjunctive(t0):
    theta = ThetaInfer.default()
    b1 = Belief(alpha=3, beta=1, stability=10, last_recall_ts=None)
    b2 = Belief(alpha=1, beta=1, stability=10, last_recall_ts=None)
    q = 0.75 * 0.5
    assert predict_correct([b1, b2], t0, theta) == pytest.approx(0.9 * q + 0.2 * (1 - q))


def test_belief_is_immutable():
    b = Belief(alpha=1, beta=1, stability=5, last_recall_ts=None)
    with pytest.raises(ValidationError):
        b.alpha = 2.0


def test_belief_rejects_naive_last_recall_ts():
    with pytest.raises(ValidationError):
        Belief(alpha=1, beta=1, stability=5, last_recall_ts=datetime(2026, 10, 1))  # noqa: DTZ001
