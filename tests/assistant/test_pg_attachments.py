"""PgAttachmentReader over the real source tables: scoping, content, cards (expiry rules)."""

from datetime import timedelta

import pytest

from evowiki.assistant.attachments import AttachmentGone, PgAttachmentReader, attachment_out
from evowiki.sources import store
from evowiki.sources.models import PageRead
from tests.runtime.helpers import make_user
from tests.sources.test_store import NOW, _sec, _src


@pytest.fixture
def rd(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    with pool.connection() as conn:
        for sid in ("a", "b"):
            store.insert_source(conn, _src(sid, sha=sid, status="ready", pages=2), b"PDF" + sid.encode(),
                                "application/pdf")
            store.replace_sections(conn, "u1", sid, [_sec(f"{sid}1", 0, source=sid)])
            store.add_use(conn, "u1", sid, "ask", session_id="s1", now=NOW)
        store.put_page_reads(conn, "u1", "a", [PageRead(2, None, "local", True)], NOW)
        store.add_use(conn, "u1", "b", "learn", now=NOW)
    return PgAttachmentReader(pool)


def test_load_scopes_by_user_and_reads_content(rd):
    a, b = rd.load("u1", ["a", "b"])
    assert a.sections[0].section_id == "a1" and a.reads[2].figure
    assert a.uses == ("ask",) and set(b.uses) == {"ask", "learn"} and not a.linked
    assert rd.file("u1", "a") == b"PDFa"
    with pytest.raises(AttachmentGone):
        rd.load("u2", ["a"])
    with pytest.raises(AttachmentGone):
        rd.file("u2", "a")
    bare = rd.load("u1", ["a"], content=False)[0]
    assert bare.sections == () and dict(bare.reads) == {}


def test_cards_expiry_link_and_deletion(rd, pool):
    a, b = rd.load("u1", ["a", "b"], content=False)
    assert attachment_out(a).expires_at == NOW + timedelta(days=30)
    assert attachment_out(b).expires_at is None                       # a non-ask use
    with pool.connection() as conn:          # attached again: the cleanup counts from then
        store.add_use(conn, "u1", "a", "ask", session_id="s2", now=NOW + timedelta(days=9))
    again = rd.load("u1", ["a"], content=False)[0]
    assert attachment_out(again).expires_at == NOW + timedelta(days=39)
    assert attachment_out(rd.load("u1", ["a"])[0]).expires_at == NOW + timedelta(days=39)
    with pool.connection() as conn:
        conn.execute("insert into source_link (user_id, node_id, section_id, role, by, "
                     "created_at) values ('u1', 'n1', 'a1', 'teaches', 'user', %s)", (NOW,))
    assert rd.load("u1", ["a"], content=False)[0].linked
    with pool.connection() as conn:
        store.soft_delete(conn, "u1", "a", NOW)
    gone = rd.load("u1", ["a"])[0]
    assert gone.deleted and gone.sections == ()
    card = attachment_out(gone)
    assert card.deleted and card.expires_at is None and card.title == "书"
    with pytest.raises(AttachmentGone):
        rd.file("u1", "a")


def test_characters_are_counted_in_sql_as_in_python(rd, pool):
    from dataclasses import replace

    from evowiki.assistant.attachments import text_chars
    secs = [_sec("c1", 0, source="c", text="〔p.1〕一二三 四\n五"), _sec("c2", 1, source="c", text="六"),
            replace(_sec("c3", 2, source="c", text="不算"), read_by="none")]
    with pool.connection() as conn:
        store.insert_source(conn, _src("c", sha="c", status="partial", pages=3,
                                       unreadable_pages=(3,)), b"PDF", "application/pdf")
        store.replace_sections(conn, "u1", "c", secs)
        store.insert_source(conn, _src("r", sha="r"), b"PDF", "application/pdf")  # reading
    bare, reading = rd.load("u1", ["c", "r"], content=False)
    assert bare.chars == 6 == text_chars(secs) == rd.load("u1", ["c"])[0].chars
    assert reading.chars is None
    card = attachment_out(bare)
    assert card.chars == 6 and card.unreadable_pages == (3,)
