from datetime import UTC, datetime, timedelta

import pytest

from evowiki.sources import store
from evowiki.sources.models import PageRead
from tests.runtime.helpers import make_user
from tests.sources.test_store import NOW, _src


@pytest.fixture
def db(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    with pool.connection() as conn:
        yield conn
        conn.rollback()


def _add(db, sid="s1", user="u1", **kw):
    store.insert_source(db, _src(sid, user, sha=sid, **kw), b"a", "x/y")


def test_put_page_reads_overwrites_same_page_and_reader(db):
    _add(db)
    store.put_page_reads(db, "u1", "s1", [PageRead(1, "旧", "local", False),
                                          PageRead(2, None, "local", True)], NOW)
    store.put_page_reads(db, "u1", "s1", [PageRead(1, "新", "local", True)], NOW)
    got = store.page_reads(db, "u1", "s1")
    assert got[1] == PageRead(1, "新", "local", True)
    assert got[2] == PageRead(2, None, "local", True)


def test_claude_read_beats_local_for_same_page(db):
    _add(db)
    store.put_page_reads(db, "u1", "s1", [PageRead(1, "本地", "local", True),
                                          PageRead(1, "云端", "claude", True),
                                          PageRead(2, "本地2", "local", False)], NOW)
    got = store.page_reads(db, "u1", "s1")
    assert got[1].by == "claude" and got[1].text == "云端"
    assert got[2].by == "local"


def test_page_reads_user_isolation_and_write_guard(db):
    _add(db)
    store.put_page_reads(db, "u1", "s1", [PageRead(1, "x", "local", False)], NOW)
    assert store.page_reads(db, "u2", "s1") == {}
    with pytest.raises(LookupError):
        store.put_page_reads(db, "u2", "s1", [PageRead(1, "x", "local", False)], NOW)


def test_soft_delete_clears_page_reads_and_refuses_writes(db):
    _add(db)
    store.put_page_reads(db, "u1", "s1", [PageRead(1, "x", "local", False)], NOW)
    assert store.soft_delete(db, "u1", "s1", NOW)
    assert store.page_reads(db, "u1", "s1") == {}
    with pytest.raises(LookupError):
        store.put_page_reads(db, "u1", "s1", [PageRead(1, "x", "local", False)], NOW)


def test_bump_run(db):
    _add(db)
    assert store.get_source(db, "u1", "s1").run_gen == 0
    assert store.bump_run(db, "u1", "s1") == 1
    assert store.bump_run(db, "u1", "s1") == 2
    assert store.get_source(db, "u1", "s1").run_gen == 2
    with pytest.raises(LookupError):
        store.bump_run(db, "u2", "s1")


def test_insert_keeps_new_fields(db):
    _add(db, run_gen=3)
    assert store.get_source(db, "u1", "s1").run_gen == 3


def test_stale_ask_only(db):
    old = NOW - timedelta(days=40)
    cutoff = NOW - timedelta(days=30)
    _add(db, "old_ask", created_at=old)
    _add(db, "old_learn", created_at=old)
    _add(db, "old_mixed", created_at=old)
    _add(db, "old_deleted", created_at=old)
    _add(db, "old_nouse", created_at=old)
    _add(db, "fresh_ask", created_at=NOW)
    _add(db, "other_user", user="u2", created_at=old)
    store.add_use(db, "u1", "old_ask", "ask", session_id="c1", now=old)
    store.add_use(db, "u1", "old_learn", "learn", now=old)
    store.add_use(db, "u1", "old_mixed", "ask", now=old)
    store.add_use(db, "u1", "old_mixed", "exam", now=old)
    store.add_use(db, "u1", "old_deleted", "ask", now=old)
    store.soft_delete(db, "u1", "old_deleted", NOW)
    store.add_use(db, "u1", "fresh_ask", "ask", now=NOW)
    store.add_use(db, "u2", "other_user", "ask", now=old)
    assert store.stale_ask_only(db, before=cutoff) == [
        ("u1", "old_ask"), ("u2", "other_user")] or \
        sorted(store.stale_ask_only(db, before=cutoff)) == [("u1", "old_ask"), ("u2", "other_user")]
    assert len(store.stale_ask_only(db, before=cutoff, limit=1)) == 1


def test_session_attachments(db):
    t = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    for sid in ("a", "b", "c", "d"):
        _add(db, sid)
    store.add_use(db, "u1", "b", "ask", session_id="c1", now=t)
    store.add_use(db, "u1", "a", "ask", session_id="c1", now=t + timedelta(minutes=1))
    store.add_use(db, "u1", "a", "ask", session_id="c1", now=t + timedelta(minutes=2))
    store.add_use(db, "u1", "c", "learn", session_id="c1", now=t)
    store.add_use(db, "u1", "d", "ask", session_id="c2", now=t)
    assert store.session_attachments(db, "u1", "c1") == ["b", "a"]
    assert store.session_attachments(db, "u2", "c1") == []
