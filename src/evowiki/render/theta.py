from itertools import pairwise
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evowiki.content.models import SegmentKind
from evowiki.render.plan import SECTION_KINDS

ScoreWeight = Annotated[float, Field(ge=0.0, le=3.0)]
Threshold = Annotated[float, Field(ge=0.0, le=8.0)]


class ResolutionParams(BaseModel):
    """need = w_M(1−m) + w_U·u + w_R(1−R); level = #thresholds ≤ need (0..4)."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    w_M: ScoreWeight = 1.0
    w_U: ScoreWeight = 1.0
    w_R: ScoreWeight = 1.0
    level_thresholds: tuple[Threshold, Threshold, Threshold, Threshold] = (0.2, 0.4, 0.6, 0.85)

    @field_validator("level_thresholds")
    @classmethod
    def _strictly_increasing(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if any(a >= b for a, b in pairwise(v)):
            raise ValueError("level_thresholds must be strictly increasing")
        return v


class RefresherParams(BaseModel):
    """A prerequisite meets the bar iff m ≥ min_m and u ≤ max_u and R ≥ min_R."""

    model_config = ConfigDict(frozen=True)

    min_m: float = Field(default=0.6, ge=0.0, le=1.0)
    max_u: float = Field(default=0.2, ge=0.0, le=0.5)
    min_R: float = Field(default=0.5, ge=0.0, le=1.0)


class ThetaRender(BaseModel):
    """θ_rep.render (spec §4.6). Plan 4 wraps it with θ_rep.rules."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    resolution: ResolutionParams = Field(default_factory=ResolutionParams)
    refresher: RefresherParams = Field(default_factory=RefresherParams)
    order: tuple[SegmentKind, ...] = SECTION_KINDS
    max_examples: int = Field(default=2, ge=1, le=5)

    @field_validator("order")
    @classmethod
    def _permutation_of_sections(cls, v: tuple[SegmentKind, ...]) -> tuple[SegmentKind, ...]:
        if len(v) != len(SECTION_KINDS) or set(v) != set(SECTION_KINDS):
            raise ValueError("order must be a permutation of SECTION_KINDS")
        return v

    @classmethod
    def default(cls) -> "ThetaRender":
        return cls()
