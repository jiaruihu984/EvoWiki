"""ε-exploration over section resolution levels (spec §5.1 step 5)."""

import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from evowiki.content.models import SegmentKind

EPSILON: Final = 0.05  # spec §4.7 system constant; not on the RSI-modifiable surface


@dataclass(frozen=True)
class Exploration:
    levels: dict[SegmentKind, int]
    explored: bool
    propensity: float
    section: SegmentKind | None = None


def explore(
    levels: Mapping[SegmentKind, int], rng: random.Random, epsilon: float = EPSILON
) -> Exploration:
    """With probability ε, shift one section's level by one step (uniform section, uniform
    feasible direction). propensity is the probability of the realized decision.
    """
    if not 0.0 <= epsilon < 1.0:
        raise ValueError("epsilon must be in [0, 1)")
    if not levels:
        raise ValueError("levels must not be empty")
    if rng.random() >= epsilon:
        return Exploration(dict(levels), explored=False, propensity=1.0 - epsilon)
    sections = list(levels)
    section = sections[rng.randrange(len(sections))]
    current = levels[section]
    steps = [s for s in (-1, 1) if 0 <= current + s <= 4]
    step = steps[rng.randrange(len(steps))]
    out = dict(levels)
    out[section] = current + step
    propensity = epsilon / len(sections) / len(steps)
    return Exploration(out, explored=True, propensity=propensity, section=section)
