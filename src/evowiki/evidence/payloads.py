"""Typed payloads for behavioral and ask events consumed by the Outcome Labeler."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SegmentExpandPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    segment_id: str
    concept_id: str
    role: Literal["prerequisite", "section", "connection", "perspective"]


class SegmentCollapsePayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    segment_id: str
    concept_id: str
    role: Literal["refresher", "section"]


class RelationProbePayload(BaseModel):
    """An answered relation probe (sim-relprobe-v1): which relation, and whether correct."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation_id: str
    item_id: str
    correct: bool


class DwellPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    segment_id: str
    seconds: float = Field(ge=0.0)


class AskPayload(BaseModel):
    """An in-page question. The structured fields are the question's intent.

    The simulator fills them directly; in production the LLM extractor fills them (Plan 5).
    """

    model_config = ConfigDict(frozen=True)

    text: str
    about_concept_ids: tuple[str, ...] = ()
    relation: tuple[str, str] | None = None

    @model_validator(mode="after")
    def _distinct_relation(self) -> "AskPayload":
        if self.relation is not None and self.relation[0] == self.relation[1]:
            raise ValueError("relation must name two distinct concepts")
        return self


class NotePayload(BaseModel):
    """A user note on a concept page (spec §4.2: raw event; the LLM extractor classifies it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    note_id: str
    concept_id: str
    text: str = Field(min_length=1)
