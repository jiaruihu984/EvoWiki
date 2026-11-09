from datetime import UTC, datetime

import pytest

from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import store
from evowiki.sources.models import ChunkRow, SectionRow, SourceRow
from evowiki.sources.search import (
    SourceHit,
    bm25_hits,
    rrf,
    search_sources,
    tokenize,
    vector_hits,
)
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
EMB = HashingEmbedder()

S1 = ["矩阵的特征值满足特征方程。", "线性变换把向量映射到向量。", "行列式等于特征值的乘积。"]
S2 = ["谱定理说明对称矩阵可以正交对角化。", "Hermitian matrices have real eigenvalues."]


def _src(sid: str, user: str) -> SourceRow:
    return SourceRow(source_id=sid, user_id=user, kind="md", title=sid, sha256=sid, bytes=1,
                     pages=None, status="ready", error=None, unreadable_pages=(), toc_mode=None,
                     progress={}, origin=None, created_at=NOW, updated_at=NOW, deleted_at=None)


def _chunks(sid: str, texts: list[str]) -> list[ChunkRow]:
    return [ChunkRow(chunk_id=f"{sid}-c{i}", section_id=f"{sid}-sec", source_id=sid, ord=i,
                     page=i + 1, text=t) for i, t in enumerate(texts)]


def _seed(conn, sid: str, user: str, texts: list[str]) -> None:
    store.insert_source(conn, _src(sid, user), b"x", "text/markdown")
    store.replace_sections(conn, user, sid, [SectionRow(
        section_id=f"{sid}-sec", source_id=sid, parent_id=None, ord=0, depth=0, title="节",
        page_from=1, page_to=len(texts), text="\n".join(texts), tokens=10, summary=None,
        read_by="text")])
    chunks = _chunks(sid, texts)
    store.replace_chunks(conn, user, sid, chunks, EMB.embed_documents(texts), EMB)


