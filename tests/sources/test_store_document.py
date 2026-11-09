"""`source_document` (0102): one layout document per source, isolated by user."""

from datetime import UTC, datetime

import pytest

from evowiki.sources import store
from evowiki.sources.document import Block, DocPage, Heading, SourceDocument
from tests.runtime.helpers import make_user
from tests.sources.test_importer import add_source

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


def doc(backend="mineru") -> SourceDocument:
    return SourceDocument(
        kind="pdf", backend=backend, version="1.0",
        pages=(DocPage(1, 612.0, 792.0), DocPage(2)),
        blocks=(Block("p1-b0", 1, "heading", "1. Intro", (0.1, 0.1, 0.5, 0.15), level=1),
                Block("p1-b1", 1, "formula", "x", None, latex="x^2"),
                Block("p2-b0", 2, "table", "a | b", html="<table></table>")),
        headings=(Heading("h1", "1. Intro", 1, 1, None, (0.1, 0.1, 0.5, 0.15), "layout"),))


@pytest.fixture
def users(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    add_source(pool, "s1", b"%PDF-1.4 x")
    return pool


def test_round_trip_and_upsert(users):
    pool = users
    with pool.connection() as conn:
        assert store.get_document(conn, "u1", "s1") is None
        store.put_document(conn, "u1", "s1", doc(), NOW)
        assert store.get_document(conn, "u1", "s1") == doc()
        store.put_document(conn, "u1", "s1", doc("docling"), NOW)
        assert store.get_document(conn, "u1", "s1") == doc("docling")


def test_cascade_on_source_delete_and_soft_delete(users):
    pool = users
    add_source(pool, "s2", b"%PDF-1.4 y")
    with pool.connection() as conn:
        store.put_document(conn, "u1", "s1", doc(), NOW)
        store.put_document(conn, "u1", "s2", doc(), NOW)
        assert store.soft_delete(conn, "u1", "s1", NOW)
        assert store.get_document(conn, "u1", "s1") is None
        n = conn.execute("select count(*) as n from source_document where source_id = 's1'").fetchone()
        assert n["n"] == 0
        conn.execute("delete from source where source_id = 's2'")
        n = conn.execute("select count(*) as n from source_document where source_id = 's2'").fetchone()
        assert n["n"] == 0
        with pytest.raises(LookupError):          # a deleted source takes no document
            store.put_document(conn, "u1", "s1", doc(), NOW)


def test_another_user_neither_writes_nor_reads(users):
    pool = users
    with pool.connection() as conn, pytest.raises(LookupError):
        store.put_document(conn, "u2", "s1", doc(), NOW)
    with pool.connection() as conn:
        store.put_document(conn, "u1", "s1", doc(), NOW)
        assert store.get_document(conn, "u2", "s1") is None
