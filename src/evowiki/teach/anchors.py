"""Fixed anchor metrics and the RSI sandbox (RSI design section 3).

Only the user changes this file; no RSI object reads its constants from a policy. Pure functions:
no I/O, no LLM, deterministic given the seed.

Each metric is a bootstrap of (after − before), oriented so + is better. `AnchorDelta.lo/hi` are the
90 % interval (shown to the user). Deciding over several metrics at once is corrected for multiple
looks (`judge`): helpful / harmful are decided on Bonferroni intervals at ALPHA / k, k = the number
of metrics with at least MIN_EVENTS on both sides, computed from the same bootstrap samples.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from evowiki.teach.contracts import AnchorDelta, AnchorMetric, Verdict

MIN_EVENTS = 10            # per side, per metric, before a metric can decide
ALPHA = 0.10               # 90 % interval (display), family-wise level when deciding
_BOOT = 2000
_EPS = 1e-6

SANDBOX: Mapping[str, str] = MappingProxyType({
    "A": "θ_infer paths (infer_paths.PATHS)",
    "B": "teaching spec items and RepParams",
    "A2": "FitModel",
})


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _q(xs: Sequence[float], q: float) -> float:
    pos = q * (len(xs) - 1)
    i = int(pos)
    j = min(i + 1, len(xs) - 1)
    return xs[i] + (xs[j] - xs[i]) * (pos - i)


@dataclass(frozen=True)
class Sampled:
    """One metric's delta with its sorted bootstrap samples (() when it has no interval)."""

    delta: AnchorDelta
    samples: tuple[float, ...] = ()

    def at(self, alpha: float) -> AnchorDelta:
        """The delta with its (1 − alpha) interval; unchanged when it has no samples."""
        if not self.samples:
            return self.delta
        return self.delta.model_copy(update={"lo": _q(self.samples, alpha / 2),
                                             "hi": _q(self.samples, 1 - alpha / 2)})


def _sampled(metric: AnchorMetric, before: float | None, after: float | None, delta: float | None,
             samples: list[float], nb: int, na: int) -> Sampled:
    samples.sort()
    s = Sampled(AnchorDelta(metric=metric, before=before, after=after, delta=delta,
                            n_before=nb, n_after=na), tuple(samples))
    return Sampled(s.at(ALPHA), s.samples)


def rate_sampled(metric: AnchorMetric, before: Sequence[float], after: Sequence[float], *,
                 higher_is_better: bool, seed: int) -> Sampled:
    """Difference of means (after - before), oriented so + is better."""
    b = [float(x) for x in before]
    a = [float(x) for x in after]
    mb, ma = _mean(b), _mean(a)
    if mb is None or ma is None:
        return Sampled(AnchorDelta(metric=metric, before=mb, after=ma, n_before=len(b),
                                   n_after=len(a)))
    sign = 1.0 if higher_is_better else -1.0
    rng = random.Random(seed)
    nb, na = len(b), len(a)
    diffs = [sign * (sum(a[rng.randrange(na)] for _ in range(na)) / na
                     - sum(b[rng.randrange(nb)] for _ in range(nb)) / nb)
             for _ in range(_BOOT)]
    return _sampled(metric, mb, ma, sign * (ma - mb), diffs, nb, na)


def rate_delta(metric: AnchorMetric, before: Sequence[float], after: Sequence[float], *,
               higher_is_better: bool, seed: int) -> AnchorDelta:
    return rate_sampled(metric, before, after, higher_is_better=higher_is_better, seed=seed).delta


def _loss(p: float, y: float) -> float:
    p = min(max(float(p), _EPS), 1.0 - _EPS)
    return -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))


def _paired(old: Sequence[tuple[float, float]], new: Sequence[tuple[float, float]],
            what: str) -> None:
    if len(old) != len(new):
        raise ValueError(f"{what} needs paired predictions ({len(old)} vs {len(new)})")


