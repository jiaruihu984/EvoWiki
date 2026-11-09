from dataclasses import replace
from datetime import UTC, datetime

import pytest

from evowiki.sources import exam_store as es
from evowiki.sources import store
from evowiki.sources.models import ExamItem
from tests.runtime.helpers import make_user
from tests.sources.test_store import _src

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def _item(iid, ord_=0, source="s1", **kw) -> ExamItem:
    base = {"item_id": iid, "source_id": source, "section_id": None, "page": 3, "ord": ord_,
            "number": str(ord_ + 1), "parent_id": None, "kind": "single", "stem": "题干",
            "options": ("A. x", "B. y"), "answer": "B", "explanation": None, "has_key": True,
            "status": "ok", "problem": None, "node_id": "n1", "attach": "attached"}
    base.update(kw)
    return ExamItem(**base)


@pytest.fixture
def db(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    with pool.connection() as conn:
        store.insert_source(conn, _src("s1", "u1", sha="a"), b"x", "application/pdf")
        store.add_use(conn, "u1", "s1", "exam", now=NOW)
        yield conn
        conn.rollback()


def test_replace_twice_keeps_second_and_roundtrips(db):
    es.replace_items(db, "u1", "s1", [_item("i1"), _item("i2", 1)], gen=1, now=NOW)
    es.replace_items(db, "u1", "s1", [_item("i3", 0, options=("A", "B"))], gen=2, now=NOW)
    got = es.items_of_source(db, "u1", "s1")
    assert got == [_item("i3", 0, options=("A", "B"))]
    assert es.exam_gen(db, "u1", "s1") == 2


def test_other_user_cannot_read_or_write(db):
    es.replace_items(db, "u1", "s1", [_item("i1")], gen=1, now=NOW)
    assert es.items_of_source(db, "u2", "s1") == []
    assert es.items_for_nodes(db, "u2", ["n1"]) == []
    assert es.items_by_id(db, "u2", ["i1"]) == []
    assert es.exam_counts(db, "u2") == {}
    assert es.exam_gen(db, "u2", "s1") is None
    with pytest.raises(LookupError):
        es.replace_items(db, "u2", "s1", [], gen=1, now=NOW)


def test_deleted_source_hidden_and_write_refused(db):
    es.replace_items(db, "u1", "s1", [_item("i1")], gen=1, now=NOW)
    store.soft_delete(db, "u1", "s1", NOW)
    assert es.items_for_nodes(db, "u1", ["n1"]) == []
    assert es.items_of_source(db, "u1", "s1") == []
    assert es.exam_counts(db, "u1") == {}
    with pytest.raises(LookupError):
        es.replace_items(db, "u1", "s1", [], gen=2, now=NOW)


def test_group_not_counted_and_parent_insert_and_filters(db):
    items = [_item("g", 0, kind="group", options=(), answer=None, has_key=False),
             _item("c1", 1, parent_id="g"), _item("c2", 2, parent_id="g"),
             _item("bad", 3, status="incomplete"),
             _item("pend", 4, attach="pending"),
             _item("other", 5, node_id="n2")]
    es.replace_items(db, "u1", "s1", items, gen=1, now=NOW)
    assert es.exam_counts(db, "u1") == {"n1": 2, "n2": 1}
    assert [i.item_id for i in es.items_for_nodes(db, "u1", ["n1"])] == ["g", "c1", "c2"]
    assert [i.item_id for i in es.items_by_id(db, "u1", ["bad", "c2"])] == ["c2", "bad"]
    assert es.items_for_nodes(db, "u1", []) == []


def test_items_for_nodes_orders_by_source_created_then_ord(db):
    store.insert_source(db, _src("s0", "u1", sha="b", created_at=datetime(2026, 1, 1, tzinfo=UTC)),
                        b"x", "application/pdf")
    store.add_use(db, "u1", "s0", "exam", now=NOW)
    es.replace_items(db, "u1", "s1", [_item("a", 0)], gen=1, now=NOW)
    es.replace_items(db, "u1", "s0", [_item("b", 1, source="s0"), _item("c", 0, source="s0")],
                     gen=1, now=NOW)
    assert [i.item_id for i in es.items_for_nodes(db, "u1", ["n1"])] == ["c", "b", "a"]


def test_groups_are_inserted_before_their_children_whatever_the_order(db):
    items = [_item("c1", 0, parent_id="g"), _item("g", 1, kind="group", options=(), answer=None,
                                                  has_key=False)]
    es.replace_items(db, "u1", "s1", items, gen=1, now=NOW)
    got = es.items_of_source(db, "u1", "s1")
    assert [i.item_id for i in got] == ["c1", "g"] and got[0].parent_id == "g"


def test_practice_reads_need_an_exam_use(db):
    """Undoing 「这是往年题」 (the exam use) takes the questions off every practice read; the
    stored items stay (the card and a later use reuse them)."""
    es.replace_items(db, "u1", "s1", [_item("i1")], gen=1, now=NOW)
    (use,) = [u for u in store.uses_of(db, "u1", "s1") if u["use"] == "exam"]
    store.delete_use(db, "u1", use["use_id"])
    assert es.items_for_nodes(db, "u1", ["n1"]) == []
    assert es.items_by_id(db, "u1", ["i1"]) == []
    assert es.exam_counts(db, "u1") == {}
    assert [i.item_id for i in es.items_of_source(db, "u1", "s1")] == ["i1"]
    store.add_use(db, "u1", "s1", "learn", now=NOW)
    assert es.exam_counts(db, "u1") == {}


def test_wrong_source_item_refused(db):
    with pytest.raises(ValueError):
        es.replace_items(db, "u1", "s1", [replace(_item("x"), source_id="zz")], gen=1, now=NOW)


def test_frequent_nodes():
    assert es.frequent_nodes({"a": 3, "b": 2}) == frozenset({"a"})


def test_an_items_locator_roundtrips(db):
    loc = {"source_id": "s1", "page": 3, "bbox": [0.1, 0.2, 0.9, 0.3], "block_id": "p3-b1",
           "char_start": 2, "char_end": 9}
    es.replace_items(db, "u1", "s1", [_item("i1", locator=loc), _item("i2", 1)], gen=1, now=NOW)
    a, b = es.items_of_source(db, "u1", "s1")
    assert a.locator == loc and b.locator is None
