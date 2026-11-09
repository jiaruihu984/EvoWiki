"""A2 (layer 2 of RSI-A): choose the A1 fitter's model structure M by held-out likelihood. No LLM.

M = which θ_infer paths are fitted, how strongly the hierarchical prior pulls, which fitting
algorithm (`FitModel`). `neighbours(m)` lists the one-step moves from m; `compare` fits two models
on the outcomes before a cutoff (`fit_infer`, the A1 fitter itself) and scores both on the outcomes
at or after it with the same prequential predictions `gate_a` uses: paired per-outcome log-loss
differences (a − b, so + means b predicts better) with a `metrics.paired_bootstrap` interval,
and both AUCs. `select_model` keeps the neighbour with the largest mean gain among those whose
interval lies above 0; a neighbour that adds a path must also gain at least ADD_MIN_GAIN (a
complexity penalty); ties go to fewer paths. None = no neighbour is better: keep m.

Fits see only ts < cutoff, so the score window is held out. The current model is fitted from
`start` (the cycle passes the default θ, so values fitted earlier on the score window cannot leak
in); every neighbour is fitted from the current model's train-window fit, so a path a neighbour
does not fit keeps that value — the move that gets applied (A1 keeps a dropped path at the user's
current value), judged without seeing the score window.

Multiple comparisons: `select_model` tests every neighbour at the Bonferroni level ALPHA / k
(k = the neighbours evaluated), with SELECT_BOOT bootstrap resamples so the far tail has enough of
them.

`A2_ENABLED` gates the cycle's A2 runs. On since the offline validation on public data passed
(the product specification not worse than A1 alone on ASSISTments,
75 % of its changes helped on test, synthetic false-pick rate 0.5 %; the local Eedi check agrees).
Tests run with it off (tests/conftest.py) unless they turn it on. Applied A2 changes are
monitored by the cycle either way."""

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime

from evowiki.belief.aspects import AspectConfig, AspectEvent
from evowiki.teach import anchors
from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import FitModel, InferParams
from evowiki.teach.fit.fitter import fit_infer
from evowiki.teach.fit.priors import PathPrior
from evowiki.teach.metrics import log_loss, paired_bootstrap, seed_of
from evowiki.teach.predict import predict_outcomes

A2_ENABLED = True           # Task 14: offline validation on public data passed
A2_MIN_OUTCOMES = 60        # held-out outcomes needed before any structure comparison
A2_EVERY = 30               # new outcomes between two A2 runs
ADD_MIN_GAIN = 0.002        # a neighbour that adds a path must gain at least this mean log-loss
ALPHA = 0.05                # family-wise level of select_model (each neighbour at ALPHA / k)
SELECT_BOOT = 4000          # bootstrap resamples per neighbour in select_model
RECENCY_HALF_LIFE_DAYS = 60.0
PRIOR_MIN, PRIOR_MAX = 0.125, 8.0     # FitModel.prior_strength bounds
_ALGORITHMS = ("coord_map@1", "coord_map_recency@1")
_TIE = 1e-12

Pairs = list[tuple[float, float]]
StructureKey = tuple[frozenset[str], float, str, float | None]


def structure_key(m: FitModel) -> StructureKey:
    """A FitModel's structure without its version: the fitted paths (as a set), the prior strength,
    the algorithm and its recency half-life. Two versions with the same key fit the same way."""
    return (frozenset(m.paths), m.prior_strength, m.algorithm, m.recency_half_life_days)


def neighbours(m: FitModel) -> list[tuple[str, FitModel]]:
    """The one-step moves from `m`, in a fixed order: add each whitelisted path not fitted, drop
    each fitted path (when more than one), prior strength ×2 / ÷2 (within bounds), the other
    fitting algorithm (recency half-life RECENCY_HALF_LIFE_DAYS). Each keeps `m.version`."""
    out: list[tuple[str, FitModel]] = []
    for path in ip.PATHS:
        if path not in m.paths:
            out.append((f"add {path}", m.model_copy(update={"paths": (*m.paths, path)})))
    if len(m.paths) > 1:
        for path in m.paths:
            out.append((f"drop {path}", m.model_copy(
                update={"paths": tuple(p for p in m.paths if p != path)})))
    if m.prior_strength * 2 <= PRIOR_MAX:
        out.append(("prior ×2", m.model_copy(update={"prior_strength": m.prior_strength * 2})))
    if m.prior_strength / 2 >= PRIOR_MIN:
        out.append(("prior ÷2", m.model_copy(update={"prior_strength": m.prior_strength / 2})))
    other = next(a for a in _ALGORITHMS if a != m.algorithm)
    out.append((f"algorithm {other}", m.model_copy(update={
        "algorithm": other,
        "recency_half_life_days": RECENCY_HALF_LIFE_DAYS if other == "coord_map_recency@1" else None})))
    # re-validate: model_copy skips the validators
    return [(label, FitModel.model_validate(x.model_dump())) for label, x in out]


