from datetime import UTC, datetime

import pytest

from evowiki.assistant.session import SessionInfo
from evowiki.memory.index import TEXT_MAX, backfill_vectors, index_turn
from evowiki.memory.store import InMemoryMemoryStore, tag
from evowiki.retrieval.embed import HashingEmbedder

T = datetime(2026, 10, 27, 9, tzinfo=UTC)


class Broken(HashingEmbedder):
    def embed_documents(self, texts):
        raise RuntimeError("embedder down")


@pytest.fixture
def info():
    return SessionInfo(session_id="s1", user_id="u1", page_id="p1", note_concept_id=None,
                       created_at=T)


def test_index_is_idempotent_and_clips_per_role(info):
    s = InMemoryMemoryStore()
    for _ in range(2):
        index_turn(s, HashingEmbedder(), info=info, turn_index=0, user_message_id="m1",
                   answer_message_id="a1", user_text="问" * 2000, answer_text="答",
                   concept_ids=("c1",), at=T)
    [e] = s.episodes("u1")
    assert len(e.user_text) == TEXT_MAX and e.trace_run_id == "assistant:s1:m1"
    assert e.page_id == "p1" and e.concept_ids == ("c1",)


def test_vector_failure_keeps_text_and_backfill_fills_it(info):
    s = InMemoryMemoryStore()
    index_turn(s, Broken(), info=info, turn_index=0, user_message_id="m1",
               answer_message_id="a1", user_text="矩阵", answer_text="…", concept_ids=(), at=T)
    assert s.episodes("u1") and not s.episode_vectors("u1", tag(HashingEmbedder()))
    assert backfill_vectors(s, HashingEmbedder(), "u1") == 1
    assert s.episode_vectors("u1", tag(HashingEmbedder()))
    assert backfill_vectors(s, HashingEmbedder(), "u1") == 0
