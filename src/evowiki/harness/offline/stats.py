"""The fixed decision rule of an offline round (spec §5.3, as corrected by the simulation in
`simulate.py`), an anchor like harness/anchors.py: only the user changes these numbers. The
bootstrap unit is the case: several samples of one case are not independent evidence.

- dev: the candidate's win-rate low bound > DEV_LOW;
- held-out: its mean > 0.5 and its low bound > HELD_LOW (same direction, not worse);
- exemplars: their mean score ≥ EXEMPLAR_MIN (one noisy loss does not sink a good change);
- A/A (two sample sets of the current pack): a health check, not a floor; a mean off 0.5 by more
  than AA_TOLERANCE means the pipeline favours one side and voids the round."""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from evowiki.harness.judge import Winner

DEV_LOW = 0.5
HELD_LOW = 0.45
EXEMPLAR_MIN = 0.45
AA_TOLERANCE = 0.08
RUBRIC_MARGIN = -0.02     # the rubric share may not drop by more than 2 points (CI low)
BOOTSTRAP = 2000
COST_FLAG = 1.5           # a candidate costing this × the current pack or more is flagged
_SCORE = {"a": 1.0, "tie": 0.5, "b": 0.0}


@dataclass(frozen=True)
class CaseResult:
    """One case: the judge's winners over its sample pairs (candidate = a, current pack = b)."""

    case_id: str
    exemplar: bool
    pairs: tuple[Winner, ...]
    rubric_cand: float | None = None      # mean rubric share over the candidate's samples
    rubric_base: float | None = None
    cost_cand: float = 0.0
    cost_base: float = 0.0
    len_cand: float = 0.0
    len_base: float = 0.0

    @property
    def win_score(self) -> float:
        return sum(_SCORE[w] for w in self.pairs) / len(self.pairs)

    @property
    def clear_loss(self) -> bool:
        return bool(self.pairs) and all(w == "b" for w in self.pairs)


def ci(values: Sequence[float], *, seed: int = 0, n: int = BOOTSTRAP) -> tuple[float, float, float]:
    """(mean, 2.5 %, 97.5 %) of the mean, bootstrapped over `values`."""
    v = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), size=(n, len(v)))].mean(axis=1)
    lo, hi = np.quantile(means, [0.025, 0.975])
    return float(v.mean()), float(lo), float(hi)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reasons: tuple[str, ...]
    win: tuple[float, float, float]                    # mean, low, high
    decisive: float                                    # share of pairs that were not ties
    rubric_delta: tuple[float, float, float] | None
    exemplar_mean: float | None
    cost_ratio: float | None
    len_ratio: float | None
    notes: tuple[str, ...]
    cases: int


def decide(results: Sequence[CaseResult], *, low_bar: float = DEV_LOW,
           mean_bar: float | None = None, seed: int = 0) -> Verdict:
    """Dev: `decide(results)`; held-out: `decide(results, low_bar=HELD_LOW, mean_bar=0.5)`."""
    problem = [r for r in results if not r.exemplar]
    if not problem:
        raise ValueError("no problem cases to decide on")
    win = ci([r.win_score for r in problem], seed=seed)
    pairs = [w for r in problem for w in r.pairs]
    decisive = sum(w != "tie" for w in pairs) / len(pairs)
    deltas = [r.rubric_cand - r.rubric_base for r in problem
              if r.rubric_cand is not None and r.rubric_base is not None]
    rub = ci(deltas, seed=seed + 1) if deltas else None
    ex = [r.win_score for r in results if r.exemplar]
    ex_mean = sum(ex) / len(ex) if ex else None
    cb, cc = sum(r.cost_base for r in results), sum(r.cost_cand for r in results)
    lb, lc = sum(r.len_base for r in results), sum(r.len_cand for r in results)
    cost_ratio = cc / cb if cb else None
    len_ratio = lc / lb if lb else None

    reasons: list[str] = []
    if win[1] <= low_bar:
        reasons.append(f"胜率下界 {win[1]:.2f} 没超过 {low_bar:.2f}")
    if mean_bar is not None and win[0] <= mean_bar:
        reasons.append(f"胜率 {win[0]:.2f} 没超过 {mean_bar:.2f}")
    if rub is not None and rub[1] < RUBRIC_MARGIN:
        reasons.append(f"rubric 变化下界 {rub[1]:+.3f} 低于 {RUBRIC_MARGIN:+.2f}")
    if ex_mean is not None and ex_mean < EXEMPLAR_MIN:
        reasons.append(f"范例段平均得分 {ex_mean:.2f} 低于 {EXEMPLAR_MIN}")
    notes = []
    if cost_ratio is not None and cost_ratio >= COST_FLAG:
        notes.append(f"花费是现在的 {cost_ratio:.1f} 倍，请你判断")
    return Verdict(not reasons, tuple(reasons) or ("通过",), win, decisive, rub, ex_mean,
                   cost_ratio, len_ratio, tuple(notes), len(problem))


def aa_check(results: Sequence[CaseResult]) -> tuple[bool, float]:
    """(healthy, mean win score) of an A/A run; unhealthy = |mean − 0.5| > AA_TOLERANCE."""
    scores = [r.win_score for r in results if not r.exemplar]
    if not scores:
        raise ValueError("no problem cases in the A/A run")
    m = sum(scores) / len(scores)
    return abs(m - 0.5) <= AA_TOLERANCE, m
