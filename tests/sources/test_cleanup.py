"""Q7: chat attachments nobody kept are cleaned up 30 days after upload."""

from datetime import UTC, datetime, timedelta

from evowiki.sources import store
from evowiki.sources.cleanup import sweep_ask_attachments
from evowiki.sources.models import SectionRow, SourceRow
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


class Runs:
    def __init__(self):
        self.cancelled: list[tuple[str, str]] = []

    def cancel(self, user_id, source_id):
        self.cancelled.append((user_id, source_id))


def add(pool, sid, days_old, uses, *, user="u1", linked=False):
    at = NOW - timedelta(days=days_old)
    row = SourceRow(source_id=sid, user_id=user, kind="text", title=sid, sha256=sid, bytes=1,
                    pages=None, status="ready", error=None, unreadable_pages=(), toc_mode=None,
                    progress={}, origin=None, created_at=at, updated_at=at, deleted_at=None)
    with pool.connection() as conn:
        store.insert_source(conn, row, b"x", "text/plain")
        for u in uses:
            store.add_use(conn, user, sid, u, session_id="chat1", now=at)
        if linked:
            store.replace_sections(conn, user, sid, [SectionRow(
                section_id=f"{sid}-1", source_id=sid, parent_id=None, ord=0, depth=0, title="t",
                page_from=None, page_to=None, text="x", tokens=1, summary=None, read_by="text")])
            conn.execute("insert into source_link (user_id, node_id, section_id, role, by, "
                         "created_at) values (%s, 'n1', %s, 'teaches', 'user', %s)",
                         (user, f"{sid}-1", at))


def deleted(pool, sid, user="u1"):
    with pool.connection() as conn:
        return store.get_source(conn, user, sid).deleted_at is not None


def test_only_old_ask_only_unlinked_sources_are_cleaned_up(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    add(pool, "old", 31, ["ask"])
    add(pool, "old2", 40, ["ask", "ask"], user="u2")
    add(pool, "young", 29, ["ask"])
    add(pool, "kept", 31, ["ask", "learn"])
    add(pool, "linked", 31, ["ask"], linked=True)
    add(pool, "unused", 31, [])                 # an upload without a use: not an attachment
    runs = Runs()
    assert sweep_ask_attachments(pool, runs, NOW) == 2
    assert sorted(runs.cancelled) == [("u1", "old"), ("u2", "old2")]
    assert deleted(pool, "old") and deleted(pool, "old2", "u2")
    assert not any(deleted(pool, s) for s in ("young", "kept", "linked", "unused"))
    with pool.connection() as conn:
        assert store.get_file(conn, "u1", "old") is None          # the original is gone
        assert store.get_source(conn, "u1", "old").title == "old"  # the row stays
    assert sweep_ask_attachments(pool, runs, NOW) == 0             # nothing left to do


def test_an_old_file_attached_again_is_young_again(pool):
    make_user(pool, "u1")
    add(pool, "old", 45, ["ask"])
    with pool.connection() as conn:
        store.add_use(conn, "u1", "old", "ask", session_id="chat2", now=NOW - timedelta(days=1))
    assert sweep_ask_attachments(pool, Runs(), NOW) == 0
    assert not deleted(pool, "old")
    assert sweep_ask_attachments(pool, Runs(), NOW + timedelta(days=31)) == 1


def test_a_use_or_link_added_after_the_scan_keeps_the_source(pool, monkeypatch):
    """D18: the scan found them stale; a fresh ask use / a learn use / a link lands before the
    delete step, which judges again under the lock and keeps each one."""
    make_user(pool, "u1")
    for sid in ("asked", "learned", "linked", "stale"):
        add(pool, sid, 40, ["ask"])
    scan = store.stale_ask_only

    def scan_then_use(conn, **kw):
        found = scan(conn, **kw)
        with pool.connection() as other:          # committed before the delete step runs
            store.add_use(other, "u1", "asked", "ask", session_id="chat2", now=NOW)
            store.add_use(other, "u1", "learned", "learn", now=NOW - timedelta(days=40))
            store.replace_sections(other, "u1", "linked", [SectionRow(
                section_id="linked-1", source_id="linked", parent_id=None, ord=0, depth=0,
                title="t", page_from=None, page_to=None, text="x", tokens=1, summary=None,
                read_by="text")])
            other.execute("insert into source_link (user_id, node_id, section_id, role, by, "
                          "created_at) values ('u1', 'n1', 'linked-1', 'teaches', 'user', %s)",
                          (NOW,))
        return found

    monkeypatch.setattr(store, "stale_ask_only", scan_then_use)
    runs = Runs()
    assert sweep_ask_attachments(pool, runs, NOW) == 1
    assert runs.cancelled == [("u1", "stale")]                     # the kept ones keep their run
    assert deleted(pool, "stale")
    assert not any(deleted(pool, s) for s in ("asked", "learned", "linked"))
    with pool.connection() as conn:
        assert store.get_file(conn, "u1", "asked") is not None
