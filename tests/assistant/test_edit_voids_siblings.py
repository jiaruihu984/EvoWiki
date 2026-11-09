"""确认 one draft of a section: the session's other drafts of that section stop waiting."""

from datetime import UTC, datetime
from types import SimpleNamespace

from evowiki.assistant.edit_executor import AssistantEditExecutor, assistant_key
from evowiki.assistant.session import MemorySessionStore, SessionInfo

T = datetime(2026, 11, 4, tzinfo=UTC)


def _draft(pid, section="s1", page="p1", kind="rewrite_section"):
    return {"proposal_id": pid, "message_id": "a1", "draft": {"kind": kind, "page_id": page, "section_id": section}}


def test_only_the_same_sections_other_drafts():
    store = MemorySessionStore()
    store.create(SessionInfo(session_id="s", user_id="u", page_id="p1", note_concept_id=None, created_at=T))
    store.append("s", "e0", "user", {"message_id": "m1", "text": "改短"}, T)
    for i, d in enumerate((_draft("a"), _draft("b"), _draft("c", section="s2"), _draft("d", page="p2"),
                           _draft("e", kind="add_section"))):
        store.append("s", f"e{i + 1}", "proposal", d, T)
    ex = AssistantEditExecutor(None, store, lambda: T, lambda p: p)
    row = SimpleNamespace(content={"proposal_id": "a", "session_id": "s", "kind": "rewrite_section",
                                   "page_id": "p1", "section_id": "s1"})
    assert ex.voided_by("u", row) == [assistant_key("b")]
    added = SimpleNamespace(content={**row.content, "kind": "add_section"})
    assert ex.voided_by("u", added) == []


def test_a_sibling_with_no_row_is_closed_in_the_session():
    store = MemorySessionStore()
    store.create(SessionInfo(session_id="s", user_id="u", page_id="p1", note_concept_id=None, created_at=T))
    store.append("s", "e0", "user", {"message_id": "m1", "text": "改短"}, T)
    store.append("s", "e1", "proposal", _draft("b"), T)
    ex = AssistantEditExecutor(None, store, lambda: T, lambda p: f"{p}9")
    ex.void_without_row(SimpleNamespace(content={"session_id": "s"}), assistant_key("b"))
    [d] = [e.payload for e in store.entries("s") if e.type == "decision"]
    assert d["proposal_id"] == "b" and d["status"] == "failed"
