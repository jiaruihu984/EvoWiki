import numpy as np
import pytest

from evowiki.teach.fit.bkt import BktParams, bkt_predict, fit_bkt

TRUE = BktParams(prior=0.2, learn=0.15, forget=0.02, guess=0.2, slip=0.1)


def _sample(p: BktParams, n: int, length: int, seed: int) -> list[list[int]]:
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        known = rng.random() < p.prior
        seq = []
        for _ in range(length):
            pc = 1.0 - p.slip if known else p.guess
            seq.append(int(rng.random() < pc))
            known = (rng.random() >= p.forget) if known else (rng.random() < p.learn)
        out.append(seq)
    return out


def test_recovers_guess_and_slip():
    seqs = _sample(TRUE, 200, 40, seed=3)
    p, ll = fit_bkt(seqs, iters=100, seed=0, restarts=3)
    assert abs(p.guess - TRUE.guess) <= 0.05, p
    assert abs(p.slip - TRUE.slip) <= 0.05, p
    assert np.isfinite(ll) and ll < 0.0
    # the fit explains the data at least as well as the truth; ll = Σ log one-step predictions
    def loglik(params):
        return sum(np.log(pc if y else 1.0 - pc)
                   for s in seqs for pc, y in zip(bkt_predict(params, s), s, strict=True))
    assert ll == pytest.approx(loglik(p))
    assert ll >= loglik(TRUE)


def test_ragged_sequences_and_predict():
    seqs = _sample(TRUE, 50, 30, seed=4)
    ragged = [s[: 5 + i % 20] for i, s in enumerate(seqs)]
    p, _ = fit_bkt(ragged, iters=20)
    for s in ragged[:5]:
        pred = bkt_predict(p, s)
        assert len(pred) == len(s)
        assert all(0.0 < v < 1.0 for v in pred)
    assert bkt_predict(p, []) == []


def test_predict_rises_after_correct_answers():
    pred = bkt_predict(TRUE, [1, 1, 1, 1, 1])
    assert pred[0] == pytest.approx(TRUE.prior * 0.9 + 0.8 * TRUE.guess)
    assert pred == sorted(pred)


def test_fails_closed_on_bad_input():
    with pytest.raises(ValueError):
        fit_bkt([])
    with pytest.raises(ValueError):
        fit_bkt([[], []])
    with pytest.raises(ValueError):
        fit_bkt([[0, 2, 1]])
