"""Channel-A scoring: per-outcome losses and a paired bootstrap over loss differences."""

import hashlib
import random
from collections.abc import Sequence
from math import log

_EPS = 1e-12


def log_loss(p: float, y: float) -> float:
    """Soft-label cross-entropy, -(y log p + (1-y) log(1-p)); p is clamped away from 0 and 1."""
    p = min(1.0 - _EPS, max(_EPS, p))
    return -(y * log(p) + (1.0 - y) * log(1.0 - p))


def brier(p: float, y: float) -> float:
    return (p - y) ** 2


def seed_of(text: str) -> int:
    """Deterministic 64-bit seed from text (sha256), for reproducible bootstraps."""
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _quantile(xs: list[float], q: float) -> float:
    """Linear-interpolated quantile of a sorted list."""
    pos = q * (len(xs) - 1)
    i = int(pos)
    j = min(i + 1, len(xs) - 1)
    return xs[i] + (xs[j] - xs[i]) * (pos - i)


def paired_bootstrap(diffs: Sequence[float], *, seed: int, n: int = 2000,
                     alpha: float = 0.05) -> tuple[float, float, float]:
    """(mean, lo, hi): the mean of `diffs` and the percentile (1 - alpha) bootstrap interval of
    the mean, resampling the paired differences with `random.Random(seed)`."""
    if not diffs:
        raise ValueError("no differences to bootstrap")
    if n < 1 or not 0.0 < alpha < 1.0:
        raise ValueError("need n >= 1 and 0 < alpha < 1")
    d = [float(x) for x in diffs]
    m = len(d)
    mean = sum(d) / m
    rng = random.Random(seed)
    means = sorted(sum(d[rng.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return mean, _quantile(means, alpha / 2.0), _quantile(means, 1.0 - alpha / 2.0)
