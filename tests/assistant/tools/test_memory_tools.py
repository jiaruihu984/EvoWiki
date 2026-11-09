from datetime import UTC, datetime, timedelta

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.assistant.tools.memory import (
    NOT_FOUND,
    ReadPastTurnTool,
    SearchMemoryTool,
)
from evowiki.llm.chat import MEMORY_FENCE_HEAD
from evowiki.memory.contracts import EvidenceRef
from evowiki.memory.ports import MemoryPorts
from evowiki.memory.store import Episode, InMemoryMemoryStore, Item
from evowiki.retrieval.embed import HashingEmbedder

T = datetime(2026, 10, 27, 15, 30, tzinfo=UTC)       # 2026-10-27 23:30 in Shanghai


@pytest.fixture
def ports():
    sessions = MemorySessionStore()
    mem = InMemoryMemoryStore()
    for uid, sid in (("u1", "s1"), ("u2", "s9")):
        sessions.create(SessionInfo(session_id=sid, user_id=uid, page_id="p1",
                                    note_concept_id=None, created_at=T))
        sessions.append(sid, f"e1{sid}", "user", {"message_id": "m1",
                                                 "text": "我不想要那种讲法，太绕了", "selection": None}, T)
        sessions.append(sid, f"e2{sid}", "assistant", {"message_id": "a1", "text": "好的",
                                                      "sources": []}, T)
        mem.upsert_episode(Episode(user_id=uid, session_id=sid, message_id="m1",
                                   answer_message_id="a1", trace_run_id=f"assistant:{sid}:m1",
                                   turn_index=0, page_id="p1", at=T,
                                   user_text="我不想要那种讲法，太绕了", answer_text="好的"))
    for iid, kind, uid in (("d1", "decision", "u1"), ("p1", "preference", "u1"),
                           ("d9", "decision", "u2")):
        mem.put_item(Item(item_id=iid, user_id=uid, kind=kind, content="讲法要直接",
                          provenance="explicit_current",
                          evidence=(EvidenceRef(message_id="m1", session_id="s1", role="user",
                                                quote="不想要那种讲法"),),
                          scope_type="global", session_id="s1", valid_from=T, created_at=T,
                          status="active" if kind == "decision" else None))
    return MemoryPorts(store=mem, embedder=HashingEmbedder(), sessions=sessions,
                       clock=lambda: T + timedelta(days=1))


def ctx(words=()):
    return ToolContext(user_id="u1", session_id="s2", page_id="p1", user_words=words)


def test_search_is_fenced_carries_ids_and_sessions(ports):
    r = SearchMemoryTool(ports).run({"query": "那种讲法"}, ctx())
    assert r.error is None and r.content.startswith(MEMORY_FENCE_HEAD) and "<历史资料>" in r.content
    assert "出处=s1/m1" in r.content and "s9" not in r.content          # only his own rows
    assert {h["id"] for h in r.data["hits"]} >= {"ep:s1/m1", "item:d1"}
    assert r.data["sources_sessions"] == ["s1"]


def test_dates_are_his_zone(ports):
    tool = SearchMemoryTool(ports)
    # 2026-10-27 15:30Z is 10-27 23:30 in Shanghai: in "10-27", not in "10-28"
    assert tool.run({"query": "那种讲法", "since": "2026-10-27", "until": "2026-10-27"},
                    ctx()).data["hits"]
    assert tool.run({"query": "那种讲法", "since": "2026-10-28"}, ctx()).data["hits"] == []


def test_read_past_turn_reads_the_turn_whole_and_only_his(ports):
    tool = ReadPastTurnTool(ports)
    r = tool.run({"memory_id": "ep:s1/m1"}, ctx())
    assert "用户：我不想要那种讲法，太绕了" in r.content and "助手：好的" in r.content
    assert r.content.startswith(MEMORY_FENCE_HEAD) and r.data["sources_sessions"] == ["s1"]
    assert NOT_FOUND in tool.run({"memory_id": "ep:s9/m1"}, ctx()).content     # another user's
    assert NOT_FOUND in tool.run({"session_id": "s9", "message_id": "m1"}, ctx()).content
    assert NOT_FOUND in tool.run({"memory_id": "item:d9"}, ctx()).content
    item = tool.run({"memory_id": "item:d1"}, ctx())
    assert "讲法要直接" in item.content and "用户：我不想要那种讲法" in item.content


def test_a_forgotten_episode_or_item_is_neither_searched_nor_read(ports):
    from evowiki.memory.forget_fact import episode_key, fact_hits
    from evowiki.memory.store import Suppression

    hits = fact_hits(ports.store, "u1", ("讲法要直接",))
    assert hits and all(h.text for h in hits)
    ports.store._suppressions.update({
        h.key: ("u1", Suppression(item_id=h.key, kind=h.kind, evidence_message_ids=frozenset(h.message_ids)))
        for h in hits})
    found = SearchMemoryTool(ports).run({"query": "讲法要直接"}, ctx()).data["hits"]
    assert not {h["id"].removeprefix("item:") for h in found} & {h.key for h in hits}
    assert episode_key("x", "y").startswith("ep:")