def _held_out(events: Sequence[AspectEvent], base: AspectConfig, start: InferParams,
              domain_of: Mapping[str, str], model: FitModel, priors: Mapping[str, PathPrior],
              cutoff: datetime) -> tuple[Pairs, InferParams]:
    """((p, y) of every outcome at or after `cutoff`, the fitted θ) for `model` fitted on
    ts < cutoff from `start`."""
    fit = fit_infer(events, base, start, domain_of, model=model, priors=priors, cutoff=cutoff)
    pairs = [(o.p, o.y) for o in predict_outcomes(events, base, fit.params, domain_of)
             if o.ts >= cutoff]
    if not pairs:
        raise ValueError("no held-out outcomes at or after the cutoff: nothing to compare")
    return pairs, fit.params


def _compare_pairs(pa: Pairs, pb: Pairs, seed: int, *, alpha: float = ALPHA,
                   n_boot: int = 2000) -> dict:
    if len(pa) != len(pb) or any(ya != yb for (_, ya), (_, yb) in zip(pa, pb, strict=True)):
        raise ValueError("held-out outcomes of the two models do not pair")
    diffs = [log_loss(p, y) - log_loss(q, y) for (p, y), (q, _) in zip(pa, pb, strict=True)]
    mean, lo, hi = paired_bootstrap(diffs, seed=seed, n=n_boot, alpha=alpha)
    return {"ci_low": lo, "ci_high": hi, "mean": mean, "n": len(diffs),
            "auc_a": anchors.auc(pa), "auc_b": anchors.auc(pb)}


def compare(events: Sequence[AspectEvent], base: AspectConfig, start: InferParams,
            domain_of: Mapping[str, str], a: FitModel, b: FitModel,
            priors: Mapping[str, PathPrior], *, cutoff: datetime, seed: str) -> dict:
    """Fit `a` on ts < cutoff from `start`, then `b` on ts < cutoff from a's fitted θ (paths b
    does not fit keep a's train-window values, as they would keep the current values when b
    replaces a); score both on ts ≥ cutoff: {"ci_low", "ci_high", "mean", "n", "auc_a", "auc_b"};
    mean / ALPHA-level interval of log_loss(a) − log_loss(b) (+ = b better)."""
    pa, fitted = _held_out(events, base, start, domain_of, a, priors, cutoff)
    pb, _ = _held_out(events, base, fitted, domain_of, b, priors, cutoff)
    return _compare_pairs(pa, pb, seed_of(seed))


def select_model(events: Sequence[AspectEvent], base: AspectConfig, start: InferParams,
                 domain_of: Mapping[str, str], current: FitModel,
                 priors: Mapping[str, PathPrior], *, cutoff: datetime,
                 seed: str, banned: Collection[StructureKey] = ()
                 ) -> tuple[str, FitModel, dict] | None:
    """(label, model, comparison of current vs it) of the best neighbour that predicts the
    held-out outcomes strictly better: ci_low > 0 at the Bonferroni level ALPHA / k (k = the
    neighbours evaluated, so ALPHA bounds the chance of picking any neighbour when the current
    model is right); an added path also needs mean ≥ ADD_MIN_GAIN. Largest mean first, ties →
    fewer paths, then the label. None when none is better. The current model is fitted once from
    `start`; each neighbour once, from the current model's train-window fit (like `compare`).
    The comparison dict also carries "alpha" and "k". `banned`: structures (`structure_key`) of
    A2 changes this user undid or that were rolled back automatically; such a neighbour is never
    evaluated or picked again (so 撤销 sticks), and k counts only the neighbours evaluated."""
    moves = [(label, m) for label, m in neighbours(current) if structure_key(m) not in banned]
    if not moves:
        return None
    base_pairs, fitted = _held_out(events, base, start, domain_of, current, priors, cutoff)
    alpha = ALPHA / len(moves)
    best: tuple[tuple[float, int, str], str, FitModel, dict] | None = None
    for label, m in moves:
        pairs, _ = _held_out(events, base, fitted, domain_of, m, priors, cutoff)
        cmp = _compare_pairs(base_pairs, pairs, seed_of(f"{seed}/{label}"), alpha=alpha,
                             n_boot=SELECT_BOOT)
        if not cmp["ci_low"] > 0.0:
            continue
        if label.startswith("add ") and cmp["mean"] < ADD_MIN_GAIN:
            continue
        key = (-round(cmp["mean"] / _TIE) * _TIE, len(m.paths), label)
        if best is None or key < best[0]:
            best = (key, label, m, {**cmp, "alpha": alpha, "k": len(moves)})
    return None if best is None else (best[1], best[2], best[3])