@pytest.fixture
def seeded(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    with pool.connection() as conn:
        _seed(conn, "s1", "u1", S1)
        _seed(conn, "s2", "u1", S2)
        conn.commit()
    return pool


def _hit(cid: str) -> SourceHit:
    return SourceHit(chunk_id=cid, source_id="s", section_id="x", page=None, text=cid, score=0.0)


def test_tokenize_mixed_text():
    toks = tokenize("特征值和Eigenvalue")
    assert "特征值" in toks and "eigenvalue" in toks
    assert "和" in toks  # single CJK characters are kept


def test_tokenize_drops_punctuation_and_space():
    toks = tokenize("矩阵 A ， 向量！ e.g.")
    assert toks and all(t.strip() and any(ch.isalnum() for ch in t) for t in toks)
    assert "a" in toks and "矩阵" in toks


def test_bm25_finds_rare_term():
    chunks = _chunks("s1", S1) + _chunks("s2", S2)
    hits = bm25_hits(chunks, "Hermitian", k=3)
    assert [h.chunk_id for h in hits] == ["s2-c1"]
    assert hits[0].score > 0 and hits[0].page == 2


def test_bm25_empty_inputs():
    assert bm25_hits([], "特征值", k=3) == []
    assert bm25_hits(_chunks("s1", S1), "！！", k=3) == []
    assert bm25_hits(_chunks("s1", S1), "毫不相干xyz", k=3) == []


def test_bm25_symbol_only_corpus_and_query():
    sym = _chunks("s9", ["∑ ≤ ∫", "（）", "→ ⊗"])
    assert bm25_hits(sym, "特征值", k=3) == []
    assert bm25_hits(sym, "∑", k=3) == []
    mixed = sym + _chunks("s8", ["特征值"])
    assert [h.chunk_id for h in bm25_hits(mixed, "特征值", k=3)] == ["s8-c0"]


def test_search_survives_symbol_only_chunks(pool):
    make_user(pool, "u1")
    texts = ["∑ ≤ ∫", "→ ⊗"]
    with pool.connection() as conn:
        _seed(conn, "s9", "u1", texts)
        conn.commit()
    hits = search_sources(pool, "u1", "特征值", embedder=EMB, k=5)
    assert {h.chunk_id for h in hits} == {"s9-c0", "s9-c1"}  # vector results kept


def test_vector_hits_skips_deleted_without_cascade(seeded):
    with seeded.connection() as conn:
        conn.execute("update source set deleted_at = %s where source_id = 's2'", (NOW,))
        hits = vector_hits(conn, "u1", EMB.embed_query(S2[1]), source_ids=None, k=8,
                           embedder=EMB)
        assert hits and all(h.source_id == "s1" for h in hits)
        assert vector_hits(conn, "u1", EMB.embed_query(S2[1]), source_ids=["s2"], k=8,
                           embedder=EMB) == []
        conn.rollback()


def test_vector_finds_exact_sentence(seeded):
    with seeded.connection() as conn:
        q = EMB.embed_query(S1[1])
        hits = vector_hits(conn, "u1", q, source_ids=None, k=3, embedder=EMB)
    assert hits[0].chunk_id == "s1-c1" and hits[0].text == S1[1]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert len(hits) == 3


def test_rrf_matches_hand_computation():
    a = [_hit("x"), _hit("y"), _hit("z")]
    b = [_hit("z"), _hit("x")]
    out = rrf([a, b], k=10, c=60)
    expected = {"x": 1 / 61 + 1 / 62, "y": 1 / 62, "z": 1 / 63 + 1 / 61}
    assert [h.chunk_id for h in out] == ["x", "z", "y"]
    for h in out:
        assert h.score == pytest.approx(expected[h.chunk_id])
    assert [h.chunk_id for h in rrf([a, b], k=2)] == ["x", "z"]


def test_rrf_ties_break_by_chunk_id():
    out = rrf([[_hit("b")], [_hit("a")]], k=5)
    assert [h.chunk_id for h in out] == ["a", "b"]


def test_search_sources_hybrid(seeded):
    hits = search_sources(seeded, "u1", "Hermitian eigenvalues", embedder=EMB, k=3)
    assert hits[0].chunk_id == "s2-c1"
    assert len({h.chunk_id for h in hits}) == len(hits) <= 3


def test_source_ids_filter(seeded):
    hits = search_sources(seeded, "u1", "Hermitian eigenvalues", embedder=EMB,
                          source_ids=["s1"], k=8)
    assert hits and {h.source_id for h in hits} == {"s1"}
    with seeded.connection() as conn:
        v = vector_hits(conn, "u1", EMB.embed_query(S2[1]), source_ids=["s1"], k=8,
                        embedder=EMB)
    assert {h.source_id for h in v} == {"s1"}


def test_other_user_sees_nothing(seeded):
    assert search_sources(seeded, "u2", "Hermitian eigenvalues", embedder=EMB) == []
    assert search_sources(seeded, "u2", "特征值", embedder=EMB, source_ids=["s1", "s2"]) == []


def test_deleted_source_not_found(seeded):
    with seeded.connection() as conn:
        store.soft_delete(conn, "u1", "s2", NOW)
        conn.commit()
    hits = search_sources(seeded, "u1", "Hermitian eigenvalues", embedder=EMB, k=8)
    assert hits and all(h.source_id == "s1" for h in hits)
    assert search_sources(seeded, "u1", "Hermitian", embedder=EMB, source_ids=["s2"]) == []


def test_other_embedder_rows_ignored(seeded):
    other = HashingEmbedder(dim=128)
    with seeded.connection() as conn:
        assert vector_hits(conn, "u1", other.embed_query(S1[0]), source_ids=None, k=5,
                           embedder=other) == []


def test_chunks_without_vectors_are_found_by_bm25_then_by_vector(pool):
    from evowiki.sources.search import search_sources, vector_hits
    make_user(pool, "u1")
    with pool.connection() as conn:
        store.insert_source(conn, _src("s3", "u1"), b"x", "text/markdown")
        store.replace_sections(conn, "u1", "s3", [SectionRow(
            section_id="s3-sec", source_id="s3", parent_id=None, ord=0, depth=0, title="节",
            page_from=1, page_to=3, text="\n".join(S1), tokens=10, summary=None,
            read_by="text")])
        store.put_chunks(conn, "u1", "s3", _chunks("s3", S1))      # no vectors yet
        assert vector_hits(conn, "u1", EMB.embed_query("特征值"), source_ids=None, k=5,
                           embedder=EMB) == []
    hits = search_sources(pool, "u1", "特征方程", embedder=EMB)
    assert hits and hits[0].chunk_id == "s3-c0"
    with pool.connection() as conn:
        todo = store.unembedded_chunks(conn, "u1", "s3")
        assert [c.chunk_id for c in todo] == ["s3-c0", "s3-c1", "s3-c2"]
        store.set_chunk_embeddings(conn, "u1", "s3", [
            (c.chunk_id, v) for c, v in zip(todo, EMB.embed_documents(S1), strict=True)], EMB)
        assert store.unembedded_chunks(conn, "u1", "s3") == []
        got = vector_hits(conn, "u1", EMB.embed_query(S1[1]), source_ids=None, k=1,
                          embedder=EMB)
    assert [h.chunk_id for h in got] == ["s3-c1"]


# --- tokenize once per call -------------------------------------------------------------------


def _old_phrase_hits(chunks, phrases, *, k):
    best = {}
    for phrase in phrases:
        need = set(tokenize(phrase))
        if not need:
            continue
        for h in bm25_hits(chunks, phrase, k=len(chunks)):
            if need <= set(tokenize(h.text)) and (
                    h.chunk_id not in best or h.score > best[h.chunk_id].score):
                best[h.chunk_id] = h
    return sorted(best.values(), key=lambda h: (-h.score, h.chunk_id))[:k]


def test_phrase_hits_same_as_per_phrase_retokenising_and_tokenises_each_text_once(monkeypatch):
    from evowiki.sources import search as srch
    chunks = _chunks("a", S1 + S2)
    phrases = ["特征值", "矩阵", "eigenvalues", "不存在的词", "", "特征方程"]
    expected = _old_phrase_hits(chunks, phrases, k=10)
    calls = []
    real = srch.tokenize
    monkeypatch.setattr(srch, "tokenize", lambda t: (calls.append(t), real(t))[1])
    got = srch.phrase_hits(chunks, phrases, k=10)
    assert [(h.chunk_id, h.score) for h in got] == [(h.chunk_id, h.score) for h in expected]
    assert got
    texts = [c.text for c in chunks]
    assert all(calls.count(t) == 1 for t in texts)              # each chunk text once
    assert len(calls) == len(texts) + len(phrases)              # + one per phrase
