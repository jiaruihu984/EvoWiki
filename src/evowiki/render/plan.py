from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from evowiki.content.models import Lang, SegmentKind
from evowiki.frozen import ro_dict
from evowiki.view.roles import Role

# Segment kinds that form page sections of the target concept, in canonical order.
# perspective and connection belong to the personal view V (Plan 4).
SECTION_KINDS: tuple[SegmentKind, ...] = (
    SegmentKind.ONE_LINE,
    SegmentKind.INTUITION,
    SegmentKind.DEFINITION,
    SegmentKind.EXAMPLE,
    SegmentKind.DERIVATION,
    SegmentKind.MISCONCEPTION_FIX,
    SegmentKind.ADVANCED,
)

Level = Annotated[int, Field(ge=0, le=4)]


class RenderKind(StrEnum):
    PAGE = "page"
    ANSWER = "answer"


class Condition(StrEnum):
    """Stage I experiment arms (spec §10.3)."""

    S0 = "S0"  # static wiki: canonical full page
    S1 = "S1"  # S0 + page Q&A
    S2 = "S2"  # + K: depth adapted to beliefs, default organization
    S3 = "S3"  # + K + V: θ_rep¹ and the slow loop (Plan 4b-1b)


class OutcomeLabel(StrEnum):
    TOO_SHALLOW = "too_shallow"
    PREREQ_OMITTED = "prereq_omitted"
    REDUNDANT = "redundant"
    NOT_UNDERSTOOD = "not_understood"
    MISSED_CONNECTION = "missed_connection"
    SUCCESS = "success"


class Refresher(BaseModel):
    model_config = ConfigDict(frozen=True)

    concept_id: str
    segment_id: str


class Expandable(BaseModel):
    model_config = ConfigDict(frozen=True)

    segment_id: str
    concept_id: str
    role: Literal["prerequisite", "section"]


