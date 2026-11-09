from typing import Literal

from pydantic import BaseModel, StrictBool

from evowiki.content.graph import ContentGraph
from evowiki.evidence.models import Event, EventType, Evidence, EvidenceType, Polarity

RULE_EXTRACTOR_VERSION = "rule-v1"


class QuizAnswerPayload(BaseModel):
    item_id: str
    correct: StrictBool


class SelfReportPayload(BaseModel):
    concept_id: str
    claim: Literal["know", "forgot"]


def _make(event: Event, index: int, concept_id: str, etype: EvidenceType, pol: Polarity) -> Evidence:
    return Evidence(
        id=f"{event.event_id}:{RULE_EXTRACTOR_VERSION}:{index}",
        source_event_id=event.event_id,
        extractor_version=RULE_EXTRACTOR_VERSION,
        concept_id=concept_id,
        evidence_type=etype,
        polarity=pol,
        extraction_confidence=1.0,
        ts=event.ts,
    )


def extract_rule_v1(event: Event, graph: ContentGraph) -> list[Evidence]:
    p = event.payload
    match event.type:
        case EventType.QUIZ_ANSWER:
            qa = QuizAnswerPayload.model_validate(p)
            item = graph.item(qa.item_id)
            etype = EvidenceType.QUIZ_CORRECT if qa.correct else EvidenceType.QUIZ_WRONG
            pol = Polarity.POSITIVE if qa.correct else Polarity.NEGATIVE
            return [_make(event, i, cid, etype, pol) for i, cid in enumerate(item.concept_ids)]
        case EventType.SELF_REPORT:
            sr = SelfReportPayload.model_validate(p)
            if sr.claim == "know":
                return [_make(event, 0, sr.concept_id, EvidenceType.SELF_REPORT_POSITIVE,
                              Polarity.POSITIVE)]
            return [_make(event, 0, sr.concept_id, EvidenceType.SELF_REPORT_FORGETTING,
                          Polarity.NEGATIVE)]
        case EventType.SEGMENT_EXPAND if p.get("role") == "prerequisite":
            return [_make(event, 0, p["concept_id"], EvidenceType.EXPAND_PREREQUISITE,
                          Polarity.NEGATIVE)]
        case EventType.SEGMENT_COLLAPSE if p.get("role") == "refresher":
            return [_make(event, 0, p["concept_id"], EvidenceType.COLLAPSE_REFRESHER,
                          Polarity.POSITIVE)]
        case _:
            return []
