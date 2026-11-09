"""Standard Bayesian Knowledge Tracing with forgetting, one skill at a time, fitted by EM.

The model is pyBKT's two-state HMM (our code, numpy only): hidden state unknown (0) / known (1),
P(known at the first opportunity) = prior; each step unknown → known with `learn`, known →
unknown with `forget`; P(correct | unknown) = guess, P(correct | known) = 1 - slip.

`fit_bkt` runs Baum–Welch (scaled forward–backward over all sequences at once, padded and masked)
from `restarts` seeded random starts and keeps the best log-likelihood among the
non-degenerate solutions (guess < 1 - slip, i.e. knowing helps); when every start is degenerate
it keeps the best one. Parameters are clipped `EPS` inside (0, 1) after each M-step.

Used only to cross-check the population guess / slip of public datasets (scripts/rsi/fit_priors.py);
the product's θ_infer is fitted by `fitter.fit_infer`."""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

EPS = 1e-4


@dataclass(frozen=True)
class BktParams:
    prior: float
    learn: float
    forget: float
    guess: float
    slip: float


def _pack(seqs: Sequence[Sequence[int]]) -> tuple[np.ndarray, np.ndarray]:
    if not seqs or not any(len(s) for s in seqs):
        raise ValueError("fit_bkt needs at least one non-empty sequence")
    seqs = [s for s in seqs if len(s)]
    t_max = max(len(s) for s in seqs)
    y = np.zeros((len(seqs), t_max), dtype=float)
    mask = np.zeros((len(seqs), t_max), dtype=bool)
    for i, s in enumerate(seqs):
        v = np.asarray(s, dtype=float)
        if not np.all((v == 0.0) | (v == 1.0)):
            raise ValueError("BKT outcomes must be 0 or 1")
        y[i, :len(v)] = v
        mask[i, :len(v)] = True
    return y, mask


def _emissions(p: BktParams, y: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """(N, T, 2) P(y_t | state); 1 on padding (no information)."""
    p_correct = np.array([p.guess, 1.0 - p.slip])
    e = np.where(y[..., None] == 1.0, p_correct, 1.0 - p_correct)
    return np.where(mask[..., None], e, 1.0)


def _trans(p: BktParams) -> np.ndarray:
    return np.array([[1.0 - p.learn, p.learn], [p.forget, 1.0 - p.forget]])


def _forward(p: BktParams, e: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Scaled forward pass → (alpha (N,T,2), scale c (N,T), predicted state before each
    observation (N,T,2))."""
    n, t_max, _ = e.shape
    a = _trans(p)
    alpha = np.empty((n, t_max, 2))
    pred = np.empty((n, t_max, 2))
    c = np.empty((n, t_max))
    state = np.tile([1.0 - p.prior, p.prior], (n, 1))
    for t in range(t_max):
        pred[:, t] = state
        f = state * e[:, t]
        c[:, t] = f.sum(axis=1)
        alpha[:, t] = f / c[:, t, None]
        state = alpha[:, t] @ a
    return alpha, c, pred


def _loglik(c: np.ndarray, mask: np.ndarray) -> float:
    return float(np.sum(np.log(c[mask])))


def _em_step(p: BktParams, y: np.ndarray, mask: np.ndarray) -> tuple[BktParams, float]:
    e = _emissions(p, y, mask)
    a = _trans(p)
    alpha, c, _ = _forward(p, e)
    n, t_max, _ = e.shape
    beta = np.ones((n, t_max, 2))
    for t in range(t_max - 2, -1, -1):
        beta[:, t] = (beta[:, t + 1] * e[:, t + 1]) @ a.T / c[:, t + 1, None]
    gamma = alpha * beta
    gamma /= gamma.sum(axis=2, keepdims=True)
    # xi[n, t, i, j] for t → t + 1, counted only where t + 1 is a real observation
    xi = (alpha[:, :-1, :, None] * a[None, None]
          * (e[:, 1:] * beta[:, 1:])[:, :, None, :] / c[:, 1:, None, None])
    xi = xi * mask[:, 1:, None, None]
    g_from = gamma[:, :-1] * mask[:, 1:, None]
    gm = gamma * mask[..., None]

    def ratio(num: float, den: float, old: float) -> float:
        v = num / den if den > 0.0 else old
        return float(min(1.0 - EPS, max(EPS, v)))

    new = BktParams(
        prior=ratio(gamma[:, 0, 1].sum(), n, p.prior),
        learn=ratio(xi[..., 0, 1].sum(), g_from[..., 0].sum(), p.learn),
        forget=ratio(xi[..., 1, 0].sum(), g_from[..., 1].sum(), p.forget),
        guess=ratio((gm[..., 0] * y).sum(), gm[..., 0].sum(), p.guess),
        slip=ratio((gm[..., 1] * (1.0 - y)).sum(), gm[..., 1].sum(), p.slip))
    return new, _loglik(c, mask)


def _loglik_of(p: BktParams, y: np.ndarray, mask: np.ndarray) -> float:
    return _loglik(_forward(p, _emissions(p, y, mask))[1], mask)


def fit_bkt(sequences: Sequence[Sequence[int]], *, iters: int = 50, seed: int = 0,
            restarts: int = 3) -> tuple[BktParams, float]:
    """EM fit of one skill's BKT on per-student 0/1 sequences → (params, log-likelihood)."""
    if iters < 1 or restarts < 1:
        raise ValueError("need iters >= 1 and restarts >= 1")
    y, mask = _pack(sequences)
    rng = np.random.default_rng(seed)
    results: list[tuple[BktParams, float]] = []
    for _ in range(restarts):
        p = BktParams(prior=float(rng.uniform(0.05, 0.5)), learn=float(rng.uniform(0.05, 0.3)),
                      forget=float(rng.uniform(0.0, 0.1)) + EPS,
                      guess=float(rng.uniform(0.05, 0.4)), slip=float(rng.uniform(0.05, 0.3)))
        prev = -np.inf
        for _ in range(iters):
            p, ll = _em_step(p, y, mask)
            if ll - prev < 1e-6:
                break
            prev = ll
        results.append((p, _loglik_of(p, y, mask)))
    good = [r for r in results if r[0].guess < 1.0 - r[0].slip]
    return max(good or results, key=lambda r: r[1])


def bkt_predict(params: BktParams, seq: Sequence[int]) -> list[float]:
    """One-step-ahead P(correct) for each position of `seq` given the outcomes before it."""
    if not len(seq):
        return []
    y, mask = _pack([seq])
    _, _, pred = _forward(params, _emissions(params, y, mask))
    p_correct = np.array([params.guess, 1.0 - params.slip])
    return [float(v) for v in pred[0] @ p_correct]
