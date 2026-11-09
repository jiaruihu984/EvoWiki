from datetime import timedelta

import psycopg
import pytest

from evowiki.store.locks import user_writer
from evowiki.store.wiki import (
    StoredWikiEvent,
    WikiEventConflict,
    append_wiki_events,
    last_wiki_seq,
    wiki_events,
)
from tests.runtime.helpers import T0, make_user


def _rows(prefix: str, n: int, start: int = 0):
    return [(f"{prefix}-{i}", T0 + timedelta(minutes=start + i), "page_saved",
             {"record": {"id": f"p{i}", "n": i, "zh": "页面"}}) for i in range(n)]


def test_batches_number_contiguously_and_read_back_in_order(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    with user_writer(pool, "u1") as tx:
        assert append_wiki_events(tx, _rows("a", 2)) == [1, 2]
    with user_writer(pool, "u1") as tx:
        assert append_wiki_events(tx, _rows("b", 3, start=2)) == [3, 4, 5]
    with user_writer(pool, "u2") as tx:
        assert append_wiki_events(tx, _rows("c", 1)) == [1]
        assert append_wiki_events(tx, []) == []
    with pool.connection() as conn:
        got = wiki_events(conn, "u1")
        assert [e.seq for e in got] == [1, 2, 3, 4, 5]
        assert [e.event_id for e in got] == ["a-0", "a-1", "b-0", "b-1", "b-2"]
        assert got[2] == StoredWikiEvent(3, "b-0", T0 + timedelta(minutes=2), "page_saved",
                                         {"record": {"id": "p0", "n": 0, "zh": "页面"}})
        assert got[0].occurred_at.utcoffset() == timedelta(0)
        assert [e.seq for e in wiki_events(conn, "u1", upto_seq=3)] == [1, 2, 3]
        assert wiki_events(conn, "u1", upto_seq=0) == []
        assert last_wiki_seq(conn, "u1") == 5 and last_wiki_seq(conn, "u2") == 1
        assert last_wiki_seq(conn, "nobody") == 0 and wiki_events(conn, "nobody") == []


def test_reused_event_id_conflicts(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    with user_writer(pool, "u1") as tx:
        append_wiki_events(tx, _rows("a", 1))
    with pytest.raises(WikiEventConflict), user_writer(pool, "u1") as tx:
        append_wiki_events(tx, _rows("a", 1))
    with pytest.raises(WikiEventConflict), user_writer(pool, "u2") as tx:
        append_wiki_events(tx, _rows("a", 1))
    with pytest.raises(WikiEventConflict), user_writer(pool, "u1") as tx:
        append_wiki_events(tx, [_rows("z", 1)[0], _rows("z", 1)[0]])
    with pool.connection() as conn:
        assert last_wiki_seq(conn, "u1") == 1 and last_wiki_seq(conn, "u2") == 0


def test_wiki_event_is_append_only(pool, admin_pool):
    make_user(pool, "u1")
    with user_writer(pool, "u1") as tx:
        append_wiki_events(tx, _rows("a", 1))
    # CASCADE passes foreign-key prechecks (rsi_feedback references this log), so this tests the append-only trigger.
    for sql, op in (("update wiki_event set type = 'x'", "UPDATE"),
                    ("delete from wiki_event", "DELETE"), ("truncate wiki_event cascade", "TRUNCATE")):
        with (pytest.raises(psycopg.errors.RaiseException,
                            match=f"append-only table wiki_event: {op}"),
              admin_pool.connection() as conn):
            conn.execute(sql)

    with pool.connection() as conn:
        assert [event.event_id for event in wiki_events(conn, "u1")] == ["a-0"]
