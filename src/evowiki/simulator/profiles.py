from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.simulator.crn import CRN


class LearnerType(StrEnum):
    A = "A"  # strong math, weak ML — negative control
    B = "B"  # average math, fluent DL, adopts representation-learning frame late — negative control
    C = "C"  # beginner — negative control
    D = "D"  # systematically over-reports own knowledge — RSI-A positive control
    E = "E"  # forgets fast (large d) — RSI-A positive control
    F = "F"  # many misconceptions, sparse interaction — fully held out


TRAINING_TYPES: tuple[LearnerType, ...] = (
    LearnerType.A, LearnerType.B, LearnerType.C, LearnerType.D, LearnerType.E,
)
HELD_OUT_TYPES: tuple[LearnerType, ...] = (LearnerType.F,)


class LearnerProfile(BaseModel):
    """Hidden generative parameters of one simulated learner. Never visible to the system."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    learner_id: str
    learner_type: LearnerType
    seed: int
    decay_d: float = Field(gt=0.0, le=2.0)
    tau: float = Field(ge=-5.0, le=5.0)
    rho_scale: float = Field(gt=0.0, le=5.0)
    p_learn: float = Field(gt=0.0, le=1.0)
    p_repair: float = Field(ge=0.0, le=1.0)
    initial_known: tuple[tuple[str, float], ...]
    self_report_bias: float = Field(ge=-1.0, le=1.0)
    self_report_noise: float = Field(ge=0.0, le=1.0)
    self_report_rate: float = Field(ge=0.0, le=1.0)
    curiosity: float = Field(ge=0.0, le=1.0)
    ask_rate: float = Field(ge=0.0, le=1.0)
    misconception_rate: float = Field(ge=0.0, le=1.0)
    external_rate_per_day: float = Field(ge=0.0, le=1.0)
    concepts_per_session: int = Field(ge=1, le=5)
    session_gap_days: tuple[float, float]
    anchoring_gain: float = Field(ge=0.0, le=2.0)
    scheduled_frames: tuple[tuple[str, str], ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "LearnerProfile":
        if any(not 0.0 <= f <= 1.0 for _, f in self.initial_known):
            raise ValueError("initial_known fractions must be in [0, 1]")
        lo, hi = self.session_gap_days
        if not 1.0 <= lo <= hi:
            raise ValueError("session gaps must be at least 1 day and ordered (lo <= hi)")
        return self

    def known_fraction(self, domain: str) -> float:
        return dict(self.initial_known).get(domain, 0.0)


_RANGES: dict[str, tuple[float, float]] = {
    "decay_d": (0.4, 0.6),
    "tau": (-1.2, -0.8),
    "rho_scale": (0.3, 0.5),
    "p_learn": (0.45, 0.65),
    "p_repair": (0.3, 0.8),
    "self_report_bias": (-0.05, 0.05),
    "self_report_noise": (0.05, 0.15),
    "self_report_rate": (0.2, 0.4),
    "curiosity": (0.05, 0.15),
    "ask_rate": (0.1, 0.3),
    "misconception_rate": (0.05, 0.15),
    "external_rate_per_day": (0.02, 0.06),
    "anchoring_gain": (0.5, 1.0),
}
_TYPE_RANGES: dict[LearnerType, dict[str, tuple[float, float]]] = {
    LearnerType.A: {},
    LearnerType.B: {},
    LearnerType.C: {"p_learn": (0.35, 0.5)},
    LearnerType.D: {"self_report_bias": (0.25, 0.4)},
    LearnerType.E: {"decay_d": (0.75, 0.95)},
    LearnerType.F: {
        "misconception_rate": (0.4, 0.6),
        "ask_rate": (0.02, 0.08),
        "self_report_rate": (0.05, 0.1),
    },
}
_INITIAL_KNOWN: dict[LearnerType, tuple[tuple[str, float], ...]] = {
    LearnerType.A: (("la", 0.85), ("prob", 0.7), ("ml", 0.15), ("dl", 0.05)),
    LearnerType.B: (("la", 0.5), ("prob", 0.5), ("ml", 0.6), ("dl", 0.75)),
    LearnerType.C: (("la", 0.05), ("prob", 0.05), ("ml", 0.05), ("dl", 0.05)),
    LearnerType.D: (("la", 0.4), ("prob", 0.4), ("ml", 0.3), ("dl", 0.2)),
    LearnerType.E: (("la", 0.5), ("prob", 0.5), ("ml", 0.4), ("dl", 0.3)),
    LearnerType.F: (("la", 0.3), ("prob", 0.3), ("ml", 0.2), ("dl", 0.1)),
}
_PACE: dict[LearnerType, tuple[int, tuple[float, float]]] = {
    **{t: (2, (1.0, 3.0)) for t in TRAINING_TYPES},
    LearnerType.F: (1, (3.0, 7.0)),
}
_SCHEDULED: dict[LearnerType, tuple[tuple[str, str], ...]] = {
    LearnerType.B: (("representation_learning", "DL"),),
}


def sample_profile(learner_type: LearnerType, seed: int) -> LearnerProfile:
    """Draw a profile of `learner_type` deterministically from `seed`."""
    crn = CRN(seed)
    ranges = _RANGES | _TYPE_RANGES[learner_type]
    # One keyed draw per field: adding a field never shifts the others.
    values = {
        name: lo + (hi - lo) * crn.uniform("profile", learner_type.value, name)
        for name, (lo, hi) in ranges.items()
    }
    per_session, gaps = _PACE[learner_type]
    return LearnerProfile(
        learner_id=f"{learner_type.value}-{seed}",
        learner_type=learner_type,
        seed=seed,
        initial_known=_INITIAL_KNOWN[learner_type],
        concepts_per_session=per_session,
        session_gap_days=gaps,
        scheduled_frames=_SCHEDULED.get(learner_type, ()),
        **values,
    )


def cohort(
    types: Iterable[LearnerType], seeds: Iterable[int], *, include_held_out: bool = False
) -> list[LearnerProfile]:
    """Profiles for every (type, seed). Held-out types need include_held_out=True (final report)."""
    types = list(types)
    seeds = list(seeds)
    held = [t.value for t in types if t in HELD_OUT_TYPES]
    if held and not include_held_out:
        raise ValueError(
            f"types {held} are held out; pass include_held_out=True only for the final report"
        )
    return [sample_profile(t, s) for t in types for s in seeds]
