from evowiki.teach.anchors import MIN_EVENTS, SANDBOX, auc, logloss_delta, rate_delta, verdict
from evowiki.teach.contracts import AnchorDelta


def _d(lo, hi, n=MIN_EVENTS):
    return AnchorDelta(metric="check_correct", delta=0.0, lo=lo, hi=hi, n_before=n, n_after=n)


def test_verdict_truth_table():
    assert verdict([]) == "pending"
    assert verdict([_d(0.1, 0.2, n=MIN_EVENTS - 1)]) == "pending"
    assert verdict([_d(0.1, 0.2)]) == "helpful"
    assert verdict([_d(-0.2, -0.1)]) == "harmful"
    assert verdict([_d(-0.1, 0.1)]) == "no_effect"
    assert verdict([_d(0.1, 0.2), _d(-0.2, -0.1)]) == "harmful"
    assert verdict([_d(0.1, 0.2, n=2), _d(-0.1, 0.1)]) == "no_effect"


def test_rate_delta_identical_is_no_effect():
    xs = [0.0, 1.0] * 20
    d = rate_delta("check_correct", xs, xs, higher_is_better=True, seed=1)
    assert d.lo <= 0 <= d.hi
    assert verdict([d]) == "no_effect"


def test_rate_delta_orientation():
    d = rate_delta("unclear_rate", [1.0] * 20, [0.0] * 20, higher_is_better=False, seed=1)
    assert d.delta == 1.0 and d.lo > 0
    assert verdict([d]) == "helpful"


def test_rate_delta_empty_side():
    d = rate_delta("undo_rate", [], [1.0], higher_is_better=False, seed=1)
    assert d.delta is None and d.n_before == 0 and verdict([d]) == "pending"


def test_logloss_sign():
    old = [(0.5, 1.0), (0.5, 0.0)] * 15
    new = [(0.9, 1.0), (0.1, 0.0)] * 15
    d = logloss_delta(old, new, seed=3)
    assert d.delta > 0 and d.lo > 0
    assert logloss_delta(new, old, seed=3).delta < 0


def test_auc():
    assert auc([(0.9, 1), (0.8, 1), (0.2, 0), (0.1, 0)]) == 1.0
    assert auc([(0.1, 1), (0.9, 0)]) == 0.0
    assert auc([(0.5, 1), (0.5, 0)]) == 0.5
    assert auc([(0.3, 1), (0.6, 1)]) is None


def test_sandbox_keys():
    assert set(SANDBOX) == {"A", "B", "A2"}
