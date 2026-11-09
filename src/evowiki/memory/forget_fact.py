"""Forgetting one fact (「忘掉我家猫叫年糕」): what was said stays in his conversations, it is no longer
recalled, searched or consolidated. A session's delete (memory/forget.py) takes everything of one
session; this takes what names the words he gave, across sessions.

`memory_suppression` is the one record: an item goes in under its id (as a forgotten item does),
an episode under `ep:<session>/<message>` with kind "episode". `search.search` hides both,
`read_past_turn` refuses the episode, consolidation skips its message (any kind of candidate resting
on it is dropped). A prompt built from a session goes through `assistant.forgotten`: such a turn is
masked, and a compaction summary loses what names the words (the row's own words, read while it is
`executed`). Nothing is deleted, so undo restores it all, except that a compaction written while it
was in force was not fed the fact (its turns come back through search and read_past_turn).

The match is by the words themselves, run when the card is shown and again when he confirms:
the message asking to forget names the fact too, and it is an episode by then. Waiting cards
(`rule` / `profile_fact` rows) that name the words are rejected with it, and come back on undo."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from evowiki.bot.actions import ActionRefused, register_undo
from evowiki.store.bot import MEMORY_FORGET_KIND, ActionRow

KIND = MEMORY_FORGET_KIND
EPISODE = "episode"
SHOWN_CHARS = 60

register_undo(KIND, lambda row: True)


def episode_key(session_id: str, message_id: str) -> str:
    return f"ep:{session_id}/{message_id}"


@dataclass(frozen=True)
class FactHit:
    key: str
    kind: str
    message_ids: tuple[str, ...]
    text: str


def _named(text: str, words: Sequence[str]) -> bool:
    return any(w in text for w in words)


def _around(text: str, words: Sequence[str]) -> str:
    at = min((text.find(w) for w in words if w in text), default=0)
    start = max(0, at - SHOWN_CHARS // 2)
    return " ".join(text[start:start + SHOWN_CHARS].split())


def fact_hits(store: Any, user_id: str, words: Sequence[str]) -> list[FactHit]:
    """What names any of `words`: his items and episodes, newest episodes first."""
    out: list[FactHit] = []
    for it in store.items(user_id):
        if _named(it.content, words):
            ids = tuple(e.message_id for e in it.evidence if e.message_id)
            out.append(FactHit(it.item_id, it.kind, ids, it.content))
    for ep in reversed(store.episodes(user_id)):
        if _named(ep.user_text, words) or _named(ep.answer_text or "", words):
            said = ep.user_text if _named(ep.user_text, words) else ep.answer_text or ""
            out.append(FactHit(episode_key(ep.session_id, ep.message_id), EPISODE,
                               (ep.message_id,), _around(said, words)))
    return out


def waiting_cards(ledger: Any, user_id: str, words: Sequence[str], *, limit: int = 50) -> list[ActionRow]:
    """His cards waiting for a yes that name the words."""
    from evowiki.wikiapp.preferences import FACT_KIND, PENDING_KIND
    return [r for r in ledger.list(user_id, status="proposed", limit=limit)
            if r.kind in (PENDING_KIND, FACT_KIND) and r.content.get("confirm")
            and _named(f"{r.summary}\n{r.content.get('text') or ''}", words)]


class MemoryForgetExecutor:
    """The row's content is `{words, quote}`; its result records what it hid, for the undo."""

    def __init__(self, pool: Any, store: Any, ledger: Any, clock: Callable[[], datetime]):
        self._pool, self._store, self._ledger, self._clock = pool, store, ledger, clock

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        words = tuple(action.content.get("words") or ())
        if not words:
            raise ActionRefused("这条记录没有写要忘掉什么")
        hits, cards = fact_hits(self._store, user_id, words), waiting_cards(self._ledger, user_id, words)
        now = self._clock()
        with self._pool.connection() as conn, conn.transaction():
            from evowiki.memory.forget import suppress_sources

            conn.execute("select pg_advisory_xact_lock(hashtext('memory:user:' || %s))", (user_id,))
            suppress_sources(conn, user_id, action.action_id,
                             [h.key if h.kind == EPISODE else f"item:{h.key}" for h in hits])
            for h in hits:
                conn.execute("insert into memory_suppression (user_id, item_id, kind, "
                             "evidence_message_ids, scope_type, scope_id, at) "
                             "values (%s, %s, %s, %s, 'global', null, %s) "
                             "on conflict (item_id) do nothing",
                             (user_id, h.key, h.kind, list(h.message_ids), now))
            for c in cards:
                conn.execute("update agent_action set status = 'rejected', decided_at = %s "
                             "where action_id = %s and user_id = %s and status = 'proposed'",
                             (now, c.action_id, user_id))
            effect(conn)
        return {"keys": [h.key for h in hits], "cards": [c.action_id for c in cards]}

    def rejected(self, user_id: str, action: ActionRow) -> None:
        pass

    def undo(self, user_id: str, action: ActionRow) -> dict:
        res = action.result or {}
        with self._pool.connection() as conn, conn.transaction():
            from evowiki.memory.forget import _invalidate_derived
            from evowiki.store.db import fetch_all

            conn.execute("select pg_advisory_xact_lock(hashtext('memory:user:' || %s))", (user_id,))
            nodes = [r["node"] for r in fetch_all(conn,
                "delete from memory_suppression_closure where user_id=%s and owner=%s returning node",
                (user_id, action.action_id))]
            _invalidate_derived(conn, user_id, nodes)
            conn.execute("delete from memory_suppression where user_id = %s and item_id = any(%s)",
                         (user_id, list(res.get("keys") or ())))
            conn.execute("update agent_action set status = 'proposed', decided_at = null "
                         "where user_id = %s and action_id = any(%s) and status = 'rejected'",
                         (user_id, list(res.get("cards") or ())))
        return {"restored": len(res.get("keys") or ())}
