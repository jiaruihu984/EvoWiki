import re
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Relation(StrEnum):
    PREREQUISITE_OF = "prerequisite_of"
    RELATED_TO = "related_to"
    PART_OF = "part_of"


class PrereqKind(StrEnum):
    FORMAL = "formal"
    PEDAGOGICAL = "pedagogical"


class ReviewStatus(StrEnum):
    AUTO_VERIFIED = "auto_verified"
    HUMAN_APPROVED = "human_approved"
    FLAGGED = "flagged"


PUBLISHABLE = frozenset({ReviewStatus.AUTO_VERIFIED, ReviewStatus.HUMAN_APPROVED})
# Plan 3 renderer name for the same set.
RENDERABLE_STATUSES = PUBLISHABLE


class LicenseTerms(BaseModel):
    model_config = ConfigDict(frozen=True)

    derivative_allowed: bool
    redistribution_allowed: bool
    commercial_allowed: bool
    attribution_required: bool
    share_alike: bool


def _terms(d: bool, r: bool, c: bool, a: bool, s: bool) -> LicenseTerms:
    return LicenseTerms(derivative_allowed=d, redistribution_allowed=r, commercial_allowed=c,
                        attribution_required=a, share_alike=s)


LICENSE_TERMS: dict[str, LicenseTerms] = {
    "CC-BY-4.0": _terms(True, True, True, True, False),
    "CC-BY-SA-4.0": _terms(True, True, True, True, True),
    "CC-BY-NC-4.0": _terms(True, True, False, True, False),
    "CC-BY-NC-SA-4.0": _terms(True, True, False, True, True),
    "Apache-2.0": _terms(True, True, True, True, False),
    "MIT": _terms(True, True, True, True, False),
    "arXiv-nonexclusive-1.0": _terms(False, False, False, True, False),
    "all-rights-reserved": _terms(False, False, False, True, False),
}


class Source(BaseModel):
    id: str
    title: str
    locator: str
    license: str
    derivative_allowed: bool
    redistribution_allowed: bool
    commercial_allowed: bool
    attribution_required: bool
    share_alike: bool
    # Last successful license verification (history; a failed check never erases it).
    license_verified_at: AwareDatetime | None = None
    license_url: str | None = None
    # Outcome and time of the most recent check (current state).
    license_check_status: Literal["verified", "unavailable", "mismatch"] | None = None
    license_checked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _flags_match_license(self) -> "Source":
        terms = LICENSE_TERMS.get(self.license)
        if terms is None:
            if self.derivative_allowed:
                raise ValueError(f"unknown license {self.license!r} cannot allow derivatives")
            return self
        actual = LicenseTerms(
            derivative_allowed=self.derivative_allowed,
            redistribution_allowed=self.redistribution_allowed,
            commercial_allowed=self.commercial_allowed,
            attribution_required=self.attribution_required,
            share_alike=self.share_alike,
        )
        if actual != terms:
            raise ValueError(f"license flags do not match {self.license}: expected {terms}")
        return self


