"""Channel A gate: time split, paired bootstrap of log-loss gains, per-domain regression veto.

Old and new θ_infer,u both predict over the full log (prequentially); only the test outcomes at
or after `cutoff` are scored. The target set does not depend on θ, so the two outcome lists pair
1:1 by event_id; a mismatch is a bug and raises."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime

from evowiki.belief.aspects import AspectConfig, AspectEvent
from evowiki.teach.contracts import GateResult, InferParams
from evowiki.teach.infer_paths import complexity
from evowiki.teach.metrics import brier, log_loss, paired_bootstrap, seed_of
from evowiki.teach.predict import predict_outcomes

MIN_OUTCOMES = 30
MIN_DOMAIN = 5
DOMAIN_TOL = 0.005
BRIER_TOL = 0.0


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def gate_a(patch_id: str, events: Sequence[AspectEvent], base: AspectConfig, old: InferParams,
           new: InferParams, *, cutoff: datetime, domain_of: Mapping[str, str]) -> GateResult:
    """Does `new` predict this user's outcomes at or after `cutoff` better than `old`?

    Fewer than MIN_OUTCOMES test outcomes → not passed with veto "insufficient_outcomes" (the
    cycle keeps the patch as a candidate). Otherwise passed iff no veto fired: "no_ci_improvement"
    (bootstrap ci_low of logloss_old − logloss_new is not > 0), "brier_worse" (mean Brier gain
    < −BRIER_TOL), "domain_regressed:<d>" (a domain with ≥ MIN_DOMAIN outcomes whose mean
    logloss_new − logloss_old > DOMAIN_TOL)."""
    olds = {o.event_id: o for o in predict_outcomes(events, base, old, domain_of) if o.ts >= cutoff}
    news = {o.event_id: o for o in predict_outcomes(events, base, new, domain_of) if o.ts >= cutoff}
    if olds.keys() != news.keys():
        raise ValueError(f"old and new outcome sets differ: {sorted(olds.keys() ^ news.keys())[:5]}")
    common = {"patch_id": patch_id, "channel": "A", "complexity_before": complexity(old),
              "complexity_after": complexity(new), "n_outcomes": len(olds), "cost_usd": 0.0}
    if len(olds) < MIN_OUTCOMES:
        return GateResult(passed=False, vetoes=("insufficient_outcomes",), **common)

    ids = sorted(olds, key=lambda i: (olds[i].ts, i))
    ll_old = [log_loss(olds[i].p, olds[i].y) for i in ids]
    ll_new = [log_loss(news[i].p, news[i].y) for i in ids]
    d = [a - b for a, b in zip(ll_old, ll_new, strict=True)]
    _, ci_low, ci_high = paired_bootstrap(d, seed=seed_of(patch_id + cutoff.isoformat()))

    vetoes: list[str] = []
    if not ci_low > 0.0:
        vetoes.append("no_ci_improvement")
    brier_gain = _mean([brier(olds[i].p, olds[i].y) - brier(news[i].p, news[i].y) for i in ids])
    if brier_gain < -BRIER_TOL:
        vetoes.append("brier_worse")
    by_domain: dict[str, list[float]] = defaultdict(list)
    for i, di in zip(ids, d, strict=True):
        by_domain[olds[i].domain].append(-di)          # logloss_new − logloss_old
    for dom in sorted(by_domain):
        xs = by_domain[dom]
        if len(xs) >= MIN_DOMAIN and _mean(xs) > DOMAIN_TOL:
            vetoes.append(f"domain_regressed:{dom}")

    return GateResult(passed=not vetoes, vetoes=tuple(vetoes), utility_before=-_mean(ll_old),
                      utility_after=-_mean(ll_new), ci_low=ci_low, ci_high=ci_high, **common)
