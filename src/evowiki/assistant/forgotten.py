"""What a confirmed 「忘掉…」 (`memory.forget_fact`) takes out of what a model reads of a session.

The log is never touched, so undoing the row brings everything back. `view` is applied where a prompt
is built from a folded session: the turn's history and summary, the next session's carry-over, a later
compaction. A turn he asked to forget (a suppressed episode) keeps its place with its words replaced,
so indices and first_kept hold. A summary loses the sentences and items naming the words; the summary
itself is rewritten only by a later compaction, which is fed this view.

A turn after the confirm whose message names the words again is new: he said it, it stays. An answer
naming them to a message that does not (「好」 → 「已忘掉年糕」) loses those sentences."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from evowiki.assistant.session import SessionState, Turn
from evowiki.assistant.summary import Evidence, Recap, render_handoff
from evowiki.store.bot import MEMORY_FORGET_KIND

MASKED = "（这部分内容他要求忘掉，这里不再提供）"
# with nothing else to go on the model says 「你没提过」: the turn's system prompt says why it cannot see it
FORGOT_LINE = ("他确认过要忘掉一些内容，那些你已经看不到了。问到你找不到的事，不要说他没提过，可以说可能是他要求忘掉的；"
               "忘掉不是删除，原文还留在他的历史对话里。")
FORGOTTEN_TITLE = "忘掉他要求忘掉的内容"
_SENTENCE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;\n]*|[。！？!?；;\n]+")


def names(text: str, words: Sequence[str]) -> bool:
    return any(w in text for w in words)


def scrub(text: str, words: Sequence[str]) -> str:
    """`text` without the sentences naming `words`."""
    if not names(text, words):
        return text
    return "".join(s for s in _SENTENCE.findall(text) if not names(s, words)).strip()


@dataclass(frozen=True)
class Forgotten:
    words: tuple[str, ...] = ()
    messages: frozenset[str] = frozenset()     # the user messages of the suppressed episodes

    @property
    def any(self) -> bool:
        return bool(self.words or self.messages)

    def answer(self, user_text: str, answer: str) -> str:
        """An answer as it may be read later; he said the words himself: as it is."""
        if not self.words or names(user_text, self.words):
            return answer
        return scrub(answer, self.words) or MASKED

    def _items(self, items: Iterable[str]) -> list[str]:
        return [i for i in items if not names(i, self.words)]

    def _evidence(self, items: Iterable[Evidence]) -> list[Evidence]:
        return [e for e in items if not names(e.concept, self.words)]

    def recap(self, recap: Recap) -> Recap:
        h = recap.handoff.model_copy(update={
            "summary": scrub(recap.handoff.summary, self.words),
            "decisions": self._items(recap.handoff.decisions),
            "active_requests": self._items(recap.handoff.active_requests)})
        ln = recap.learning.model_copy(update={
            "unclear": self._evidence(recap.learning.unclear),
            "understood": self._evidence(recap.learning.understood)})
        return recap.model_copy(update={"handoff": h, "learning": ln, "edits": tuple(self._items(recap.edits)),
                                        "legacy_text": scrub(recap.legacy_text, self.words)})

    def _turn(self, t: Turn) -> Turn:
        if t.message_id in self.messages:
            return t.model_copy(update={"user_text": MASKED, "selection": None, "edits": (), "asks": (),
                                        "answer": MASKED if t.answer is not None else None,
                                        "paused": None, "pending_ops": ()})
        if not self.words or names(t.user_text, self.words) or t.answer is None:
            return t
        return t.model_copy(update={"answer": self.answer(t.user_text, t.answer), "asks": tuple(self._items(t.asks)),
                                    "edits": tuple(self._items(t.edits))})

    def view(self, state: SessionState) -> SessionState:
        if not self.any:
            return state
        recap = self.recap(state.recap) if state.recap is not None and self.words else state.recap
        return state.model_copy(update={"turns": tuple(self._turn(t) for t in state.turns), "recap": recap,
                                        "summary": render_handoff(recap)})


NOTHING = Forgotten()


def forgotten_of(store: Any, user_id: str) -> Forgotten:
    """What is forgotten now: `store` is his `MemoryStore`."""
    return Forgotten(words=tuple(store.forgotten_words(user_id)), messages=frozenset(
        m for s in store.suppressions(user_id) if s.kind == "episode" for m in s.evidence_message_ids))


def prompt_title(row: Any) -> str:
    """A ledger row's title as a model may read it: a confirmed forget's title names what was forgotten."""
    return FORGOTTEN_TITLE if row.kind == MEMORY_FORGET_KIND and row.status == "executed" else row.summary
