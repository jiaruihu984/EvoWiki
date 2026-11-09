from collections.abc import Mapping
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evowiki.content.models import PrereqKind
from evowiki.evidence.models import EvidenceType
from evowiki.frozen import ro_dict as _ro

Weight = Annotated[float, Field(ge=0.0, le=3.0)]
StabilityDays = Annotated[float, Field(ge=0.5, le=365.0)]


class UpAlpha(BaseModel):
    model_config = ConfigDict(frozen=True)

    formal: float = Field(default=0.3, ge=0.0, le=1.0)
    pedagogical: float = Field(default=0.15, ge=0.0, le=1.0)

    def for_kind(self, kind: PrereqKind) -> float:
        return self.formal if kind is PrereqKind.FORMAL else self.pedagogical


class Forgetting(BaseModel):
    """The legacy concept-level Tracer's forgetting (belief/update.py, beta.py, coldstart.py).
    Aspect K does not use it: its memory is FSRS (belief/memory.py, user ruling 2026-10-24)."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    S0_by_difficulty: _ro(dict[int, StabilityDays]) = Field(
        default_factory=lambda: {1: 60.0, 2: 30.0, 3: 14.0, 4: 7.0, 5: 4.0}
    )
    growth: float = Field(default=2.0, ge=1.0, le=5.0)
    shrink: float = Field(default=0.5, ge=0.1, le=1.0)
    S_min: float = Field(default=0.5, ge=0.1, le=5.0)
    # Safety cap (like S_min): keeps stability finite for strict JSONB. Not an RSI-A path.
    S_max: float = Field(default=3650.0, ge=30.0, le=36500.0)

    @field_validator("S0_by_difficulty")
    @classmethod
    def _all_difficulties(cls, v: Mapping[int, float]) -> Mapping[int, float]:
        if set(v) != {1, 2, 3, 4, 5}:
            raise ValueError("S0_by_difficulty must define difficulties 1..5")
        return v

    @model_validator(mode="after")
    def _cap_above_initial(self) -> "Forgetting":
        if self.S_max < max(self.S0_by_difficulty.values()):
            raise ValueError("S_max must be >= every S0_by_difficulty value")
        return self


class BackgroundPrior(BaseModel):
    model_config = ConfigDict(frozen=True)

    mean: float = Field(ge=0.01, le=0.99)
    strength: float = Field(ge=0.5, le=50.0)


class ProbePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    uncertainty_threshold: float = Field(default=0.15, ge=0.0, le=0.5)
    max_per_session: int = Field(default=2, ge=0, le=10)


_DEFAULT_WEIGHTS: dict[EvidenceType, float] = {
    EvidenceType.QUIZ_CORRECT: 1.0,
    EvidenceType.QUIZ_WRONG: 1.0,
    EvidenceType.CORRECT_APPLICATION: 1.0,
    EvidenceType.EXPLICIT_CONFUSION: 0.7,
    EvidenceType.MISCONCEPTION: 0.8,
    EvidenceType.SELF_REPORT_POSITIVE: 0.2,
    EvidenceType.SELF_REPORT_FORGETTING: 0.5,
    EvidenceType.EXPAND_PREREQUISITE: 0.0,
    EvidenceType.COLLAPSE_REFRESHER: 0.0,
}

_DEFAULT_BACKGROUNDS: dict[str, dict[str, dict[str, float]]] = {
    "none": {},
    "university_math": {"la": {"mean": 0.7, "strength": 4.0}, "prob": {"mean": 0.6, "strength": 4.0}},
    "ml_beginner": {"la": {"mean": 0.75, "strength": 4.0}, "prob": {"mean": 0.65, "strength": 4.0},
                    "ml": {"mean": 0.4, "strength": 3.0}},
    "ml_experienced": {"la": {"mean": 0.85, "strength": 5.0}, "prob": {"mean": 0.8, "strength": 5.0},
                       "ml": {"mean": 0.75, "strength": 4.0}, "dl": {"mean": 0.4, "strength": 3.0}},
    "dl_experienced": {"la": {"mean": 0.85, "strength": 5.0}, "prob": {"mean": 0.8, "strength": 5.0},
                       "ml": {"mean": 0.8, "strength": 5.0}, "dl": {"mean": 0.75, "strength": 4.0}},
}


class ThetaInfer(BaseModel):
    model_config = ConfigDict(frozen=True, validate_default=True)

    evidence_weights: _ro(dict[EvidenceType, Weight])
    up_alpha: UpAlpha = Field(default_factory=UpAlpha)
    forgetting: Forgetting = Field(default_factory=Forgetting)
    slip: float = Field(default=0.1, ge=0.0, le=0.3)
    guess: float = Field(default=0.2, ge=0.0, le=0.5)
    prior_by_background: _ro(dict[str, _ro(dict[str, BackgroundPrior])]) = Field(
        default_factory=dict
    )
    default_prior: BackgroundPrior = Field(
        default_factory=lambda: BackgroundPrior(mean=0.3, strength=2.0)
    )
    probe: ProbePolicy = Field(default_factory=ProbePolicy)

    @field_validator("evidence_weights")
    @classmethod
    def _covers_all_types(cls, v: Mapping[EvidenceType, float]) -> Mapping[EvidenceType, float]:
        missing = set(EvidenceType) - set(v)
        if missing:
            raise ValueError(f"evidence_weights missing: {sorted(missing)}")
        return v

    def weight(self, t: EvidenceType) -> float:
        return self.evidence_weights[t]

    @classmethod
    def default(cls) -> "ThetaInfer":
        return cls.model_validate(
            {"evidence_weights": dict(_DEFAULT_WEIGHTS), "prior_by_background": _DEFAULT_BACKGROUNDS}
        )
