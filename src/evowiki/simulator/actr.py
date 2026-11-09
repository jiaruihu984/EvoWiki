from collections.abc import Sequence
from datetime import datetime
from math import inf, log

from evowiki.simulator.mathutil import sigmoid
from evowiki.timeutil import days_between

MIN_LAG_DAYS = 0.01


def activation(trace_times: Sequence[datetime], now: datetime, d: float) -> float:
    """ACT-R base-level activation A = ln Σ_j t_j^(−d), t_j = days since practice j (spec §9.2).

    Traces after `now` are ignored and lags are floored at MIN_LAG_DAYS. No usable trace: −inf.
    """
    if d <= 0.0:
        raise ValueError("decay d must be positive")
    total = 0.0
    for t in sorted(trace_times):
        lag = days_between(t, now)
        if lag < 0.0:
            continue
        total += max(lag, MIN_LAG_DAYS) ** (-d)
    return log(total) if total > 0.0 else -inf


def retrievability(a: float, tau: float, s: float) -> float:
    """ρ(A) = σ((A − τ) / s): probability that a learned KC can be retrieved now."""
    if s <= 0.0:
        raise ValueError("rho scale s must be positive")
    if a == -inf:
        return 0.0
    return sigmoid((a - tau) / s)