class Probe(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    concept_id: str
    predicted_p_correct: float = Field(ge=0.0, le=1.0)


class SlotType(StrEnum):
    ONE_LINE = "one_line"
    INTUITION = "intuition"
    DEFINITION = "definition"
    DERIVATION = "derivation"
    EXAMPLE = "example"
    MISCONCEPTION_FIX = "misconception_fix"
    ADVANCED = "advanced"
    CONNECTION = "connection"
    PERSPECTIVE = "perspective"
    PREREQ_REFRESHER = "prereq_refresher"
    PREREQ_LINK = "prereq_link"


SECTION_SLOT_TYPES: frozenset[SlotType] = frozenset(SlotType(k.value) for k in SECTION_KINDS)


class Visibility(StrEnum):
    SHOWN = "shown"
    COLLAPSED = "collapsed"
    OMIT = "omit"


class TemplateVisibility(StrEnum):
    """A template position's visibility rule (Stage I-B spec §1.2): K-driven, always shown, or
    collapsed."""

    K = "K"
    ALWAYS = "always"
    COLLAPSED = "collapsed"


class SlotSource(StrEnum):
    K = "K"
    ROLE = "role"
    CONTEXT = "context"
    SAFETY = "safety"


class RoleSource(StrEnum):
    DEFAULT = "default"
    CONTEXT = "context"


class PerspectiveSource(StrEnum):
    DEFAULT = "default"
    CONTEXT = "context"
    NONE = "none"


class Slot(BaseModel):
    """One position of a rendered page (Stage I-B spec §1.6). `k_visibility` records what K
    alone would have done, so the SafetyClamp can be audited; `source` says which layer set
    `visibility`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot_type: SlotType
    segment_ids: tuple[str, ...] = ()
    visibility: Visibility
    source: SlotSource
    k_visibility: Visibility | None = None
    relation_id: str | None = None
    concept_id: str | None = None
    skip_reason: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "Slot":
        if not self.segment_ids and (self.visibility is not Visibility.OMIT
                                     or self.skip_reason is None):
            raise ValueError("a slot without segments must be omitted with a skip_reason")
        if self.skip_reason is not None and self.segment_ids:
            raise ValueError("a skipped slot carries no segments")
        if len(set(self.segment_ids)) != len(self.segment_ids):
            raise ValueError("slot segment ids repeat")
        if (self.slot_type in (SlotType.CONNECTION, SlotType.PERSPECTIVE)
                and len(self.segment_ids) > 1):
            # attention is keyed by a slot's first segment: one segment per slot keeps S2 and
            # S3 draws aligned (CRN, Stage I-B spec §4.3)
            raise ValueError(f"{self.slot_type.value} slots carry exactly one segment")
        if self.slot_type is SlotType.CONNECTION and self.segment_ids and not self.relation_id:
            raise ValueError("connection slots need a relation_id")
        if (self.slot_type in (SlotType.PREREQ_REFRESHER, SlotType.PREREQ_LINK)
                and self.concept_id is None):
            raise ValueError("prerequisite slots need the prerequisite concept_id")
        return self


class RenderPlan(BaseModel):
    """Structured record of one page/answer rendering decision (spec §4.5)."""

    model_config = ConfigDict(frozen=True)

    id: str
    user_id: str
    session_id: str
    concept_id: str
    kind: RenderKind = RenderKind.PAGE
    condition: Condition
    policy_version: str
    view_version: str | None = None
    ts: AwareDatetime
    lang: Lang
    # V hook points: set only by the S3 branch (Plan 4); None otherwise.
    primary_hub_id: str | None = None
    perspective_segment_id: str | None = None
    surfaced_links: tuple[str, ...] | None = None
    resolution_by_section: ro_dict(dict[SegmentKind, Level])
    segments_selected: tuple[str, ...]
    refreshers: tuple[Refresher, ...] = ()
    expandables: tuple[Expandable, ...] = ()
    probe: Probe | None = None
    bridge_llm_call_ids: tuple[str, ...] = ()
    explored: bool = False
    propensity: float = Field(gt=0.0, le=1.0)
    outcome_labels: tuple[OutcomeLabel, ...] = ()
    role_default: Role | None = None
    role_requested: Role | None = None
    role_effective: Role | None = None
    role_source: RoleSource | None = None
    role_anchor: str | None = None
    perspective_default: str | None = None
    perspective_effective: str | None = None
    perspective_source: PerspectiveSource | None = None
    slots: tuple[Slot, ...] = ()
    collapse_blocked: bool = False

    @field_validator("resolution_by_section")
    @classmethod
    def _section_keys(cls, v):
        bad = set(v) - set(SECTION_KINDS)
        if bad:
            raise ValueError(f"not page sections: {sorted(bad)}")
        return v

    @field_validator("segments_selected", "outcome_labels")
    @classmethod
    def _unique(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("entries must be unique")
        return v

    @field_validator("slots")
    @classmethod
    def _slot_segments_unique(cls, v):
        ids = [s for slot in v for s in slot.segment_ids]
        if len(set(ids)) != len(ids):
            raise ValueError("a segment appears in more than one slot")
        return v

    @field_validator("surfaced_links")
    @classmethod
    def _unique_links(cls, v):
        if v is not None and len(set(v)) != len(v):
            raise ValueError("surfaced_links must be unique")
        return v

    @field_validator("refreshers")
    @classmethod
    def _one_refresher_per_concept(cls, v):
        if len({r.concept_id for r in v}) != len(v):
            raise ValueError("at most one refresher per concept")
        return v

    @property
    def qa_enabled(self) -> bool:
        return self.condition is not Condition.S0

    def shown_segment_ids(self) -> frozenset[str]:
        """Segments rendered inline: every segment of a SHOWN slot, plus (legacy fields)
        selected sections, refreshers and, on S3 pages, the perspective segment and surfaced
        connection segments."""
        shown = set(self.segments_selected) | {r.segment_id for r in self.refreshers}
        shown |= {sid for slot in self.slots if slot.visibility is Visibility.SHOWN
                  for sid in slot.segment_ids}
        if self.perspective_segment_id is not None:
            shown.add(self.perspective_segment_id)
        shown |= set(self.surfaced_links or ())
        return frozenset(shown)

    def with_labels(self, labels: Iterable[OutcomeLabel]) -> "RenderPlan":
        wanted = set(labels)
        data = self.model_dump()
        data["outcome_labels"] = [lb for lb in OutcomeLabel if lb in wanted]
        return RenderPlan.model_validate(data)
