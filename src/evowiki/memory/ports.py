"""What the assistant turn holds of the memory layer (`TurnDeps.memory`). Each call here fails
soft for the turn (a warning; the answer never fails because of memory) and is one recorded
step inside a durable workflow (`durable()`)."""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from evowiki.agent import durable
from evowiki.assistant.session import SessionInfo, SessionState, SessionStore
from evowiki.llm.chat import ChatMessage
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import LLMCall
from evowiki.llm.usage import Account
from evowiki.memory import index
from evowiki.memory.lander import NullPreferenceLander, NullRuleGuard, PreferenceLander, RuleGuard
from evowiki.memory.store import MemoryStore
from evowiki.retrieval.embed import Embedder

if TYPE_CHECKING:
    from evowiki.memory.recall import Recall
    from evowiki.memory.search import AuthorityOf, UndoneOf

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryPorts:
    store: MemoryStore
    embedder: Embedder | None
    sessions: SessionStore
    # user id -> the Bot settings' zone (dates the learner gives / sees)
    timezone: Callable[[str], str] = field(default=lambda uid: "Asia/Shanghai")
    # (user id, concept ids) -> those K holds as KNOWN (read only); None: none known
    known_concepts: Callable[[str, Sequence[str]], frozenset[str]] | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # consolidation: S3's land_preference (S3PreferenceLander; Null: every preference waits)
    # and the rule contract's guard (AutoRuleGuard; Null: nothing blocked)
    lander: PreferenceLander = field(default_factory=NullPreferenceLander)
    guard: RuleGuard = field(default_factory=NullRuleGuard)
    # history search: user id -> (preference / fact item -> its authority status); None: such
    # hits are labelled unchecked
    authority: "AuthorityOf | None" = None
    # what of a turn's actions he undid (memory/search.py); None: nothing to ask
    undone: "UndoneOf | None" = None
    new_id: Callable[[str], str] = field(default=lambda prefix: f"{prefix}_{uuid4().hex}")
    # (user id, item id) -> "committed" | "failed" | "not_found" | "elsewhere": forgetting one
    # item through a forget request (memory/forget.py); None: the store's own forget_item
    forget_item: Callable[[str, str], str] | None = None
    # after a committed consolidation: his library read for conflicts (memory/conflicts.py
    # ConflictService); None: not checked
    conflicts: Any = None
    steps: bool = False               # True: each call is a durable step (durable())
    anchors: Any = None
    background_allowed: Callable[[str], bool] = field(default=lambda uid: True)

    def durable(self) -> "MemoryPorts":
        return replace(self, steps=True)

    def _step(self, name: str, fn):
        return durable.step(name, fn) if self.steps else fn()

    def index_turn(self, *, info: SessionInfo, turn_index: int, user_message_id: str,
                   answer_message_id: str, user_text: str, answer_text: str,
                   concept_ids: Sequence[str], at: datetime) -> None:
        def run() -> None:
            index.index_turn(self.store, self.embedder, info=info, turn_index=turn_index,
                             user_message_id=user_message_id,
                             answer_message_id=answer_message_id, user_text=user_text,
                             answer_text=answer_text, concept_ids=concept_ids, at=at)
        try:
            self._step("assistant.memory.index", run)
        except Exception:
            log.warning("memory index failed for %s/%s", info.session_id, user_message_id,
                        exc_info=True)

    def rated(self, info: SessionInfo, entries: Sequence[Any], turn_id: str) -> None:
        """A rating of turn `turn_id` was stored: its episode carries it now
        (`index.reindex_feedback`). A warning on failure: the rating itself stands."""
        try:
            index.reindex_feedback(self.store, self.embedder, entries, user_id=info.user_id,
                                   session_id=info.session_id, turn_id=turn_id)
        except Exception:
            log.warning("memory feedback index failed for %s/%s", info.session_id, turn_id,
                        exc_info=True)

    def recall(self, *, info: SessionInfo, query: str, concept_ids: Sequence[str],
               state: SessionState, now: datetime,
               page_titles: Mapping[str, str] = {},
               live_from: int | None = None,
               excluded_sources: frozenset[str] = frozenset()) -> "Recall":
        """The turn's recall message; NO_RECALL (warning) on any failure: the turn answers
        without it. One recorded step: a replay uses what the first run recalled."""
        from evowiki.memory.recall import NO_RECALL, Recall, recall_block

        def run() -> dict:
            known = (self.known_concepts(info.user_id, concept_ids)
                     if self.known_concepts is not None and concept_ids else frozenset())
            r = recall_block(self, info=info, query=query, page_id=info.page_id,
                             concept_ids=concept_ids, state=state, known_concepts=known,
                             now=now, page_titles=page_titles, live_from=live_from,
                             excluded_sources=excluded_sources)
            return {"message": None if r.message is None else r.message.model_dump(mode="json"),
                    "recalled": list(r.recalled)}
        try:
            raw = self._step("assistant.memory.recall", run)
        except Exception:
            log.warning("memory recall failed for %s", info.session_id, exc_info=True)
            return NO_RECALL
        msg = raw["message"]
        return Recall(None if msg is None else ChatMessage.model_validate(msg),
                      tuple(raw["recalled"]))

    def consolidate(self, state: SessionState, *, gateway: Gateway, account: Account | None,
                    first: int, through: int, language: str = "zh") -> list[LLMCall]:
        """Consolidate `state`'s session up to turn `through` (memory/consolidate.py); one
        recorded step. Returns the model calls made. Failures propagate: the caller is the
        session's background wrap-up, which records and retries them (assistant/wrapup.py)."""
        from evowiki.memory.consolidate import consolidate

        def run() -> dict:
            r = consolidate(state, sessions=self.sessions, store=self.store, gateway=gateway,
                            account=account, lander=self.lander, guard=self.guard, first=first,
                            through=through, now=self.clock(), new_id=self.new_id,
                            language=language, embedder=self.embedder,  # type: ignore[arg-type]
                            undone=self.undone)
            return None if r.call is None else r.call.model_dump(mode="json")
        raw = self._step("assistant.memory.consolidate", run)
        return [] if raw is None else [LLMCall.model_validate(raw)]

    def check_conflicts(self, user_id: str, gateway: Gateway, account: Account | None) -> None:
        """His memory library read for conflicts unless it is the one read last time (no model
        call then). Failures propagate, as `consolidate`'s."""
        if self.conflicts is not None:
            self.conflicts.check(user_id, gateway, account)

    def retry_pending(self, user_id: str) -> int:
        """`retry_pending_landings` for this user (one step); 0 and a warning on failure. With
        the Null lander nothing can land, so nothing is read."""
        from evowiki.memory.consolidate import retry_pending_landings

        if isinstance(self.lander, NullPreferenceLander):
            return 0
        try:
            return int(self._step("assistant.memory.retry", lambda: retry_pending_landings(
                self.store, self.sessions, self.lander, user_id, self.clock(), self.new_id,
                guard=self.guard)))
        except Exception:
            log.warning("retrying pending preferences failed for %s", user_id, exc_info=True)
            return 0
