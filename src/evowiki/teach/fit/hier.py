"""Population (hierarchical) prior from per-student likelihood grids: type-II maximum likelihood.

Per-student MAPs under a flat prior are noisy, and on weakly identified paths they pile up at the
bounds, so their sample mean / sd describe the noise more than the population. `fit_population`
instead fits independent normals on the logit scale of each path (the shape `priors.PathPrior`
holds) by maximising the marginal likelihood Σ_s log Σ_g exp(ll_s(g)) · N(z_g; μ, σ) over a fixed
product grid of logit values (EM: responsibilities over the grid, then moment updates of μ and σ
per path). The normal is evaluated on the grid and renormalised, so σ below about half the grid
spacing is not resolvable: σ is floored at `sd_floor`. The moment update is that of an
untruncated normal, so near the grid edge it is not an exact EM step and the marginal
likelihood need not rise monotonically; the loop stops at the first gain below `tol` (a decrease
included); the returned marginal log-likelihood is that of the (μ, σ) before the last update."""

from collections.abc import Sequence

import numpy as np


def _log_normal(z: np.ndarray, mu: float, sd: float) -> np.ndarray:
    lp = -0.5 * ((z - mu) / sd) ** 2
    return lp - np.logaddexp.reduce(lp)


def fit_population(ll: np.ndarray, axes: Sequence[Sequence[float]], *, sd_floor: float = 0.5,
                   iters: int = 500, tol: float = 1e-7) -> tuple[list[float], list[float], float]:
    """`ll[s, i1, …, ik]` = student s's log-likelihood at grid point (axes[0][i1], …) (logit
    values per path) → (μ per path, σ per path, marginal log-likelihood)."""
    ll = np.asarray(ll, dtype=float)
    k = len(axes)
    zs = [np.asarray(a, dtype=float) for a in axes]
    if ll.ndim != k + 1 or ll.shape[1:] != tuple(len(z) for z in zs):
        raise ValueError(f"ll shape {ll.shape} does not match axes {[len(z) for z in zs]}")
    if ll.shape[0] < 2:
        raise ValueError("need at least two students")
    if not np.all(np.isfinite(ll)):
        raise ValueError("non-finite log-likelihoods")
    mu = [float(np.mean(z)) for z in zs]
    sd = [max(sd_floor, float(np.ptp(z)) / 4.0) for z in zs]
    prev = -np.inf
    total = prev
    for _ in range(iters):
        lp = np.zeros(ll.shape[1:])
        for j in range(k):
            shape = [1] * k
            shape[j] = len(zs[j])
            lp = lp + _log_normal(zs[j], mu[j], sd[j]).reshape(shape)
        joint = ll + lp                                        # (S, grid)
        norm = np.logaddexp.reduce(joint.reshape(len(ll), -1), axis=1)
        total = float(norm.sum())
        resp = np.exp(joint - norm.reshape((-1,) + (1,) * k))
        for j in range(k):
            other = tuple(a for a in range(1, k + 1) if a != j + 1)
            m = resp.sum(axis=other) if other else resp        # (S, len(zs[j]))
            w = m.sum(axis=0) / len(ll)
            mu[j] = float(np.dot(w, zs[j]))
            sd[j] = max(sd_floor, float(np.sqrt(np.dot(w, (zs[j] - mu[j]) ** 2))))
        if total - prev < tol:
            break
        prev = total
    return mu, sd, total
