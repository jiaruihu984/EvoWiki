"""Ports the reading assistant's turn loop is given (plan rev 3.2 §9.1). The loop never reaches
into storage, K or the curator itself; the app wires real implementations in, tests pass fakes.

  AssistantData      read-only views of the user's wiki, K and guidelines (context assembly)
  ProposalApplier    applies an accepted proposal (called by the decision endpoint, never by
                     the loop — a turn only ever *raises* proposals)"""

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from evowiki.agent.tools import ProposalDraft
from evowiki.goals.context import PageDigest
from evowiki.goals.language import Language


class PageSectionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    section_id: str
    heading: str
    body: str  # empty for structural checks/unwritten sections; id/heading still name the section


class PageView(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_id: str
    title: str
    concepts: tuple[str, ...]
    sections: tuple[PageSectionView, ...]
    checks: tuple[str, ...] = ()  # safe check_status_lines, including checks without SectionText


def page_version_of(data: object, user_id: str, page_id: str) -> str | None:
    """`AssistantData.page_version` is optional (a data port without it, e.g. a test fake, gives
    None = proposals carry no version and are never checked for staleness)."""
    fn = getattr(data, "page_version", None)
    return fn(user_id, page_id) if fn is not None else None


def section_version_of(data: object, user_id: str, page_id: str,
                       section_id: str | None) -> str | None:
    """`AssistantData.section_version` (optional like `page_version`): the fingerprint of the
    section a proposal changes, so it can still apply after other sections changed."""
    fn = getattr(data, "section_version", None)
    return fn(user_id, page_id, section_id) if fn is not None and section_id else None


class AssistantData(Protocol):
    # Optional (not declared here, see `page_version_of`):
    #   page_version(user_id, page_id) -> str | None   fingerprint of the page right now
    #   section_version(user_id, page_id, section_id) -> str | None   of one section
    #   open_diagnoses(user_id, draft_ids) -> tuple[str, ...]   those still open (turn_skills)
    def page(self, user_id: str, page_id: str) -> PageView | None: ...

    def pages(self, user_id: str) -> Sequence[PageDigest]: ...

    def k_text(self, user_id: str, concept_ids: Sequence[str]) -> str: ...

    def guidelines(self, user_id: str) -> Sequence[str]: ...

    def language(self, user_id: str) -> Language:
        """The user's content language (WikiState.language); the system prompt and the
        compaction summary are written in it."""
        ...

    def prereqs(self, user_id: str, concept_ids: Sequence[str]) -> tuple[str, ...]:
        """Prerequisites of `concept_ids` in this user's K (concept ids are per user)."""
        ...

    def named_nodes(self, user_id: str, text: str) -> tuple[str, ...]:
        """The graph's nodes whose name `text` says, each as one line (id, name, definition)."""
        ...


class ProposalApplier(Protocol):
    def apply(self, user_id: str, draft: ProposalDraft) -> tuple[str, ...]:
        """Write the accepted draft; returns the section ids written. Raises on failure."""
        ...
