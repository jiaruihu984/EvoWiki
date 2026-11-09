"""The only LLM role in the slow loop (spec §6.3): name a hub. It goes through the Gateway, so
every call is logged and replayable. (The θ_rep⁰ perspective pickers were removed in Plan 4b-1b
Task 9; θ_rep¹ chooses perspectives deterministically in `view.perspective`.)"""

from collections.abc import Sequence
from typing import Annotated, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field

from evowiki.content.graph import ContentGraph
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import LLMRequest

HUB_NAME_PURPOSE: Final = "view.hub_name"
HUB_NAME_PROMPT_VERSION: Final = "view.hub_name.v1"
MAX_LABEL: Final = 60

HUB_NAME_SYSTEM = (
    "You name a personal grouping of concepts in one learner's wiki. The name only labels the "
    "group; it must not state or imply any new fact. Give a short English label and a short "
    "Chinese label (keep English technical terms). Output JSON only."
)

Label = Annotated[str, Field(min_length=1, max_length=MAX_LABEL)]


class HubName(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label_en: Label
    label_zh: Label


class HubNamer(Protocol):
    def name(self, members: Sequence[str]) -> tuple[HubName, str | None]: ...


def _fit(text: str, first: str, n: int) -> str:
    return text if len(text) <= MAX_LABEL else f"{first[:MAX_LABEL - 6]} +{n - 1}"


class TemplateHubNamer:
    """Deterministic names from member concept names (no LLM)."""

    def __init__(self, graph: ContentGraph):
        self._graph = graph

    def name(self, members: Sequence[str]) -> tuple[HubName, str | None]:
        ms = sorted(members)
        en = [self._graph.concept(m).name_en for m in ms]
        zh = [self._graph.concept(m).name_zh for m in ms]
        return HubName(label_en=_fit(" / ".join(en), en[0], len(ms)),
                       label_zh=_fit(" / ".join(zh), zh[0], len(ms))), None


class GatewayHubNamer:
    def __init__(self, gateway: Gateway, graph: ContentGraph):
        self._gw, self._graph = gateway, graph

    def name(self, members: Sequence[str]) -> tuple[HubName, str | None]:
        ms = sorted(members)
        lines = "\n".join(f"- {m}: {self._graph.concept(m).name_en} / "
                          f"{self._graph.concept(m).name_zh}" for m in ms)
        req = LLMRequest(
            purpose=HUB_NAME_PURPOSE, prompt_version=HUB_NAME_PROMPT_VERSION,
            subject="|".join(ms), system=HUB_NAME_SYSTEM,
            prompt=(f"Concepts in the group:\n{lines}\n"
                    'Return {"label_en": "...", "label_zh": "..."}, at most 60 characters each.'),
        )
        out, call = self._gw.call("generator", req, HubName)
        return out, call.id
