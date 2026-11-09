from datetime import UTC, datetime

import psycopg
import pytest

from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import store
from evowiki.sources.models import ChunkRow, SectionRow, SourceRow
from evowiki.sources.parsed import OutlineEntry, PageText, ParsedDoc, ParseError
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
EMB = HashingEmbedder()


def _src(sid="s1", user="u1", sha="aa", status="reading", **kw) -> SourceRow:
    base = {"source_id": sid, "user_id": user, "kind": "pdf", "title": "书", "sha256": sha,
            "bytes": 3, "pages": None, "status": status, "error": None, "unreadable_pages": (),
            "toc_mode": None, "progress": {}, "origin": None, "created_at": NOW,
            "updated_at": NOW, "deleted_at": None}
    base.update(kw)
    return SourceRow(**base)


def _sec(sid, ord_, source="s1", parent=None, text="正文") -> SectionRow:
    return SectionRow(section_id=sid, source_id=source, parent_id=parent, ord=ord_, depth=0,
                      title=f"节{ord_}", page_from=1, page_to=2, text=text, tokens=3,
                      summary=None, read_by="text")


@pytest.fixture
def db(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    with pool.connection() as conn:
        yield conn
        conn.rollback()


def test_types_construct():
    d = ParsedDoc(kind="md", title="t", pages=(PageText(1, "x", "text"),),
                  outline=(OutlineEntry("h", 0, None),), headings_inline=True)
    assert d.pages[0].reason is None and issubclass(ParseError, Exception)


def test_insert_get_list_and_isolation(db):
    store.insert_source(db, _src(), b"abc", "application/pdf")
    got = store.get_source(db, "u1", "s1")
    assert got == _src()
    assert [r.source_id for r in store.list_sources(db, "u1")] == ["s1"]
    assert store.get_source(db, "u2", "s1") is None
    assert store.list_sources(db, "u2") == []
    assert store.get_file(db, "u2", "s1") is None
    assert store.get_file(db, "u1", "s1") == (b"abc", "application/pdf")


def test_find_by_sha_skips_failed_and_deleted(db):
    store.insert_source(db, _src("s1", sha="h"), b"a", "x/y")
    assert store.find_by_sha(db, "u1", "h").source_id == "s1"
    assert store.find_by_sha(db, "u2", "h") is None
    store.set_status(db, "u1", "s1", status="failed", error="坏了")
    assert store.find_by_sha(db, "u1", "h") is None
    store.insert_source(db, _src("s2", sha="k"), b"a", "x/y")
    store.soft_delete(db, "u1", "s2", NOW)
    assert store.find_by_sha(db, "u1", "k") is None


def test_set_status_and_progress(db):
    store.insert_source(db, _src(), b"a", "x/y")
    store.set_status(db, "u1", "s1", status="partial", error="部分", pages=9,
                     unreadable_pages=[3, 4], toc_mode="pages")
    store.set_progress(db, "u1", "s1", {"stage": "sections", "done": 2})
    r = store.get_source(db, "u1", "s1")
    assert (r.status, r.error, r.pages, r.unreadable_pages, r.toc_mode) == (
        "partial", "部分", 9, (3, 4), "pages")
    assert r.progress == {"stage": "sections", "done": 2}


def test_replace_sections_reentrant(db):
    store.insert_source(db, _src(), b"a", "x/y")
    store.replace_sections(db, "u1", "s1", [_sec("a", 0), _sec("b", 1, parent="a")])
    store.replace_sections(db, "u1", "s1", [_sec("c", 0, text="新")])
    got = store.sections(db, "u1", "s1")
    assert [s.section_id for s in got] == ["c"] and got[0].text == "新"
    assert store.sections(db, "u2", "s1") == []


def test_summaries_cache(db):
    store.insert_source(db, _src(), b"a", "x/y")
    store.replace_sections(db, "u1", "s1", [_sec("a", 0)])
    store.set_summaries(db, "u1", "s1", [("a", "摘要", "h1")])
    assert store.sections(db, "u1", "s1")[0].summary == "摘要"
    assert store.summaries_by_hash(db, "u1", ["h1", "zz"]) == {"h1": "摘要"}
    assert store.summaries_by_hash(db, "u2", ["h1"]) == {}


def test_chunks_and_dimension(db):
    store.insert_source(db, _src(), b"a", "x/y")
    store.replace_sections(db, "u1", "s1", [_sec("a", 0)])
    ch = [ChunkRow("k1", "a", "s1", 0, 1, "你好 world"), ChunkRow("k2", "a", "s1", 1, 2, "第二")]
    vecs = EMB.embed_documents([c.text for c in ch])
    store.replace_chunks(db, "u1", "s1", ch, vecs, EMB)
    assert store.chunks_of(db, "u1", ["s1"]) == ch
    assert store.chunks_of(db, "u2", ["s1"]) == []
    row = db.execute("select dim, embedder_id, vector_dims(embedding) as d from source_chunk "
                     "limit 1").fetchone()
    assert row["dim"] == row["d"] == EMB.dim and row["embedder_id"] == "hash"
    store.set_section_embeddings(db, "u1", "s1", [("a", EMB.embed_query("x"))], EMB)
    assert db.execute("select dim from source_section where section_id='a'"
                      ).fetchone()["dim"] == EMB.dim
    with pytest.raises(psycopg.Error), db.transaction():
        store.replace_chunks(db, "u1", "s1", ch[:1], [[0.1] * 5], EMB)
    with pytest.raises(psycopg.Error), db.transaction():
        store.set_section_embeddings(db, "u1", "s1", [("a", [0.1] * 5)], EMB)


def test_soft_delete(db):
    store.insert_source(db, _src(), b"a", "x/y")
    store.replace_sections(db, "u1", "s1", [_sec("a", 0)])
    store.replace_chunks(db, "u1", "s1", [ChunkRow("k1", "a", "s1", 0, 1, "t")],
                         EMB.embed_documents(["t"]), EMB)
    assert store.soft_delete(db, "u2", "s1", NOW) is False
    assert store.soft_delete(db, "u1", "s1", NOW) is True
    assert store.get_file(db, "u1", "s1") is None
    assert store.sections(db, "u1", "s1") == []
    assert store.chunks_of(db, "u1", ["s1"]) == []
    r = store.get_source(db, "u1", "s1")
    assert r is not None and r.deleted_at == NOW and r.title == "书"
    assert store.list_sources(db, "u1") == []


def test_uses(db):
    store.insert_source(db, _src(), b"a", "x/y")
    uid = store.add_use(db, "u1", "s1", "learn", goal_id="g", page_id="p", session_id="x", now=NOW)
    store.add_use(db, "u1", "s1", "ask", now=NOW)
    uses = store.uses_of(db, "u1", "s1")
    assert len(uses) == 2 and {u["use"] for u in uses} == {"learn", "ask"}
    first = next(u for u in uses if u["use_id"] == uid)
    assert (first["goal_id"], first["page_id"], first["session_id"]) == ("g", "p", "x")
    assert store.uses_of(db, "u2", "s1") == []


def test_writes_after_soft_delete_fail(db):
    store.insert_source(db, _src(), b"a", "x/y")
    store.replace_sections(db, "u1", "s1", [_sec("a", 0)])
    store.soft_delete(db, "u1", "s1", NOW)
    with pytest.raises(LookupError):
        store.replace_sections(db, "u1", "s1", [_sec("a", 0)])
    with pytest.raises(LookupError):
        store.replace_chunks(db, "u1", "s1", [], [], EMB)
    with pytest.raises(LookupError):
        store.set_status(db, "u1", "s1", status="ready")
    with pytest.raises(LookupError):
        store.set_progress(db, "u1", "s1", {})
    with pytest.raises(LookupError):
        store.add_use(db, "u1", "s1", "ask", now=NOW)
    assert store.sections(db, "u1", "s1") == [] and store.chunks_of(db, "u1", ["s1"]) == []


def test_foreign_user_writes_raise(db):
    store.insert_source(db, _src(), b"a", "x/y")
    store.replace_sections(db, "u1", "s1", [_sec("a", 0)])
    for call in (
        lambda: store.replace_sections(db, "u2", "s1", [_sec("z", 0)]),
        lambda: store.replace_chunks(db, "u2", "s1", [], [], EMB),
        lambda: store.set_status(db, "u2", "s1", status="ready"),
        lambda: store.set_progress(db, "u2", "s1", {}),
        lambda: store.set_summaries(db, "u2", "s1", [("a", "x", "h")]),
        lambda: store.set_section_embeddings(db, "u2", "s1", [("a", EMB.embed_query("x"))], EMB),
        lambda: store.add_use(db, "u2", "s1", "ask", now=NOW),
        lambda: store.set_status(db, "u1", "nope", status="ready"),
    ):
        with pytest.raises(LookupError):
            call()
    assert [s.section_id for s in store.sections(db, "u1", "s1")] == ["a"]


def test_unknown_section_and_value_guards(db):
    store.insert_source(db, _src(), b"a", "x/y")
    store.replace_sections(db, "u1", "s1", [_sec("a", 0)])
    with pytest.raises(LookupError):
        store.set_summaries(db, "u1", "s1", [("missing", "x", "h")])
    with pytest.raises(LookupError):
        store.set_section_embeddings(db, "u1", "s1", [("missing", EMB.embed_query("x"))], EMB)
    with pytest.raises(ValueError):
        store.replace_sections(db, "u1", "s1", [_sec("b", 0, source="other")])
    ch = ChunkRow("k1", "a", "s1", 0, 1, "t")
    with pytest.raises(ValueError):
        store.replace_chunks(db, "u1", "s1", [ch], [], EMB)
    with pytest.raises(ValueError):
        store.replace_chunks(db, "u1", "s1", [ChunkRow("k2", "a", "other", 0, 1, "t")],
                             EMB.embed_documents(["t"]), EMB)


def test_concurrent_writers_wait_instead_of_deadlocking(pool):
    import threading
    make_user(pool, "u1")
    with pool.connection() as c:
        store.insert_source(c, _src("dl1"), b"a", "x/y")
        c.commit()
    errors: list[BaseException] = []
    try:
        with pool.connection() as a, pool.connection() as b:
            store._require_owner(a, "u1", "dl1")  # a holds the owner lock

            def second():
                try:
                    store.set_progress(b, "u1", "dl1", {"stage": "b"})
                    b.commit()
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

            t = threading.Thread(target=second)
            t.start()
            t.join(0.5)
            assert t.is_alive()  # b waits for a instead of racing ahead
            store.set_progress(a, "u1", "dl1", {"stage": "a"})  # would deadlock with `for share`
            a.commit()
            t.join(10)
            assert not t.is_alive() and errors == []
    finally:
        with pool.connection() as c:
            c.execute("delete from source where source_id = 'dl1'")
            c.commit()
