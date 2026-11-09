from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from evowiki.simulator.verbalize import AskIntent


@dataclass(frozen=True)
class AnswerTruth:
    event_id: str
    item_id: str
    z: float
    p_correct: float
    stream: str = "practice"


@dataclass(frozen=True)
class LearnTruth:
    ts: datetime
    kc: str
    cause: Literal["initial", "page", "expand", "external"]


@dataclass(frozen=True)
class MisconceptionTruth:
    ts: datetime
    misconception_id: str
    change: Literal["initial", "acquired", "fixed"]


@dataclass(frozen=True)
class FrameTruth:
    ts: datetime
    frame_id: str
    cause: Literal["phase", "progress"]


@dataclass(frozen=True)
class SelfReportTruth:
    event_id: str
    concept_id: str
    true_level: float
    reported: float


@dataclass(frozen=True)
class AskTruth:
    event_id: str
    intent: AskIntent
    verbalizer_version: str


@dataclass(frozen=True)
class ExternalTruth:
    ts: datetime
    concept_id: str
    kcs_learned: tuple[str, ...]


@dataclass(frozen=True)
class NavigationTruth:
    ts: datetime
    target: str
    hub_id: str | None
    cost: float
    clicks: int = 0
    branching: int = 0
    backtracks: int = 0


@dataclass(frozen=True)
class HubTruth:
    event_id: str
    suggestion_id: str
    p_accept: float
    accepted: bool


@dataclass(frozen=True)
class SnapshotTruth:
    ts: datetime
    session_id: str
    known: frozenset[str]
    retrievability: tuple[tuple[str, float], ...]
    misconceptions: frozenset[str]
    active_frames: tuple[str, ...]


@dataclass(frozen=True)
class PageTruth:
    ts: datetime
    concept_id: str
    render_plan_id: str | None
    needed_depth: int
    shown_depth: int
    redundant_sections: int
    section_expansions: int
    visit_index: int = 0     # 0-based visit number of this learner to this concept


@dataclass(frozen=True)
class AttentionTruth:
    ts: datetime
    session_index: int
    concept_id: str
    render_plan_id: str | None
    slot_index: int          # 1-based position among displayed slots
    slot_type: str
    segment_id: str          # first segment of the slot (the CRN key)
    expanded: bool
    p_attend: float
    attended: bool
    capacity: float
    segment_ids: tuple[str, ...] = ()   # every segment of the slot, in display order
    visit_index: int = 0                # 0-based visit number of this learner to this concept
    u_attend: float | None = None       # CRN uniform of the attention draw; None = no draw
    visibility: str = "shown"           # the slot's displayed visibility: "shown" | "collapsed"


@dataclass(frozen=True)
class RelationTruth:
    ts: datetime
    session_index: int
    relation_id: str
    segment_id: str
    exposure_index: int
    kind: Literal["initial", "acquire_try", "trace"]
    p_acquire: float | None
    acquired: bool
    render_plan_id: str | None = None   # page on which the exposure happened (None: initial)
    concept_id: str | None = None       # concept of that page (None: initial)


@dataclass(frozen=True)
class HiddenFinalState:
    """Hidden learner state at the end of the study route. Evaluation-only (spec §4.4)."""

    ts: datetime
    last_session_start: datetime  # begin time of the last study session (post-test clock)
    learner_type: str
    seed: int
    decay_d: float
    tau: float
    rho_scale: float
    rel_decay_mult: float
    known: frozenset[str]
    traces: tuple[tuple[str, tuple[datetime, ...]], ...]
    rel_known: frozenset[str]
    rel_traces: tuple[tuple[str, tuple[datetime, ...]], ...]
    misconceptions: frozenset[str]


@dataclass(frozen=True)
class RelProbeTruth:
    """Hidden side of one relation probe (sim-relprobe-v1): the answer's model inputs."""

    event_id: str
    ts: datetime
    session_index: int
    relation_id: str
    known: bool
    rho_e: float
    z: float
    p_correct: float
    correct: bool
    render_plan_id: str | None = None


@dataclass
class GroundTruth:
    """Hidden-state log for evaluation only (recovery metrics, privileged reference)."""

    answers: list[AnswerTruth] = field(default_factory=list)
    learned: list[LearnTruth] = field(default_factory=list)
    misconceptions: list[MisconceptionTruth] = field(default_factory=list)
    frames: list[FrameTruth] = field(default_factory=list)
    self_reports: list[SelfReportTruth] = field(default_factory=list)
    asks: list[AskTruth] = field(default_factory=list)
    external: list[ExternalTruth] = field(default_factory=list)
    navigation: list[NavigationTruth] = field(default_factory=list)
    hubs: list[HubTruth] = field(default_factory=list)
    snapshots: list[SnapshotTruth] = field(default_factory=list)
    pages: list[PageTruth] = field(default_factory=list)
    attention: list[AttentionTruth] = field(default_factory=list)
    relations: list[RelationTruth] = field(default_factory=list)
    final: HiddenFinalState | None = None
    relprobe_log: list[RelProbeTruth] = field(default_factory=list)
