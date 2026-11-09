"""A changed embedding model re-embeds stored sources by itself; until then search is BM25-only
for the stale rows (never a cross-space comparison)."""

import pytest

from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import store
from evowiki.sources.importer import embed_pending
from evowiki.sources.reembed import reembed_stale, start_background
from evowiki.sources.search import search_sources, vector_hits
from tests.runtime.helpers import make_user
from tests.sources.test_search import NOW, S1, _seed

OLD = HashingEmbedder()
OLD.model_version = "fastembed-0.7.0"           # the old tag format
NEW = HashingEmbedder()                          # same dim, another vector space (hash-v1-d384)


@pytest.mark.parametrize("sweep", [True, False])
def test_generation_changed_during_reembed_keeps_new_generation_unembedded(pool, sweep):
    make_user(pool, "u1")
    with pool.connection() as conn:
        _seed(conn, "s1", "u1", S1)
    embed_pending(pool, "u1", "s1", OLD)

    class Changed(HashingEmbedder):
        calls = 0

        def embed_documents(self, texts):
            self.calls += 1
            with pool.connection() as conn:
                store.bump_run(conn, "u1", "s1")
            return super().embed_documents(texts)

    changed = Changed()
    if sweep:
        assert reembed_stale(pool, changed) == 0
    else:
        from evowiki.sources.importer import _Deleted
        with pytest.raises(_Deleted):
            embed_pending(pool, "u1", "s1", changed)
    assert changed.calls == 1
    with pool.connection() as conn:
        assert len(store.unembedded_chunks(conn, "u1", "s1", changed)) == 3


def test_stale_rows_are_found_re_embedded_and_searchable(pool):
    make_user(pool, "u1")
    with pool.connection() as conn:
        _seed(conn, "s1", "u1", S1)
    embed_pending(pool, "u1", "s1", OLD)
    with pool.connection() as conn:
        assert store.unembedded_chunks(conn, "u1", "s1") == []          # embedded, but by OLD
        assert len(store.unembedded_chunks(conn, "u1", "s1", NEW)) == 3
        assert store.unembedded_sections(conn, "u1", "s1", NEW) == ["s1-sec"]
        assert store.sources_with_stale_vectors(conn, NEW) == [("u1", "s1")]
        assert store.sources_with_stale_vectors(conn, OLD) == []
        assert vector_hits(conn, "u1", NEW.embed_query(S1[0]), source_ids=None, k=5,
                           embedder=NEW) == []
    # while stale: BM25 still answers
    assert search_sources(pool, "u1", "特征方程", embedder=NEW)[0].chunk_id == "s1-c0"

    assert reembed_stale(pool, NEW) == 3

    with pool.connection() as conn:
        assert store.unembedded_chunks(conn, "u1", "s1", NEW) == []
        assert store.unembedded_sections(conn, "u1", "s1", NEW) == []
        assert store.sources_with_stale_vectors(conn, NEW) == []
        got = vector_hits(conn, "u1", NEW.embed_query(S1[1]), source_ids=None, k=1,
                          embedder=NEW)
    assert [h.chunk_id for h in got] == ["s1-c1"]
    assert reembed_stale(pool, NEW) == 0                                  # idempotent


def test_sweep_skips_a_source_being_imported_and_deleted_ones(pool):
    make_user(pool, "u1")
    with pool.connection() as conn:
        for sid in ("s1", "s2", "s3"):
            _seed(conn, sid, "u1", S1)
    for sid in ("s1", "s2", "s3"):
        embed_pending(pool, "u1", sid, OLD)
    with pool.connection() as conn:
        conn.execute("update source set status = 'reading' where source_id = 's1'")
        store.soft_delete(conn, "u1", "s2", NOW)
        conn.commit()
    assert reembed_stale(pool, NEW) == 3                                 # s3 only
    with pool.connection() as conn:
        assert store.unembedded_chunks(conn, "u1", "s1", NEW)           # the importer's job
        assert store.unembedded_chunks(conn, "u1", "s3", NEW) == []


def test_background_thread_does_the_sweep(pool):
    make_user(pool, "u1")
    with pool.connection() as conn:
        _seed(conn, "s1", "u1", S1)
    embed_pending(pool, "u1", "s1", OLD)
    t, _stop = start_background(pool, NEW)
    t.join(30)
    assert not t.is_alive()
    with pool.connection() as conn:
        assert store.sources_with_stale_vectors(conn, NEW) == []


def test_failure_is_raised_not_swallowed(pool):
    make_user(pool, "u1")
    with pool.connection() as conn:
        _seed(conn, "s1", "u1", S1)
    embed_pending(pool, "u1", "s1", OLD)

    class Boom(HashingEmbedder):
        def embed_documents(self, texts):
            raise RuntimeError("onnx down")

    with pytest.raises(RuntimeError, match="onnx down"):
        reembed_stale(pool, Boom())
    with pool.connection() as conn:                                      # rows untouched
        assert store.sources_with_stale_vectors(conn, OLD) == []
