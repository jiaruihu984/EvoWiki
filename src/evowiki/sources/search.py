"""Hybrid retrieval over source chunks: exact pgvector cosine + in-process BM25 (bm25s over jieba
tokens), merged by reciprocal rank fusion. One person's material is small, so the BM25 index is
built per query and there is no ANN index and no cache. Every query is scoped by user_id and
skips deleted sources."""

import logging
import threading
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, replace

import bm25s
import numpy as np
import psycopg
from psycopg_pool import ConnectionPool

from evowiki.retrieval.embed import Embedder
from evowiki.sources import store
from evowiki.sources.models import ChunkRow
from evowiki.store.db import fetch_all


@dataclass(frozen=True)
class SourceHit:
    chunk_id: str
    source_id: str
    section_id: str
    page: int | None
    text: str
    score: float


# --- tokenization ---------------------------------------------------------------------------

_jieba = None
_jieba_lock = threading.Lock()


def _get_jieba():
    """Lazy: loading the dictionary costs about a second, paid on the first search only."""
    global _jieba
    if _jieba is None:
        with _jieba_lock:
            if _jieba is None:
                with warnings.catch_warnings():  # jieba's regexes trip SyntaxWarning on 3.12+
                    warnings.simplefilter("ignore", SyntaxWarning)
                    import jieba

                jieba.setLogLevel(logging.WARNING)
                jieba.initialize()
                _jieba = jieba
    return _jieba


def tokenize(text: str) -> list[str]:
    """jieba words, lower-cased; punctuation and whitespace dropped, single CJK characters kept."""
    out = []
    for tok in _get_jieba().lcut(text):
        t = tok.strip().lower()
        # Math symbols (∑, ≤, ...) are dropped on purpose; the vector path covers them.
        if t and any(ch.isalnum() for ch in t):
            out.append(t)
    return out


# --- the two retrievers ---------------------------------------------------------------------

def vector_hits(conn: psycopg.Connection, user_id: str, query_vec: Sequence[float], *,
                source_ids: list[str] | None, k: int, embedder: Embedder) -> list[SourceHit]:
    """Exact cosine among chunks embedded by this same (embedder_id, model_version, dim).
    Chunks whose vectors are not in yet (the import adds them after the source is usable) are
    left to BM25."""
    if len(query_vec) != embedder.dim:
        raise ValueError(f"query vector has {len(query_vec)} dimensions, "
                         f"embedder declares {embedder.dim}")
    if k <= 0 or (source_ids is not None and not source_ids):
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    rows = fetch_all(
        conn,
        "select c.chunk_id, c.source_id, c.section_id, c.page, c.text, "
        "1 - (c.embedding <=> %(q)s) as sim "
        "from source_chunk c join source s using (source_id) "
        "where s.user_id = %(uid)s and s.deleted_at is null "
        "and (%(sids)s::text[] is null or c.source_id = any(%(sids)s::text[])) "
        "and c.embedding is not null "
        "and c.embedder_id = %(eid)s and c.model_version = %(ver)s and c.dim = %(dim)s "
        "order by c.embedding <=> %(q)s, c.chunk_id limit %(k)s",
        {"q": q, "uid": user_id, "sids": source_ids, "eid": embedder.id,
         "ver": embedder.model_version, "dim": embedder.dim, "k": k})
    return [SourceHit(chunk_id=r["chunk_id"], source_id=r["source_id"],
                      section_id=r["section_id"], page=r["page"], text=r["text"],
                      score=float(r["sim"])) for r in rows]


def _index(corpus: list[list[str]]) -> bm25s.BM25 | None:
    if not any(corpus):  # bm25s cannot index a corpus with no tokens at all
        return None
    retriever = bm25s.BM25()
    retriever.index(corpus, show_progress=False)
    return retriever


def _ranked(chunks: Sequence[ChunkRow], retriever: bm25s.BM25, q: list[str],
            k: int) -> list[SourceHit]:
    scores = retriever.get_scores(q)
    order = sorted((i for i in range(len(chunks)) if scores[i] > 0),
                   key=lambda i: (-float(scores[i]), chunks[i].chunk_id))
    return [SourceHit(chunk_id=chunks[i].chunk_id, source_id=chunks[i].source_id,
                      section_id=chunks[i].section_id, page=chunks[i].page, text=chunks[i].text,
                      score=float(scores[i])) for i in order[:k]]


def bm25_rank(docs: Sequence[tuple[str, str]], query: str, *, k: int) -> list[tuple[str, float]]:
    """BM25 over (id, text) pairs, built fresh; zero-score docs are not hits; ties by id."""
    q = tokenize(query)
    if not docs or not q or k <= 0:
        return []
    retriever = _index([tokenize(t) for _, t in docs])
    if retriever is None:
        return []
    scores = retriever.get_scores(q)
    order = sorted((i for i in range(len(docs)) if scores[i] > 0),
                   key=lambda i: (-float(scores[i]), docs[i][0]))
    return [(docs[i][0], float(scores[i])) for i in order[:k]]


