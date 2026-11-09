"""Depth decisions (spec §5.1 step 3): section resolution and prerequisite treatment."""

from bisect import bisect_right
from datetime import datetime
from enum import StrEnum

from evowiki.belief.beta import Belief
from evowiki.content.models import PrereqKind
from evowiki.render.theta import RefresherParams, ResolutionParams


def need_score(b: Belief, now: datetime, p: ResolutionParams) -> float:
    """w_M(1−m) + w_U·u + w_R(1−R)."""
    return p.w_M * (1.0 - b.mastery) + p.w_U * b.uncertainty + p.w_R * (1.0 - b.recall(now))


def resolution_level(score: float, p: ResolutionParams) -> int:
    """Number of thresholds ≤ score: 0 (least detail) .. 4 (most detail)."""
    return bisect_right(p.level_thresholds, score)


class PrereqTreatment(StrEnum):
    REFRESHER = "refresher"    # one-sentence refresher rendered inline
    EXPANDABLE = "expandable"  # collapsed block the reader may open


def meets_bar(m: float, u: float, r: float, p: RefresherParams) -> bool:
    return m >= p.min_m and u <= p.max_u and r >= p.min_R


def prereq_treatment(
    kind: PrereqKind, m: float, u: float, r: float, p: RefresherParams
) -> PrereqTreatment:
    """A formal prerequisite below the bar gets a refresher; everything else is expandable."""
    if kind is PrereqKind.FORMAL and not meets_bar(m, u, r, p):
        return PrereqTreatment.REFRESHER
    return PrereqTreatment.EXPANDABLE


def treatment_for(kind: PrereqKind, b: Belief, now: datetime, p: RefresherParams) -> PrereqTreatment:
    return prereq_treatment(kind, b.mastery, b.uncertainty, b.recall(now), p)


def decision_flips(kind: PrereqKind, b: Belief, now: datetime, p: RefresherParams) -> bool:
    """Would resolving the uncertainty change the treatment?

    Compares the current treatment with the treatments at m − σ and m + σ, evaluated as if
    the probe had removed the uncertainty (u = 0).
    """
    m, u, r = b.mastery, b.uncertainty, b.recall(now)
    outcomes = {
        prereq_treatment(kind, m, u, r, p),
        prereq_treatment(kind, max(0.0, m - u), 0.0, r, p),
        prereq_treatment(kind, min(1.0, m + u), 0.0, r, p),
    }
    return len(outcomes) > 1
