"""A learner's self-report as typed evidence (stage 3 T10, design A7 #2 / #8, B9 S3-5).

「我会这个」「学过」「没学过」 is a claim of one kind (`self_claim`), recorded on the cells it names
at a fixed strength under the evidence kind `self`. K's own rules weigh it (`evidence_weight["self"]`
and what RSI-A learned for it); nothing here sets a band, declares a cell or skips a path. A claim
names cells one by one: there is no "every aspect" default, so 「我会这个」 never makes a whole point
known without answers.

The table is code (a core contract): no Skill text, tool description or harness key changes it."""

from collections.abc import Sequence
from typing import Literal

from evowiki.goals.contracts import Observation, Polarity

SelfClaim = Literal["studied", "partly", "can_do", "fluent", "never"]
SELF_CLAIMS: tuple[SelfClaim, ...] = ("studied", "partly", "can_do", "fluent", "never")
# claim -> (polarity, strength): 学过，不一定还记得 / 熟悉 (the 摸底 grid's) / 会用 (its 掌握) / 熟练 / 没学过.
# 熟悉 is a positive claim, weaker than 会用, not half wrong (user ruling 2026-11-04). Alone it leaves a cell
# 熟悉 (shaky); with one right answer the cell is known (BandParams), a right answer from nothing only likely.
# 「学过但忘了」 is not 熟悉: studied, and one right answer stops at likely.
SELF_CLAIM: dict[SelfClaim, tuple[Polarity, float]] = {
    "studied": ("+", 0.2),
    "partly": ("+", 0.75),
    "can_do": ("+", 0.9),
    "fluent": ("+", 1.0),
    "never": ("-", 0.5),
}
CLAIM_ZH: dict[SelfClaim, str] = {"studied": "学过，不一定还记得", "partly": "熟悉", "can_do": "会用",
                                  "fluent": "熟练", "never": "没学过"}
# the diagnosis self-report's option meanings (the probe module's `Means`) as claims; unsure: none
MEANS_CLAIM: dict[str, SelfClaim] = {"know": "can_do", "partly": "partly", "forgot": "studied", "dont": "never"}
# a level the goal flow reads from his words (S1's `said_level`, S2's enter@v1 `known` entries:
# 没学过 / 学过但忘了 / 还会 / 很熟练) as a claim: the same table, so a level says what a claim says
LEVEL_CLAIM: dict[str, SelfClaim] = {"unlearned": "never", "beginner": "studied",
                                     "intermediate": "can_do", "advanced": "fluent"}


def self_observation(claim: SelfClaim, cell: str, *, quote: str, evidence_ref: str
                     ) -> Observation:
    """One `self` observation of `claim` on `cell` at the table's strength."""
    polarity, strength = SELF_CLAIM[claim]
    return Observation(cell=cell, polarity=polarity, strength=strength, kind="self",
                       evidence_ref=evidence_ref, quote=quote)


def self_observations(claim: SelfClaim, cells: Sequence[str], *, quote: str,
                      evidence_ref: str) -> tuple[Observation, ...]:
    """The claim on each named cell (in order, once each)."""
    return tuple(self_observation(claim, c, quote=quote, evidence_ref=evidence_ref)
                 for c in dict.fromkeys(cells))