def bm25_hits(chunks: Sequence[ChunkRow], query: str, *, k: int) -> list[SourceHit]:
    """BM25 over the given chunks, built fresh. Chunks with zero score are not hits."""
    by_id = {c.chunk_id: c for c in chunks}
    return [SourceHit(chunk_id=i, source_id=by_id[i].source_id, section_id=by_id[i].section_id,
                      page=by_id[i].page, text=by_id[i].text, score=s)
            for i, s in bm25_rank([(c.chunk_id, c.text) for c in chunks], query, k=k)]


# --- fusion ---------------------------------------------------------------------------------

def rrf_ids(lists: Sequence[Sequence[str]], *, k: int, c: int = 60) -> list[tuple[str, float]]:
    """Reciprocal rank fusion over id lists: score = sum of 1/(c + rank), rank from 1.
    Ties break by id so the result is deterministic."""
    score: dict[str, float] = {}
    for ids in lists:
        for rank, i in enumerate(ids, start=1):
            score[i] = score.get(i, 0.0) + 1.0 / (c + rank)
    order = sorted(score, key=lambda i: (-score[i], i))
    return [(i, score[i]) for i in order[:k]]


def rrf(lists: Sequence[Sequence[SourceHit]], *, k: int, c: int = 60) -> list[SourceHit]:
    """`rrf_ids` by chunk_id; each hit keeps the first copy seen."""
    first: dict[str, SourceHit] = {}
    for hits in lists:
        for h in hits:
            first.setdefault(h.chunk_id, h)
    return [replace(first[i], score=s)
            for i, s in rrf_ids([[h.chunk_id for h in hits] for hits in lists], k=k, c=c)]


def search_sources(pool: ConnectionPool, user_id: str, query: str, *, embedder: Embedder,
                   source_ids: list[str] | None = None, k: int = 8) -> list[SourceHit]:
    """Hybrid search over this user's live sources (optionally only `source_ids`)."""
    if k <= 0:
        return []
    depth = max(k * 3, 20)  # each retriever looks deeper than k so fusion has room to work
    query_vec = embedder.embed_query(query)
    with pool.connection() as conn:
        live = {s.source_id for s in store.list_sources(conn, user_id)}
        wanted = sorted(live if source_ids is None else live & set(source_ids))
        if not wanted:
            return []
        chunks = store.chunks_of(conn, user_id, wanted)
        vec = vector_hits(conn, user_id, query_vec, source_ids=wanted, k=depth,
                          embedder=embedder)
    lex = bm25_hits(chunks, query, k=depth)
    return rrf([vec, lex], k=k)


def phrase_hits(chunks: Sequence[ChunkRow], phrases: Sequence[str], *, k: int) -> list[SourceHit]:
    """Lexical only (no vectors): the chunks that contain every token of at least one phrase (a
    page title, a concept name), ranked by that phrase's best BM25 score. Whether a material
    talks about something at all: a vector neighbour is always found, so it never counts here."""
    best: dict[str, SourceHit] = {}
    if not chunks or k <= 0:
        return []
    # the corpus is tokenized and indexed once, shared by every phrase
    corpus = [tokenize(c.text) for c in chunks]
    retriever = _index(corpus)
    if retriever is None:
        return []
    token_sets = [set(t) for t in corpus]
    index_of = {c.chunk_id: i for i, c in enumerate(chunks)}
    for phrase in phrases:
        q = tokenize(phrase)
        need = set(q)
        if not need:
            continue
        for h in _ranked(chunks, retriever, q, len(chunks)):
            if need <= token_sets[index_of[h.chunk_id]] and (
                    h.chunk_id not in best or h.score > best[h.chunk_id].score):
                best[h.chunk_id] = h
    return sorted(best.values(), key=lambda h: (-h.score, h.chunk_id))[:k]


def lexical_sources(pool: ConnectionPool, user_id: str, phrases: Sequence[str], *,
                    source_ids: list[str] | None = None, k: int = 20) -> list[SourceHit]:
    """`phrase_hits` over this user's live sources (optionally only `source_ids`)."""
    if k <= 0:
        return []
    with pool.connection() as conn:
        live = {s.source_id for s in store.list_sources(conn, user_id)}
        wanted = sorted(live if source_ids is None else live & set(source_ids))
        if not wanted:
            return []
        chunks = store.chunks_of(conn, user_id, wanted)
    return phrase_hits(chunks, phrases, k=k)
