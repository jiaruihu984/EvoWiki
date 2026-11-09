from datetime import timedelta

import psycopg
import pytest

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import EntryConflict, MemorySessionStore, SessionInfo, fold
from evowiki.content.models import Lang
from evowiki.store.users import UserRow, create_user
from tests.assistant.fakes import DRAFT, INFO, T0, run


@pytest.fixture
def pg(pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2",
                                  lang=Lang.ZH, created_at=T0))
    return PgSessionStore(pool)


def test_replay_equals_memory_run(pg):
    run(pg)
    assert pg.info("s1") == INFO
    assert pg.entries("s1") == run(MemorySessionStore()).entries("s1")
    assert fold(INFO, pg.entries("s1")) == fold(INFO, run(MemorySessionStore()).entries("s1"))
    info, entry = pg.find_proposal("pr1")
    assert info == INFO and entry.seq == 3
    with pytest.raises(LookupError):
        pg.find_proposal("nope")
    with pytest.raises(LookupError):
        pg.info("nope")


def test_entries_are_append_only(pg, pool):
    run(pg)
    with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute("update assistant_entry set payload = '{}'")
    with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute("delete from assistant_session")


def test_proposal_id_is_unique(pg):
    run(pg)
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.append("s1", "e9", "proposal", {"proposal_id": "pr1", "message_id": "m2",
                                           "draft": DRAFT}, T0)
    assert len(pg.entries("s1")) == 8


def test_user_sessions_only_owner_with_a_user_entry(pg):
    run(pg)
    pg.create(INFO.model_copy(update={"session_id": "s2"}))      # empty -> not listed
    assert [i.session_id for i in pg.user_sessions("u1")] == ["s1"]
    assert pg.user_sessions("u2") == []


def test_append_is_idempotent_on_the_entry_id(pg):
    pg.create(INFO)
    payload = {"message_id": "m1", "text": "问", "selection": None}
    first = pg.append("s1", "e1", "user", payload, T0)
    again = pg.append("s1", "e1", "user", dict(payload), T0)
    assert again == first and len(pg.entries("s1")) == 1
    nxt = pg.append("s1", "e2", "user", {**payload, "message_id": "m2"}, T0)
    assert nxt.seq == 2                       # a retry did not burn a seq
    with pytest.raises(EntryConflict):
        pg.append("s1", "e1", "user", {**payload, "text": "别的"}, T0)
    with pytest.raises(EntryConflict):
        pg.append("s1", "e1", "assistant", payload, T0)
    assert len(pg.entries("s1")) == 2


def test_recent_sessions_newest_first_by_page_and_exclude(pg, pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u2", background="none", condition="S2",
                                  lang=Lang.ZH, created_at=T0))
    for i, page in enumerate(["p1", "p2", "p1"]):
        pg.create(SessionInfo(session_id=f"s{i}", user_id="u1", page_id=page,
                              note_concept_id=None, created_at=T0 + timedelta(minutes=i)))
    pg.create(SessionInfo(session_id="x", user_id="u2", page_id="p1", note_concept_id=None,
                          created_at=T0))
    ids = lambda xs: [x.session_id for x in xs]
    assert ids(pg.recent_sessions("u1", limit=5)) == ["s2", "s1", "s0"]
    assert ids(pg.recent_sessions("u1", limit=5, page_id="p1", exclude="s2")) == ["s0"]
    assert ids(pg.recent_sessions("u1", limit=1)) == ["s2"]


def test_rating_entries_are_stored_and_folded(pg):
    """Migration 0072: the entry type check takes `rating`; the latest one shows on the answer."""
    run(pg)
    pay = {"message_id": "a1", "turn_id": "m1", "chips": ["不对"], "note": "错了"}
    pg.append("s1", "r1", "rating", {**pay, "value": "up", "chips": []}, T0 + timedelta(seconds=9))
    pg.append("s1", "r2", "rating", {**pay, "value": "down"}, T0 + timedelta(seconds=10))
    msgs = fold(INFO, pg.entries("s1")).messages
    answer = next(m for m in msgs if m.message_id == "a1")
    assert answer.rating is not None and (answer.rating.value, answer.rating.chips) == (
        "down", ("不对",))
