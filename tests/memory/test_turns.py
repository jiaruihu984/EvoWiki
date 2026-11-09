import pytest

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.memory.turns import read_turn, verify_quote
from tests.memory.conftest import T, seed_user_and_session


@pytest.fixture(params=["mem", "pg"])
def session_store_with_turns(request):
    if request.param == "mem":
        s = MemorySessionStore()
        s.create(SessionInfo(session_id="s1", user_id="u1", page_id="p1", note_concept_id=None,
                             created_at=T))
    else:
        pool = request.getfixturevalue("pool")
        seed_user_and_session(pool, "u1", "s1")
        s = PgSessionStore(pool)
    s.append("s1", "e1", "user", {"message_id": "m1", "text": "以后数学题都先解释符号",
                                  "selection": None}, T)
    s.append("s1", "e2", "assistant", {"message_id": "a1", "text": "好的好的", "sources": []}, T)
    s.append("s1", "e3", "user", {"message_id": "m2", "text": "还没答的", "selection": None}, T)
    return s


def test_quote_must_be_in_that_user_message_of_that_user(session_store_with_turns):
    s = session_store_with_turns
    assert verify_quote(s, "u1", "s1", "m1", "以后数学题都先解释符号")
    assert verify_quote(s, "u1", "s1", "m1", "以后 数学题")          # whitespace ignored
    assert not verify_quote(s, "u1", "s1", "m1", "以后")             # under 4 chars
    assert not verify_quote(s, "u1", "s1", "a1", "好的好的")          # an assistant message
    assert not verify_quote(s, "u2", "s1", "m1", "以后数学题都先解释符号")   # another user
    assert not verify_quote(s, "u1", "s1", "m9", "以后数学题都先解释符号")   # no such message
    assert not verify_quote(s, "u1", "s9", "m1", "以后数学题都先解释符号")   # no such session


def test_read_turn_returns_user_and_answer_whole_and_is_bound_to_the_user(session_store_with_turns):
    t = read_turn(session_store_with_turns, "u1", "s1", "m1")
    assert t.user_text == "以后数学题都先解释符号" and t.answer_text == "好的好的"
    assert t.page_id == "p1" and t.at == T
    assert read_turn(session_store_with_turns, "u2", "s1", "m1") is None
    assert read_turn(session_store_with_turns, "u1", "s1", "a1") is None     # not a user message
    assert read_turn(session_store_with_turns, "u1", "s1", "m2") is None     # no answer yet


def test_a_short_latin_name_is_a_quote():
    s = MemorySessionStore()
    s.create(SessionInfo(session_id="s", user_id="u", page_id=None, note_concept_id=None, created_at=T))
    s.append("s", "e", "user", {"message_id": "m", "text": "c++ 我什么都不会，别的再说"}, T)
    assert verify_quote(s, "u", "s", "m", "c++")
    assert not verify_quote(s, "u", "s", "m", "别的")
