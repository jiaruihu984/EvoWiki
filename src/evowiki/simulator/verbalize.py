from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from evowiki.content.graph import ContentGraph
from evowiki.content.models import split_tag


class AskKind(StrEnum):
    KC_CONFUSION = "kc_confusion"
    MISCONCEPTION = "misconception"
    RELATION = "relation"


@dataclass(frozen=True)
class AskIntent:
    """Structured intent behind an `ask` event (hidden truth; spec §9.2)."""

    kind: AskKind
    concept_id: str
    kc: str | None = None
    misconception_id: str | None = None
    other_concept_id: str | None = None


class Verbalizer(Protocol):
    """Turns an AskIntent into question text. An LLM-backed verbalizer may implement this later."""

    version: str

    def verbalize(self, intent: AskIntent) -> str: ...


def _tail(tag: str | None) -> str:
    parts = split_tag(tag or "")
    return parts[1].replace("_", " ") if parts else ""


class TemplateVerbalizer:
    """Deterministic, LLM-free verbalizer."""

    version = "template-v1"

    def __init__(self, graph: ContentGraph):
        self._graph = graph

    def verbalize(self, intent: AskIntent) -> str:
        name = self._graph.concept(intent.concept_id).name_en
        match intent.kind:
            case AskKind.KC_CONFUSION:
                return f"I don't get the {_tail(intent.kc)} part of {name}. Can you explain it differently?"
            case AskKind.MISCONCEPTION:
                return f"Isn't {name} basically about '{_tail(intent.misconception_id)}'? That's how I read it."
            case AskKind.RELATION:
                other = self._graph.concept(intent.other_concept_id or "").name_en
                return f"How does {name} relate to {other}?"
