from datetime import UTC, datetime
from pathlib import Path

from evowiki.content.models import Lang
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.store.content import import_snapshot_file, load_content
from evowiki.store.vectors import index_segments, search_segments

SNAP = Path(__file__).resolve().parents[1] / "fixtures" / "runtime" / "content_snapshot.json"


def test_index_and_search(pool):
    e = HashingEmbedder()
    with pool.connection() as conn:
        cv = import_snapshot_file(conn, SNAP, datetime(2026, 10, 2, tzinfo=UTC))
        lc = load_content(conn, cv)
        assert index_segments(conn, lc, e) == len(lc.graph.segments)
        assert index_segments(conn, lc, e) == 0  # already indexed
        target = lc.graph.segments_for("pca")[0]
        hits = search_segments(conn, cv, e, target.body, k=3, lang=Lang.EN)
        row = conn.execute("select embedder_id, model_version, dim from segment_embedding "
                           "limit 1").fetchone()
    assert hits[0][0] == target.id and hits[0][1] > 0.6
    assert len(hits) == 3 and hits == sorted(hits, key=lambda h: -h[1])
    assert row == {"embedder_id": "hash", "model_version": "hash-v1-d384", "dim": 384}


def test_models_are_never_mixed(pool):
    small, big = HashingEmbedder(dim=16), HashingEmbedder(dim=384)  # two "models", one id
    assert small.model_version != big.model_version
    with pool.connection() as conn:
        cv = import_snapshot_file(conn, SNAP, datetime(2026, 10, 2, tzinfo=UTC))
        lc = load_content(conn, cv)
        index_segments(conn, lc, small)
        assert search_segments(conn, cv, big, "variance", k=3) == []  # big never indexed
        n = index_segments(conn, lc, big)  # full re-embed under the new model
        assert n == len(lc.graph.segments)
        assert len(search_segments(conn, cv, small, "variance", k=3)) == 3
        assert len(search_segments(conn, cv, big, "variance", k=3)) == 3
