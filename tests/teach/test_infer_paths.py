import pytest
from pydantic import ValidationError

from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import InferParams


def test_paths_cover_the_whitelist():
    assert ip.PATHS == (
        "evidence_weight.quiz", "evidence_weight.check", "evidence_weight.self",
        "evidence_weight.feedback", "evidence_weight.ask", "evidence_weight.recall",
        "evidence_weight.slip_claim", "evidence_weight.learned_claim", "forgetting_scale", "slip", "guess_scale", "inference_strength",
        "start_known")
    assert set(ip.PATH_LABEL) == set(ip.PATHS)
    assert set(ip.PATH_DESCRIPTION) == set(ip.PATHS)
    assert all(len(ip.PATH_DESCRIPTION[p]) > 10 for p in ip.PATHS)
    assert ip.PATH_LABEL["evidence_weight.self"] == "自我评估权重"
    assert ip.PATH_LABEL["start_known"] == "初始掌握概率"
    assert all(ip.PATH_LABEL[p] for p in ip.PATHS)


def test_bounds_come_from_the_contract():
    assert ip.bounds("evidence_weight.self") == (0.0, 2.0)
    assert ip.bounds("evidence_weight.slip_claim") == ip.bounds("evidence_weight.learned_claim") == (0.0, 1.0)
    assert ip.bounds("forgetting_scale") == (0.5, 2.0)
    assert ip.bounds("slip") == (0.0, 0.3)
    assert ip.bounds("guess_scale") == (0.5, 1.5)
    assert ip.bounds("inference_strength") == (0.0, 1.5)
    assert ip.bounds("start_known") == (0.05, 0.95)
    for path in ip.PATHS:      # every bound is accepted by the contract itself
        lo, hi = ip.bounds(path)
        for v in (lo, hi):
            ip.set_(InferParams(), path, v)


def test_get_reads_defaults():
    d = InferParams()
    assert ip.get(d, "evidence_weight.self") == 0.6
    assert ip.get(d, "forgetting_scale") == 1.0
    assert ip.get(d, "slip") == 0.1


@pytest.mark.parametrize("path", ip.PATHS)
def test_set_clamps_and_returns_new(path):
    d = InferParams()
    lo, hi = ip.bounds(path)
    assert ip.get(ip.set_(d, path, hi + 10.0), path) == hi
    assert ip.get(ip.set_(d, path, lo - 10.0), path) == lo
    mid = (lo + hi) / 2
    p = ip.set_(d, path, mid)
    assert ip.get(p, path) == mid
    assert ip.get(d, path) == ip.get(InferParams(), path)       # the input is unchanged
    assert ip.complexity(p) == (0 if mid == ip.get(d, path) else 1)
    for other in ip.PATHS:
        if other != path:
            assert ip.get(p, other) == ip.get(d, other)


def test_set_keeps_other_weights_and_validates():
    p = ip.set_(InferParams(), "evidence_weight.self", 0.2)
    assert p.evidence_weight == {**InferParams().evidence_weight, "self": 0.2}
    assert isinstance(p, InferParams)
    with pytest.raises(ValidationError):
        InferParams(slip=0.5)               # set_ never produces this: it clamps first


def test_complexity_counts_changed_paths():
    p = InferParams()
    assert ip.complexity(p) == 0
    p = ip.set_(p, "slip", 0.2)
    p = ip.set_(p, "evidence_weight.ask", 0.1)
    assert ip.complexity(p) == 2
    assert ip.complexity(ip.set_(p, "slip", 0.1)) == 1


def test_unknown_path_raises():
    with pytest.raises(KeyError):
        ip.get(InferParams(), "evidence_weight.nope")
    with pytest.raises(KeyError):
        ip.bounds("prior_a")
    with pytest.raises(KeyError):
        ip.set_(InferParams(), "bands.recall_min", 0.5)


def test_theta_stored_before_the_claim_kinds_gets_their_defaults():
    old = {"quiz": 1.0, "check": 1.0, "self": 0.5, "feedback": 0.4, "ask": 0.3, "recall": 1.0}
    p = InferParams.model_validate({"evidence_weight": old})
    assert p.evidence_weight["self"] == 0.5
    assert p.evidence_weight["slip_claim"] == p.evidence_weight["learned_claim"] == 0.3
    with pytest.raises(ValidationError):           # a claim weight is a trust level in [0, 1]
        InferParams.model_validate({"evidence_weight": {**old, "slip_claim": 1.5}})
