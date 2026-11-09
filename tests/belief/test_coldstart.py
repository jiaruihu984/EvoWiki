import pytest

from evowiki.belief.coldstart import prior_beliefs, select_diagnostic_concepts
from evowiki.belief.theta import ThetaInfer


def test_prior_uses_domain_background(graph):
    theta = ThetaInfer.default()
    pri = prior_beliefs(graph, theta, "ml_experienced")
    assert pri["eigenvalue"].mastery == pytest.approx(0.85)          # la
    assert pri["autoencoder"].mastery == pytest.approx(0.4)          # dl
    assert pri["eigenvalue"].stability == 14.0                       # difficulty 3
    assert pri["eigenvalue"].last_recall_ts is None


def test_none_background_falls_back_to_default(graph):
    theta = ThetaInfer.default()
    pri = prior_beliefs(graph, theta, "none")
    assert pri["pca"].mastery == pytest.approx(0.3)


def test_unknown_background_raises(graph):
    with pytest.raises(KeyError):
        prior_beliefs(graph, ThetaInfer.default(), "astronaut")


def test_diagnostic_prefers_uncertain_and_central(graph):
    theta = ThetaInfer.default()
    pri = prior_beliefs(graph, theta, "none")  # equal uncertainty everywhere
    picked = select_diagnostic_concepts(pri, graph, n=2)
    # vector has the most downstream concepts, then matrix
    assert picked == ["vector", "matrix"]


def test_diagnostic_respects_exclude(graph):
    pri = prior_beliefs(graph, ThetaInfer.default(), "none")
    assert "vector" not in select_diagnostic_concepts(pri, graph, n=3, exclude={"vector"})
