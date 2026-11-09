"""ViewOps (spec §4.4): the atomic, replayable changes to a user's personal view V."""

from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from evowiki.view.roles import Role


class Mode(StrEnum):
    INSTRUCTIONAL = "instructional"
    STANDARD = "standard"
    COMPRESSED = "compressed"
    REFERENCE = "reference"


class OpKind(StrEnum):
    SET_MODE = "SET_MODE"  # legacy, load-only (Plan 7 fixture); never appended
    SET_ROLE = "SET_ROLE"
    SET_PERSPECTIVE = "SET_PERSPECTIVE"
    CREATE_HUB = "CREATE_HUB"
    ATTACH = "ATTACH"
    DETACH = "DETACH"
    SURFACE_LINK = "SURFACE_LINK"
    RETIRE_HUB = "RETIRE_HUB"
    MERGE_HUB = "MERGE_HUB"


# Spec §6.3: these always need user confirmation; ATTACH/DETACH only when they change the
# concept's primary attachment.
ALWAYS_STRUCTURAL: Final = frozenset({OpKind.CREATE_HUB, OpKind.MERGE_HUB, OpKind.RETIRE_HUB})
PRIMARY_SENSITIVE: Final = frozenset({OpKind.ATTACH, OpKind.DETACH})
DEFAULT_SALIENCE: Final = 0.5

Salience = Annotated[float, Field(gt=0.0, le=1.0)]
Label = Annotated[str, Field(min_length=1, max_length=60)]


class _Args(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SetMode(_Args):
    """Legacy θ_rep⁰ op. Load-only: old ViewLog lines still fold, `ViewLog.append` refuses it."""

    kind: Literal["SET_MODE"] = "SET_MODE"
    concept_id: str
    mode: Mode


class SetRole(_Args):
    """Persistent role of a concept in V (Stage I-B spec §1). support/bridge carry an anchor."""

    kind: Literal["SET_ROLE"] = "SET_ROLE"
    concept_id: str
    role: Role
    anchor: str | None = None

    @model_validator(mode="after")
    def _anchor(self) -> "SetRole":
        anchored = self.role in (Role.SUPPORT, Role.BRIDGE)
        if anchored != (self.anchor is not None):
            raise ValueError(f"role {self.role} {'requires' if anchored else 'forbids'} an anchor")
        if self.anchor == self.concept_id:
            raise ValueError("a concept cannot anchor itself")
        return self


class SetPerspective(_Args):
    kind: Literal["SET_PERSPECTIVE"] = "SET_PERSPECTIVE"
    concept_id: str
    segment_id: str


class CreateHub(_Args):
    kind: Literal["CREATE_HUB"] = "CREATE_HUB"
    hub_id: str
    label_en: Label
    label_zh: Label
    members: tuple[str, ...] = Field(min_length=2)
    support_paths: tuple[tuple[str, ...], ...] = ()
    salience: Salience = DEFAULT_SALIENCE

    @field_validator("members")
    @classmethod
    def _sorted_unique(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("hub members repeat a concept")
        return tuple(sorted(v))


class Attach(_Args):
    kind: Literal["ATTACH"] = "ATTACH"
    concept_id: str
    hub_id: str
    salience: Salience


class Detach(_Args):
    kind: Literal["DETACH"] = "DETACH"
    concept_id: str
    hub_id: str


class SurfaceLink(_Args):
    kind: Literal["SURFACE_LINK"] = "SURFACE_LINK"
    concept_id: str
    other_concept_id: str
    segment_id: str

    @model_validator(mode="after")
    def _distinct(self) -> "SurfaceLink":
        if self.concept_id == self.other_concept_id:
            raise ValueError("a link joins two distinct concepts")
        return self


class RetireHub(_Args):
    kind: Literal["RETIRE_HUB"] = "RETIRE_HUB"
    hub_id: str


class MergeHub(_Args):
    """Fold `absorbed_hub_id` into `hub_id`."""

    kind: Literal["MERGE_HUB"] = "MERGE_HUB"
    hub_id: str
    absorbed_hub_id: str

    @model_validator(mode="after")
    def _distinct(self) -> "MergeHub":
        if self.hub_id == self.absorbed_hub_id:
            raise ValueError("cannot merge a hub into itself")
        return self


OpArgs = Annotated[
    SetMode | SetRole | SetPerspective | CreateHub | Attach | Detach | SurfaceLink | RetireHub
    | MergeHub,
    Field(discriminator="kind"),
]


class Confirmation(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AUTO = "auto"


class ProposedBy(StrEnum):
    RULE = "rule"
    LLM = "llm"


class ViewOp(BaseModel):
    """One ViewOp as appended (spec §4.4). `confirmation` is the value at creation (pending or
    auto); the effective value lives in the ViewLog's decisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: Literal["op"] = "op"
    id: str
    user_id: str
    session_id: str
    seq: int = Field(ge=0)
    ts: AwareDatetime
    args: OpArgs
    triggered_by_rule_id: str | None = None
    proposed_by: ProposedBy = ProposedBy.RULE
    requires_confirmation: bool
    confirmation: Confirmation
    policy_version: str
    llm_call_ids: tuple[str, ...] = ()
    proposal_fingerprint: str | None = None                 # CREATE_HUB re-proposal identity
    evidence_snapshot: tuple[tuple[str, int], ...] = ()     # member evidence_count at proposal

    @model_validator(mode="after")
    def _creation_confirmation(self) -> "ViewOp":
        if self.args.kind in ALWAYS_STRUCTURAL and not self.requires_confirmation:
            raise ValueError(f"structural op {self.args.kind} always requires confirmation "
                             f"and can never be auto (spec §6.3)")
        expected = Confirmation.PENDING if self.requires_confirmation else Confirmation.AUTO
        if self.confirmation is not expected:
            raise ValueError(f"a new op with requires_confirmation={self.requires_confirmation} "
                             f"must be {expected.value}")
        return self

    @property
    def kind(self) -> OpKind:
        return OpKind(self.args.kind)


class Decision(BaseModel):
    """A user (or system) decision on an op: accept/reject a pending op, or undo an auto op."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: Literal["decision"] = "decision"
    op_id: str
    user_id: str
    session_id: str
    seq: int = Field(ge=0)
    ts: AwareDatetime
    accepted: bool
    reason: str | None = None


LogEntry = Annotated[ViewOp | Decision, Field(discriminator="entry")]
