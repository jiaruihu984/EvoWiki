"""The memory layer's shared contract (S3 imports it): where a remembered thing came from, what
it applies to, and the evidence that backs it. Shapes only; no I/O."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

Provenance = Literal["explicit_current", "explicit_history", "observed", "inferred"]
ScopeType = Literal["global", "goal", "page", "concept"]
ItemKind = Literal["preference", "about_me", "decision", "open_question"]
ItemStatus = Literal["active", "superseded", "resolved"]   # decision / open_question only
Role = Literal["user", "assistant"]


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceRef(_M):
    """Exactly one of a session message (with its session, its role, and optionally the verbatim
    quote of the learner's words in it) or a learning event."""

    message_id: str | None = None
    session_id: str | None = None
    role: Role | None = None
    quote: str | None = None
    event_id: str | None = None

    @model_validator(mode="after")
    def _one(self) -> "EvidenceRef":
        if (self.message_id is None) == (self.event_id is None):
            raise ValueError("evidence names exactly one of message_id / event_id")
        if self.message_id is not None and (self.role is None or self.session_id is None):
            raise ValueError("message evidence needs its session_id and role")
        if self.event_id is not None and (self.role or self.quote or self.session_id):
            raise ValueError("event evidence carries no role, quote or session")
        return self


class MemoryScope(_M):
    scope_type: ScopeType
    scope_id: str | None = None

    @model_validator(mode="after")
    def _id(self) -> "MemoryScope":
        if (self.scope_type == "global") != (self.scope_id is None):
            raise ValueError("global has no scope_id; goal / page / concept need one")
        return self


class PastTurn(_M):
    """One answered turn read back from the session log, whole."""

    session_id: str
    message_id: str
    at: AwareDatetime
    page_id: str | None
    user_text: str
    answer_text: str