class Passage(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    source_id: str
    locator: str
    text: str = Field(min_length=1)


class Concept(BaseModel):
    id: str
    name_en: str
    name_zh: str
    domain: str
    difficulty: int = Field(ge=1, le=5)
    aliases: list[str] = Field(default_factory=list)
    content_version: str | None = None


class Edge(BaseModel):
    src: str
    dst: str
    relation: Relation
    kind: PrereqKind | None = None
    strength: float = Field(ge=0.0, le=1.0)
    citations: list[str] = Field(default_factory=list)
    provenance: str | None = None
    review_status: ReviewStatus = ReviewStatus.HUMAN_APPROVED

    @model_validator(mode="after")
    def _kind_matches_relation(self) -> "Edge":
        if self.relation is Relation.PREREQUISITE_OF and self.kind is None:
            raise ValueError("prerequisite_of edges require kind")
        if self.relation is not Relation.PREREQUISITE_OF and self.kind is not None:
            raise ValueError("only prerequisite_of edges may set kind")
        return self


class SegmentKind(StrEnum):
    ONE_LINE = "one_line"
    INTUITION = "intuition"
    DEFINITION = "definition"
    DERIVATION = "derivation"
    EXAMPLE = "example"
    ADVANCED = "advanced"
    PERSPECTIVE = "perspective"
    CONNECTION = "connection"
    MISCONCEPTION_FIX = "misconception_fix"


BASE_SEGMENT_KINDS: tuple[SegmentKind, ...] = (
    SegmentKind.ONE_LINE, SegmentKind.INTUITION, SegmentKind.DEFINITION,
    SegmentKind.DERIVATION, SegmentKind.EXAMPLE, SegmentKind.ADVANCED,
)


class Lang(StrEnum):
    EN = "en"
    ZH = "zh"


class CitedSentence(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    citations: list[str] = Field(min_length=1)


class LicenseObligations(BaseModel):
    model_config = ConfigDict(frozen=True)

    attribution: list[str]
    share_alike: bool
    non_commercial: bool


class Segment(BaseModel):
    """Canonical content segment (spec 4.1). The renderer composes pages from segment metadata only.

    Pipeline content carries cited `sentences`; `body` and `citations` are then derived from them.
    A segment without sentences (hand-written renderer seed) keeps a free-form `body`.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    concept_id: str
    kind: SegmentKind
    lang: Lang
    review_status: ReviewStatus
    body: str = ""
    citations: tuple[str, ...] = ()
    sentences: tuple[CitedSentence, ...] = ()
    license_obligations: LicenseObligations | None = None
    content_version: str | None = None
    translated_from: str | None = None

    @model_validator(mode="after")
    def _derive_from_sentences(self) -> "Segment":
        if not self.sentences:
            return self
        sep = "" if self.lang is Lang.ZH else " "
        body = sep.join(s.text for s in self.sentences)
        citations = tuple(sorted({c for s in self.sentences for c in s.citations}))
        fields = self.model_fields_set
        if "body" in fields and self.body not in ("", body):
            raise ValueError(f"segment {self.id} body disagrees with its sentences")
        if "citations" in fields and self.citations not in ((), citations):
            raise ValueError(f"segment {self.id} citations disagree with its sentences")
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "citations", citations)
        if self.lang is Lang.ZH and self.translated_from is None:
            raise ValueError("zh segments require translated_from")
        if self.lang is Lang.EN and self.translated_from is not None:
            raise ValueError("en segments must not set translated_from")
        return self


class Perspective(BaseModel):
    segment_id: str
    concept_id: str
    framing_label: str
    presumes_required: list[str] = Field(default_factory=list)
    presumes_helpful: list[str] = Field(default_factory=list)
    covers_relation_ids: list[str] = Field(default_factory=list)
    covers_review_status: ReviewStatus | None = None
    # Reviewed field of study the perspective frames its concept through (Stage I-B readiness
    # classifies by this label only, never by the phases inside A(p)).
    framing_phase: Literal["LA", "ML", "DL"] | None = None
    framing_phase_status: ReviewStatus | None = None

    @model_validator(mode="after")
    def _presumes_consistent(self) -> "Perspective":
        req, helpful = set(self.presumes_required), set(self.presumes_helpful)
        if self.concept_id in req | helpful:
            raise ValueError("a perspective cannot presume its own concept")
        if req & helpful:
            raise ValueError(f"presumes_required and presumes_helpful overlap: {req & helpful}")
        if len(set(self.covers_relation_ids)) != len(self.covers_relation_ids):
            raise ValueError("covers_relation_ids repeat a relation")
        if self.covers_relation_ids and self.covers_review_status is None:
            raise ValueError("non-empty covers_relation_ids require covers_review_status")
        if self.framing_phase is not None and self.framing_phase_status is None:
            raise ValueError("a framing_phase requires framing_phase_status")
        if self.framing_phase_status is not None and self.framing_phase is None:
            raise ValueError("a framing_phase_status requires a framing_phase")
        return self


class Connection(BaseModel):
    """Metadata for a connection segment: the concept path it explains (at most 3 hops)."""

    model_config = ConfigDict(frozen=True)

    segment_id: str
    path: tuple[str, ...] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def _distinct_path(self) -> "Connection":
        if len(set(self.path)) != len(self.path):
            raise ValueError("connection path repeats a concept")
        return self


_TAG = re.compile(r"([^/:]+)[/:](.+)")


def split_tag(tag: str) -> tuple[str, str] | None:
    """Split a KC/misconception tag into (concept_id, slug).

    The pipeline writes '<concept_id>/<slug>'; the Plan 2 simulator fixtures use
    '<concept_id>:<name>'. Both are accepted; returns None for a malformed tag.
    """
    m = _TAG.fullmatch(tag)
    return (m.group(1), m.group(2)) if m else None


class QuizItem(BaseModel):
    id: str
    concept_ids: list[str] = Field(min_length=1)
    kc_tags: list[str] = Field(default_factory=list)
    misconception_tags: list[str] = Field(default_factory=list)
    difficulty: int = Field(ge=1, le=5)
    kind: Literal["recall", "application", "transfer"]
    answer_verified_by_human: bool = False
    stem: str | None = None
    answer_key: str | None = None
    core: bool = False
    tests_connection_id: str | None = None
    citations: list[str] = Field(default_factory=list)
    content_version: str | None = None
    review_status: ReviewStatus = ReviewStatus.HUMAN_APPROVED
    transfer_kind: Literal["seen", "near"] | None = None

    @model_validator(mode="after")
    def _tags_name_own_concepts(self) -> "QuizItem":
        for tag in [*self.kc_tags, *self.misconception_tags]:
            parts = split_tag(tag)
            if parts is None or parts[0] not in self.concept_ids:
                raise ValueError(f"tag {tag!r} must be '<concept_id>/<slug>' (or "
                                 f"'<concept_id>:<slug>') for one of {self.concept_ids}")
        if self.transfer_kind is not None and (
                self.kind != "transfer" or self.tests_connection_id is None):
            raise ValueError("transfer_kind requires kind='transfer' and a tests_connection_id")
        return self


class ContentVersion(BaseModel):
    id: str
    created_at: AwareDatetime
    snapshot_ref: str


class MisconceptionFix(BaseModel):
    """Content link (spec §9.2): a misconception_fix segment and the misconception tags it repairs.

    Tags use the quiz-item tag forms ('<concept_id>/<slug>' or '<concept_id>:<slug>').
    """

    model_config = ConfigDict(frozen=True)

    segment_id: str
    misconception_tags: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_tags(self) -> "MisconceptionFix":
        if len(set(self.misconception_tags)) != len(self.misconception_tags):
            raise ValueError("misconception_tags repeat a tag")
        return self
