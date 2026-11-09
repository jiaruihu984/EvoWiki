from datetime import timedelta

import pytest

from evowiki.belief.beta import Belief
from evowiki.belief.theta import ThetaInfer
from evowiki.belief.update import apply_evidence
from evowiki.evidence.models import Evidence, EvidenceType, Polarity


def _b(a=2.0, b=2.0, s=10.0, last=None):
    return Belief(alpha=a, beta=b, stability=s, last_recall_ts=last)


def _beliefs(graph, **overrides):
    out = {cid: _b() for cid in graph.concept_ids}
    out.update(overrides)
    return out


def _e(ts, cid, etype, pol, conf=1.0):
    return Evidence(id="x", source_event_id="e", extractor_version="rule-v1", concept_id=cid,
                    evidence_type=etype, polarity=pol, extraction_confidence=conf, ts=ts)


def test_quiz_correct_updates_alpha_stability_and_recall_ts(graph, t0):
    theta = ThetaInfer.default()
    res = apply_evidence(_beliefs(graph), _e(t0, "pca", EvidenceType.QUIZ_CORRECT, Polarity.POSITIVE),
                         graph, theta)
    pca = res.updated["pca"]
    assert pca.alpha == pytest.approx(3.0)
    assert pca.stability == pytest.approx(20.0)
    assert pca.last_recall_ts == t0
    assert pca.evidence_count == 1


def test_positive_propagates_to_prereqs_by_kind(graph, t0):
    theta = ThetaInfer.default()
    res = apply_evidence(_beliefs(graph), _e(t0, "pca", EvidenceType.QUIZ_CORRECT, Polarity.POSITIVE),
                         graph, theta)
    assert res.updated["eigenvalue"].alpha == pytest.approx(2.0 + 0.3)
    assert res.updated["covariance"].alpha == pytest.approx(2.0 + 0.3)
    res2 = apply_evidence(_beliefs(graph), _e(t0, "svd", EvidenceType.QUIZ_CORRECT, Polarity.POSITIVE),
                          graph, theta)
    assert res2.updated["eigenvalue"].alpha == pytest.approx(2.0 + 0.15)


def test_negative_does_not_propagate_and_marks_ambiguous(graph, t0):
    theta = ThetaInfer.default()
    before = _beliefs(graph)
    res = apply_evidence(before, _e(t0, "pca", EvidenceType.QUIZ_WRONG, Polarity.NEGATIVE), graph, theta)
    assert res.updated["eigenvalue"] == before["eigenvalue"]
    assert res.ambiguous == {"eigenvalue", "covariance"}


def test_negative_is_discounted_by_recall(graph, t0):
    theta = ThetaInfer.default()
    stale = _b(s=10.0, last=t0 - timedelta(days=10))  # R = e^-1
    res = apply_evidence(_beliefs(graph, pca=stale),
                         _e(t0, "pca", EvidenceType.QUIZ_WRONG, Polarity.NEGATIVE), graph, theta)
    assert res.updated["pca"].beta == pytest.approx(2.0 + 0.36787944, rel=1e-6)
    assert res.updated["pca"].stability == pytest.approx(5.0)


def test_self_report_forgetting_shrinks_stability_only(graph, t0):
    theta = ThetaInfer.default()  # weight 0.5, shrink 0.5
    res = apply_evidence(_beliefs(graph),
                         _e(t0, "eigenvalue", EvidenceType.SELF_REPORT_FORGETTING, Polarity.NEGATIVE),
                         graph, theta)
    e = res.updated["eigenvalue"]
    assert (e.alpha, e.beta) == (2.0, 2.0)
    assert e.stability == pytest.approx(10.0 * 0.5 ** 0.5)


def test_zero_weight_behavioral_is_noop(graph, t0):
    theta = ThetaInfer.default()
    before = _beliefs(graph)
    res = apply_evidence(before,
                         _e(t0, "eigenvalue", EvidenceType.EXPAND_PREREQUISITE, Polarity.NEGATIVE),
                         graph, theta)
    assert res.updated == before
    assert res.ambiguous == set()


def test_input_not_mutated(graph, t0):
    theta = ThetaInfer.default()
    before = _beliefs(graph)
    snapshot = dict(before)
    apply_evidence(before, _e(t0, "pca", EvidenceType.QUIZ_CORRECT, Polarity.POSITIVE), graph, theta)
    assert before == snapshot
