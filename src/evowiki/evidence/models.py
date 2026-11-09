from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EventType(StrEnum):
    PAGE_VIEW = "page_view"
    SEGMENT_EXPAND = "segment_expand"
    SEGMENT_COLLAPSE = "segment_collapse"
    DWELL = "dwell"
    SCROLL = "scroll"
    ASK = "ask"
    QUIZ_ANSWER = "quiz_answer"
    SELF_REPORT = "self_report"
    NOTE_CREATED = "note_created"
    NOTE_EDITED = "note_edited"
    VIEW_SUGGESTION_ACCEPTED = "view_suggestion_accepted"
    VIEW_SUGGESTION_REJECTED = "view_suggestion_rejected"
    # Simulator-only observable channel (sim-relprobe-v1): answered relation probes. Never part
    # of the main event stream; no extractor or usage index consumes it.
    RELATION_PROBE_ANSWER = "relation_probe_answer"


class Event(BaseModel):
    """Raw, immutable interaction fact."""

    event_id: str
    user_id: str
    session_id: str
    seq: int
    ts: AwareDatetime
    type: EventType
    payload: dict[str, Any]
    render_plan_id: str | None = None


class EvidenceType(StrEnum):
    QUIZ_CORRECT = "quiz_correct"
    QUIZ_WRONG = "quiz_wrong"
    CORRECT_APPLICATION = "correct_application"
    EXPLICIT_CONFUSION = "explicit_confusion"
    MISCONCEPTION = "misconception"
    SELF_REPORT_POSITIVE = "self_report_positive"
    SELF_REPORT_FORGETTING = "self_report_forgetting"
    EXPAND_PREREQUISITE = "expand_prerequisite"
    COLLAPSE_REFRESHER = "collapse_refresher"


BEHAVIORAL_TYPES = frozenset({EvidenceType.EXPAND_PREREQUISITE, EvidenceType.COLLAPSE_REFRESHER})
RECALL_TYPES = frozenset(
    {EvidenceType.QUIZ_CORRECT, EvidenceType.QUIZ_WRONG, EvidenceType.CORRECT_APPLICATION}
)


class Polarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class Evidence(BaseModel):
    """Versioned interpretation of an event. extraction_confidence is NOT an impact weight."""

    model_config = ConfigDict(frozen=True)

    id: str
    source_event_id: str
    extractor_version: str
    concept_id: str
    evidence_type: EvidenceType
    polarity: Polarity
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    ts: AwareDatetime
    llm_call_id: str | None = None
    kc_hint: str | None = None
