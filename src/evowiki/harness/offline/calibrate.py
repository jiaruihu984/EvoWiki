"""Judge calibration (spec §4): before a judge may gate anything, measure how often it agrees with a
person on labelled pairs, how often swapping the order changes its mind, and whether it prefers the
longer text more than the person does. The bars are anchors (only the user changes them)."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from evowiki.harness.judge import Winner

Label = Literal["a", "b", "tie"]
MIN_AGREEMENT = 0.75        # on pairs the person did not call a tie
MIN_KAPPA = 0.4             # Cohen's kappa over a / b / tie
MIN_POSITION = 0.85         # share of pairs whose two orders did not name opposite texts
MAX_LENGTH_EXCESS = 0.10    # judge's longer-text rate may exceed the person's by at most this


@dataclass(frozen=True)
class LabelledPair:
    human: Label
    judge: Winner
    flipped: bool            # the judge's two orders named opposite texts
    len_a: int
    len_b: int


@dataclass(frozen=True)
class Calibration:
    n: int
    agreement: float | None     # None: the person called every pair a tie
    kappa: float | None
    position: float
    length_excess: float | None
    passed: bool
    reasons: tuple[str, ...]


def kappa(xs: Sequence[str], ys: Sequence[str]) -> float | None:
    n = len(xs)
    if n == 0:
        return None
    cats = sorted(set(xs) | set(ys))
    po = sum(x == y for x, y in zip(xs, ys, strict=True)) / n
    pe = sum((xs.count(c) / n) * (ys.count(c) / n) for c in cats)
    return None if pe == 1 else (po - pe) / (1 - pe)


def _longer(p: LabelledPair, side: str) -> bool:
    return (p.len_a > p.len_b) if side == "a" else (p.len_b > p.len_a)


def calibrate(pairs: Sequence[LabelledPair]) -> Calibration:
    if not pairs:
        raise ValueError("no labelled pairs")
    decisive = [p for p in pairs if p.human != "tie"]
    agree = (sum(p.judge == p.human for p in decisive) / len(decisive)) if decisive else None
    k = kappa([p.human for p in pairs], [p.judge for p in pairs])
    position = 1 - sum(p.flipped for p in pairs) / len(pairs)
    both = [p for p in decisive if p.judge != "tie" and p.len_a != p.len_b]
    excess = None
    if both:
        judge_long = sum(_longer(p, p.judge) for p in both) / len(both)
        human_long = sum(_longer(p, p.human) for p in both) / len(both)
        excess = judge_long - human_long
    reasons = []
    if agree is None or agree < MIN_AGREEMENT:
        reasons.append(f"和人一致的比例 {agree if agree is None else round(agree, 2)} 低于 {MIN_AGREEMENT}")
    if k is None or k < MIN_KAPPA:
        reasons.append(f"kappa {k if k is None else round(k, 2)} 低于 {MIN_KAPPA}")
    if position < MIN_POSITION:
        reasons.append(f"换顺序不变的比例 {position:.2f} 低于 {MIN_POSITION}")
    if excess is not None and excess > MAX_LENGTH_EXCESS:
        reasons.append(f"比人更偏爱长文 {excess:+.2f}，超过 {MAX_LENGTH_EXCESS:+.2f}")
    return Calibration(len(pairs), agree, k, position, excess, not reasons,
                       tuple(reasons) or ("合格",))
