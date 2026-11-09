"""A1 fitter: grid MAP / posterior of θ_infer paths on the prequential likelihood.

The likelihood is the one `gate_a` scores: `predict_outcomes` (one-step-ahead) and
`metrics.log_loss`, summed over the training outcomes (`ts < cutoff`, all when None).

`coord_map@1`: for `sweeps` rounds, for each path in `model.paths`, evaluate `grid` values evenly
spaced on the logit scale of `bounds(path)` (endpoints clamped 1e-3 inside) plus the current
value; log posterior = Σ_train -log_loss + log_prior; take the argmax (ties keep the current value)
and set it. Afterwards, per path, exp(log posterior - max) over its grid with every other path at
its MAP is normalised (mass per grid point, i.e. a logit-uniform measure) → the 5 % / 95 %
quantiles. `coord_map_recency@1`: each training outcome's term is weighted by
0.5 ** (age_days / recency_half_life_days), age measured to the newest training outcome.

Exactness and cost: the target set does not depend on θ (see `predict`), so the training outcomes
are found once and only the log prefix up to the last training outcome is replayed (a prediction
only sees events before it in log order, so this changes nothing). Replays are memoised on the
fitted values, so the posterior pass is free when the last sweep did not move."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from evowiki.belief.aspects import AspectConfig, AspectEvent
from evowiki.schema import days_between
from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import FitModel, InferParams
from evowiki.teach.fit.priors import EDGE, PathPrior, from_logit, log_prior
from evowiki.teach.metrics import log_loss
from evowiki.teach.predict import predict_outcomes

Q_LO, Q_HI = 0.05, 0.95


@dataclass(frozen=True)
class PathPosterior:
    path: str
    value: float                        # MAP on the grid
    lo: float                           # 5 % posterior quantile on the grid
    hi: float                           # 95 %
    prior_mean: float


@dataclass(frozen=True)
class FitResult:
    params: InferParams                 # current params with the fitted paths replaced
    posteriors: tuple[PathPosterior, ...]
    loglik_train: float                 # Σ log p(y) over training outcomes under params
    loglik_start: float                 # same under the starting params
    n_train: int


def _grid(path: str, n: int) -> list[float]:
    zmax = float(np.log((1.0 - EDGE) / EDGE))
    return [from_logit(path, float(z)) for z in np.linspace(-zmax, zmax, n)]


def _quantile(values: Sequence[float], logpost: Sequence[float], q: float) -> float:
    order = np.argsort(np.asarray(values), kind="stable")
    v = np.asarray(values)[order]
    w = np.exp(np.asarray(logpost)[order] - np.max(logpost))
    cdf = np.cumsum(w) / np.sum(w)
    return float(v[min(int(np.searchsorted(cdf, q)), len(v) - 1)])


def fit_infer(events: Sequence[AspectEvent], base: AspectConfig, start: InferParams,
              domain_of: Mapping[str, str], *, model: FitModel,
              priors: Mapping[str, PathPrior], cutoff: datetime | None,
              grid: int = 21, sweeps: int = 3) -> FitResult:
    if model.algorithm not in ("coord_map@1", "coord_map_recency@1"):
        raise ValueError(f"unknown fit algorithm {model.algorithm!r}")
    if grid < 2 or sweeps < 1:
        raise ValueError("need grid >= 2 and sweeps >= 1")
    missing = [p for p in model.paths if p not in priors]
    if missing:
        raise ValueError(f"no prior for fit paths {missing}")

    outs0 = predict_outcomes(events, base, start, domain_of)
    train = [o for o in outs0 if cutoff is None or o.ts < cutoff]
    if not train:
        raise ValueError("no training outcomes before the cutoff: nothing to fit")
    train_ids = {o.event_id for o in train}
    last = max(i for i, ev in enumerate(events) if ev.event_id in train_ids)
    prefix = list(events[:last + 1])

    if model.algorithm == "coord_map_recency@1":
        newest = max(o.ts for o in train)
        hl = float(model.recency_half_life_days)
        weight = {o.event_id: 0.5 ** (days_between(o.ts, newest) / hl) for o in train}
    else:
        weight = {o.event_id: 1.0 for o in train}

    cache: dict[tuple[float, ...], tuple[float, float]] = {}

    def score(params: InferParams) -> tuple[float, float]:
        """(weighted, unweighted) Σ log p(y) over the training outcomes."""
        key = tuple(ip.get(params, p) for p in model.paths)
        hit = cache.get(key)
        if hit is None:
            wsum = usum = 0.0
            for o in predict_outcomes(prefix, base, params, domain_of):
                if o.event_id in train_ids:
                    ll = -log_loss(o.p, o.y)
                    wsum += weight[o.event_id] * ll
                    usum += ll
            hit = cache[key] = (wsum, usum)
        return hit

    def sweep_path(params: InferParams, path: str) -> tuple[list[float], list[float]]:
        cur = ip.get(params, path)
        values = [cur] + [v for v in _grid(path, grid) if v != cur]
        logpost = [score(ip.set_(params, path, v))[0]
                   + log_prior(path, v, priors[path], model.prior_strength) for v in values]
        return values, logpost

    params = start
    for _ in range(sweeps):
        for path in model.paths:
            values, logpost = sweep_path(params, path)
            best = int(np.argmax(logpost))          # first max: index 0 = current value
            params = ip.set_(params, path, values[best])

    posteriors = []
    for path in model.paths:
        values, logpost = sweep_path(params, path)
        posteriors.append(PathPosterior(
            path=path, value=ip.get(params, path),
            lo=_quantile(values, logpost, Q_LO), hi=_quantile(values, logpost, Q_HI),
            prior_mean=priors[path].mean))

    return FitResult(params=params, posteriors=tuple(posteriors),
                     loglik_train=score(params)[1], loglik_start=score(start)[1],
                     n_train=len(train))
