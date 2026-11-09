from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from evowiki.simulator.mathutil import sigmoid


@dataclass(frozen=True)
class ItemParams:
    """Hidden per-item 3PL parameters and KC weights w_qk (spec §9.2).

    `weights` is a tuple of (kc, w) pairs sorted by kc, so it stays hashable and picklable.
    """

    a: float
    b: float
    c: float
    weights: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if self.a <= 0.0:
            raise ValueError("discrimination a must be positive")
        if not 0.0 <= self.c < 1.0:
            raise ValueError("guessing c must be in [0, 1)")
        if not self.weights or any(w <= 0.0 for _, w in self.weights):
            raise ValueError("weights must be non-empty and positive")
        kcs = [k for k, _ in self.weights]
        if kcs != sorted(set(kcs)):
            raise ValueError("weights must be sorted by kc without duplicates")


def z_score(
    params: ItemParams,
    known: AbstractSet[str],
    rho: Mapping[str, float],
    active_misconceptions: AbstractSet[str],
    item_misconceptions: AbstractSet[str],
    delta: Mapping[str, float],
) -> float:
    """z_{u,q} = Σ_k w_qk·s_uk·ρ(A_uk) − Σ_m δ_m·𝟙[m active ∧ m ∈ tags(q)]  (spec §9.2)."""
    gain = sum(w * rho[k] for k, w in params.weights if k in known)
    penalty = sum(delta[m] for m in sorted(active_misconceptions & item_misconceptions))
    return gain - penalty


def p_correct(params: ItemParams, z: float) -> float:
    """3PL: P(Y=1) = c + (1 − c)·σ(a·(z − b))  (spec §9.2)."""
    return params.c + (1.0 - params.c) * sigmoid(params.a * (z - params.b))
