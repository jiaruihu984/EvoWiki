from collections.abc import Set as AbstractSet
from math import exp


def sigmoid(x: float) -> float:
    """Overflow-safe logistic function."""
    if x >= 0.0:
        return 1.0 / (1.0 + exp(-x))
    e = exp(x)
    return e / (1.0 + e)


def jaccard(a: AbstractSet[str], b: AbstractSet[str]) -> float:
    """|a ∩ b| / |a ∪ b|; 0.0 when both are empty."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0
