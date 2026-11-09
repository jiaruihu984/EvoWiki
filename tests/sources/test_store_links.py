from datetime import UTC, datetime

import psycopg
import pytest

from evowiki.sources import store
from evowiki.sources.models import LinkRow
from tests.runtime.helpers import make_user
from tests.sources.test_store import _sec, _src

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


@pytest.fixture
def db(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    with pool.connection() as conn:
        store.insert_source(conn, _src("s1", "u1", "a"), b"a", "x/y")
        store.replace_sections(conn, "u1", "s1", [_sec("a1", 0, "s1"), _sec("a2", 1, "s1")])
        store.insert_source(conn, _src("s2", "u2", "b"), b"a", "x/y")
        store.replace_sections(conn, "u2", "s2", [_sec("b1", 0, "s2")])
        yield conn
        conn.rollback()


def _l(node, sec, src, role="teaches", by="intake") -> LinkRow:
    return LinkRow(node, sec, src, role, by)


def test_users_do_not_collide_and_cross_user_refused(db):
    with pytest.raises(LookupError):
        store.replace_links(db, "u2", ["c1"], [_l("c1", "a1", "s1")], by="intake", now=NOW)
    store.replace_links(db, "u1", ["c1"], [_l("c1", "a1", "s1")], by="intake", now=NOW)
    store.replace_links(db, "u2", ["c1"], [_l("c1", "b1", "s2")], by="intake", now=NOW)
    assert store.links_for_nodes(db, "u1", ["c1"]) == [_l("c1", "a1", "s1")]
    assert store.links_for_nodes(db, "u2", ["c1"]) == [_l("c1", "b1", "s2")]


def test_replace_links_replaces(db):
    store.replace_links(db, "u1", ["c1"], [_l("c1", "a1", "s1")], by="intake", now=NOW)
    store.replace_links(db, "u1", ["c1"], [_l("c1", "a2", "s1")], by="intake", now=NOW)
    assert store.links_for_nodes(db, "u1", ["c1"]) == [_l("c1", "a2", "s1")]
    assert store.links_for_sources(db, "u1", ["s1"]) == [_l("c1", "a2", "s1")]


def test_reads_role_ok_and_bogus_rejected(db):
    store.replace_links(db, "u1", ["c1"], [_l("c1", "a1", "s1", "reads")], by="intake", now=NOW)
    assert store.links_for_nodes(db, "u1", ["c1"])[0].role == "reads"
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into source_link (user_id, node_id, section_id, role, by, created_at) "
                   "values ('u1', 'c2', 'a1', 'bogus', 'intake', now())")


def test_soft_deleted_source_links_gone(db):
    store.replace_links(db, "u1", ["c1"], [_l("c1", "a1", "s1")], by="intake", now=NOW)
    store.soft_delete(db, "u1", "s1", NOW)
    assert store.links_for_nodes(db, "u1", ["c1"]) == []
    assert store.links_for_sources(db, "u1", ["s1"]) == []


def test_replace_links_touches_only_the_same_by(db):
    store.replace_links(db, "u1", ["c1"], [_l("c1", "a1", "s1", by="intake")], by="intake",
                        now=NOW)
    store.replace_links(db, "u1", ["c1"], [_l("c1", "a2", "s1", by="user")], by="user", now=NOW)
    store.replace_links(db, "u1", ["c1"], [], by="intake", now=NOW)
    assert store.links_for_nodes(db, "u1", ["c1"]) == [_l("c1", "a2", "s1", by="user")]


def test_replace_links_refuses_a_link_with_another_by(db):
    with pytest.raises(ValueError):
        store.replace_links(db, "u1", ["c1"], [_l("c1", "a1", "s1", by="user")], by="intake",
                            now=NOW)


def test_reading_by_ok_and_reads_role_kept(db):
    """0088 (reading-mode spec §7 item 2): a unit's teaches link is by "reading"; the old reads
    role still holds; an unknown `by` is still refused."""
    store.replace_links(db, "u1", ["c1"], [_l("c1", "a1", "s1", by="reading")], by="reading",
                        now=NOW)
    store.replace_links(db, "u1", ["c2"], [_l("c2", "a2", "s1", "reads")], by="intake", now=NOW)
    assert store.links_for_nodes(db, "u1", ["c1", "c2"]) == [
        _l("c1", "a1", "s1", by="reading"), _l("c2", "a2", "s1", "reads")]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into source_link (user_id, node_id, section_id, role, by, created_at) "
                   "values ('u1', 'c3', 'a1', 'teaches', 'bogus', now())")



def test_replace_source_links_replaces_one_materials_links_only(db):
    """Reading links are replaced per material (wikiapp.reading.teaches_links): a node's reading
    link into another material stays; a row no longer listed goes."""
    store.insert_source(db, _src("s3", "u1", "c"), b"a", "x/y")
    store.replace_sections(db, "u1", "s3", [_sec("c1", 0, "s3")])
    store.replace_links(db, "u1", ["n1"], [_l("n1", "c1", "s3", by="reading")], by="reading",
                        now=NOW)
    store.replace_source_links(db, "u1", "s1", [_l("n1", "a1", "s1", by="reading"),
                                                _l("n2", "a2", "s1", by="reading")],
                               by="reading", now=NOW)
    store.replace_source_links(db, "u1", "s1", [_l("n1", "a2", "s1", by="reading")],
                               by="reading", now=NOW)
    assert store.links_for_sources(db, "u1", ["s1"]) == [_l("n1", "a2", "s1", by="reading")]
    assert store.links_for_sources(db, "u1", ["s3"]) == [_l("n1", "c1", "s3", by="reading")]
    with pytest.raises(ValueError):
        store.replace_source_links(db, "u1", "s1", [_l("n1", "b1", "s2", by="reading")],
                                   by="reading", now=NOW)