def logloss_sampled(old: Sequence[tuple[float, float]], new: Sequence[tuple[float, float]], *,
                    seed: int) -> Sampled:
    """Paired (p, y) predictions on the same events; delta = logloss_old - logloss_new (+ better).
    The two sequences must pair one to one (ValueError otherwise)."""
    _paired(old, new, "logloss_delta")
    n = len(old)
    if n == 0:
        return Sampled(AnchorDelta(metric="pred_logloss"))
    lo_old = [_loss(p, y) for p, y in old]
    lo_new = [_loss(p, y) for p, y in new]
    d = [o - w for o, w in zip(lo_old, lo_new, strict=True)]
    rng = random.Random(seed)
    means = [sum(d[rng.randrange(n)] for _ in range(n)) / n for _ in range(_BOOT)]
    return _sampled("pred_logloss", sum(lo_old) / n, sum(lo_new) / n, sum(d) / n, means, n, n)


def logloss_delta(old: Sequence[tuple[float, float]], new: Sequence[tuple[float, float]], *,
                  seed: int) -> AnchorDelta:
    return logloss_sampled(old, new, seed=seed).delta


def auc(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Rank AUC of (score, label) pairs; ties count 1/2; None when one class is missing."""
    pos = [s for s, y in pairs if y >= 0.5]
    neg = [s for s, y in pairs if y < 0.5]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def _auc_ranked(scores: Sequence[float], labels: Sequence[float]) -> float | None:
    """Same value as `auc` (ties 1/2) via average ranks, O(n log n)."""
    n = len(scores)
    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    pos = [k for k in range(n) if labels[k] >= 0.5]
    n_pos, n_neg = len(pos), n - len(pos)
    if n_pos == 0 or n_neg == 0:
        return None
    return (sum(ranks[k] for k in pos) - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def auc_sampled(old: Sequence[tuple[float, float]], new: Sequence[tuple[float, float]], *,
                seed: int) -> Sampled:
    """Paired (p, y) predictions on the same events; delta = AUC_new - AUC_old (+ better), paired
    bootstrap over events (resamples missing a class are skipped). No delta when one class is
    missing from the events."""
    _paired(old, new, "auc_delta")
    n = len(old)
    if any(float(a[1]) != float(b[1]) for a, b in zip(old, new, strict=True)):
        raise ValueError("auc_delta: paired events must have the same outcome")
    ys = [float(y) for _, y in old]
    so = [float(p) for p, _ in old]
    sn = [float(p) for p, _ in new]
    a_old, a_new = _auc_ranked(so, ys), _auc_ranked(sn, ys)
    if a_old is None or a_new is None:
        return Sampled(AnchorDelta(metric="pred_auc", before=a_old, after=a_new, n_before=n,
                                   n_after=n))
    rng = random.Random(seed)
    diffs = []
    for _ in range(_BOOT):
        idx = [rng.randrange(n) for _ in range(n)]
        y = [ys[i] for i in idx]
        bo, bn = _auc_ranked([so[i] for i in idx], y), _auc_ranked([sn[i] for i in idx], y)
        if bo is not None and bn is not None:
            diffs.append(bn - bo)
    return _sampled("pred_auc", a_old, a_new, a_new - a_old, diffs, n, n)


def auc_delta(old: Sequence[tuple[float, float]], new: Sequence[tuple[float, float]], *,
              seed: int) -> AnchorDelta:
    return auc_sampled(old, new, seed=seed).delta


def _decided(d: AnchorDelta) -> bool:
    return (d.n_before >= MIN_EVENTS and d.n_after >= MIN_EVENTS
            and d.lo is not None and d.hi is not None)


def verdict(deltas: Sequence[AnchorDelta]) -> Verdict:
    """The rule on the intervals as given: pending when no metric can decide; harmful when some
    interval lies below 0; helpful when some lies above 0; else no_effect. Over several metrics, use
    `judge` (it applies this rule to Bonferroni intervals)."""
    decided = [d for d in deltas if _decided(d)]
    if not decided:
        return "pending"
    if any(d.hi < 0 for d in decided):  # type: ignore[operator]
        return "harmful"
    if any(d.lo > 0 for d in decided):  # type: ignore[operator]
        return "helpful"
    return "no_effect"


def judge(metrics: Sequence[Sampled]) -> tuple[tuple[AnchorDelta, ...], Verdict]:
    """(the 90 % deltas for display, the verdict on Bonferroni intervals at ALPHA / k), k = the
    metrics that can decide (≥ MIN_EVENTS on both sides, with an interval)."""
    shown = tuple(m.delta for m in metrics)
    k = sum(1 for d in shown if _decided(d))
    if k == 0:
        return shown, "pending"
    return shown, verdict([m.at(ALPHA / k) for m in metrics])
