"""Per-turn index: one answered turn -> one `memory_episode` row (idempotent on the session and
the user message), plus its vector when the embedder works. A vector failure keeps the text row
(BM25 still finds it) and `backfill_vectors` fills it in later.

The learner's 👍 / 👎 on an answer (a `rating` entry) joins its episode: `reindex_feedback`
rebuilds the episode's answer text from the log plus one line `以前的反馈：…` (the latest rating
of that answer; a cleared rating removes the line), and embeds it again. It is never an item,
a profile fact or K, and consolidation never takes it as evidence (it reads the user / assistant
entries only)."""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from evowiki.assistant.session import Entry, SessionInfo
from evowiki.memory.store import Episode, MemoryStore, tag
from evowiki.retrieval.embed import Embedder

TEXT_MAX = 800            # characters kept per role

log = logging.getLogger(__name__)


def embed_text(user_text: str, answer_text: str) -> str:
    return f"用户：{user_text}\n助手：{answer_text}"


def index_turn(store: MemoryStore, embedder: Embedder | None, *, info: SessionInfo,
               turn_index: int, user_message_id: str, answer_message_id: str, user_text: str,
               answer_text: str, concept_ids: Sequence[str], at: datetime) -> None:
    """Store the turn (text clipped to TEXT_MAX per role). The store write raises; the vector
    never does (a warning, the row stays without one)."""
    ep = Episode(user_id=info.user_id, session_id=info.session_id, message_id=user_message_id,
                 answer_message_id=answer_message_id,
                 trace_run_id=f"assistant:{info.session_id}:{user_message_id}",
                 turn_index=turn_index, page_id=info.page_id, concept_ids=tuple(concept_ids),
                 at=at, user_text=user_text[:TEXT_MAX], answer_text=answer_text[:TEXT_MAX])
    store.upsert_episode(ep)
    if embedder is None:
        return
    try:
        [vec] = embedder.embed_documents([embed_text(ep.user_text, ep.answer_text)])
        store.set_episode_vector(ep.session_id, ep.message_id, vec, embedder)
    except Exception:
        log.warning("memory vector failed for %s/%s", ep.session_id, ep.message_id,
                    exc_info=True)


FEEDBACK_HEAD = "以前的反馈："
CORRECTED_ANSWER = "（这次回答他纠正过，不作数）"


def _store_answer(store: MemoryStore, embedder: Embedder | None, ep: Episode, text: str) -> None:
    """The episode with `text` as its answer, embedded again; a failed embedding drops the stale
    vector for the backfill."""
    new = ep.model_copy(update={"answer_text": text})
    store.upsert_episode(new)
    if embedder is None:
        return
    try:
        [vec] = embedder.embed_documents([embed_text(new.user_text, new.answer_text)])
        store.set_episode_vector(ep.session_id, ep.message_id, vec, embedder)
    except Exception:
        log.warning("memory vector failed for %s/%s; left for the backfill", ep.session_id,
                    ep.message_id, exc_info=True)
        store.clear_episode_vector(ep.session_id, ep.message_id)


def mark_answer_corrected(store: MemoryStore, embedder: Embedder | None, ep: Episode) -> None:
    """His correction of this answer: recall no longer repeats what it said (the session log, his
    own record, keeps it). His words of that turn stay searchable."""
    _store_answer(store, embedder, ep, CORRECTED_ANSWER)


def feedback_line(rating: Mapping[str, Any]) -> str | None:
    """`以前的反馈：👎（问题「…」） 问过了；备注：…` for a rating entry's payload; None for a
    cleared one."""
    value = rating.get("value")
    if value not in ("up", "down"):
        return None
    line = FEEDBACK_HEAD + ("👍" if value == "up" else "👎")
    asks = [str(a.get("question") or "") for a in rating.get("asks") or ()]
    if any(asks):
        line += "（问题" + "".join(f"「{q}」" for q in asks if q) + "）"
    if chips := "、".join(rating.get("chips") or ()):
        line += f" {chips}"
    if note := str(rating.get("note") or "").strip():
        line += f"；备注：{note}"
    return line


def _turn_view(entries: Sequence[Entry], turn_id: str) -> tuple[str | None, dict | None]:
    """The turn's (latest) answer text and the latest rating of its answer, from the log."""
    answer: str | None = None
    answer_ids: set[str] = set()
    current: str | None = None
    rating: dict | None = None
    for e in sorted(entries, key=lambda x: x.seq):
        if e.type == "user":
            current = str(e.payload.get("message_id"))
        elif e.type == "assistant" and current == turn_id:
            answer = str(e.payload.get("text") or "")
            answer_ids.add(str(e.payload.get("message_id")))
        elif e.type == "rating" and (e.payload.get("turn_id") == turn_id
                                     or e.payload.get("message_id") in answer_ids):
            rating = e.payload
    return answer, rating


def reindex_feedback(store: MemoryStore, embedder: Embedder | None, entries: Sequence[Entry],
                     *, user_id: str, session_id: str, turn_id: str) -> bool:
    """Rebuild turn `turn_id`'s episode with its latest rating (module doc); idempotent. False
    when the turn has no episode (never indexed, or hidden by a forget) or nothing changed. A
    failed embedding drops the stale vector for the backfill."""
    ep = next((e for e in store.episodes(user_id)
               if e.session_id == session_id and e.message_id == turn_id), None)
    answer, rating = _turn_view(entries, turn_id)
    if ep is None or answer is None or ep.answer_text.startswith(CORRECTED_ANSWER):
        return False
    line = feedback_line(rating) if rating is not None else None
    text = answer[:TEXT_MAX] + (f"\n{line}" if line else "")
    if text == ep.answer_text:
        return False
    _store_answer(store, embedder, ep, text)
    return True


BACKFILL_BATCH = 32


def backfill_vectors(store: MemoryStore, embedder: Embedder, user_id: str,
                     limit: int = 200) -> int:
    """Embed up to `limit` of this user's episodes that have no vector of `embedder`'s tag, in
    batches; returns how many got one. An embedder failure raises (the caller is a maintenance
    job and reports it)."""
    todo = store.episodes_without_vector(user_id, tag(embedder), limit)
    done = 0
    for i in range(0, len(todo), BACKFILL_BATCH):
        batch = todo[i:i + BACKFILL_BATCH]
        vecs = embedder.embed_documents([embed_text(e.user_text, e.answer_text) for e in batch])
        for e, v in zip(batch, vecs, strict=True):
            store.set_episode_vector(e.session_id, e.message_id, v, embedder)
            done += 1
    return done
