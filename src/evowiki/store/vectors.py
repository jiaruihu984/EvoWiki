import numpy as np
import psycopg

from evowiki.content.loaded import LoadedContent
from evowiki.retrieval.embed import Embedder
from evowiki.schema import Lang, Segment
from evowiki.store.db import fetch_all


def segment_text(content: LoadedContent, seg: Segment) -> str:
    c = content.graph.concept(seg.concept_id)
    name = c.name_zh if seg.lang is Lang.ZH else c.name_en
    return f"{name}: {seg.body}"


def _vec(v: list[float], dim: int) -> np.ndarray:
    if len(v) != dim:
        raise ValueError(f"embedding has {len(v)} dimensions, model declares {dim}")
    return np.asarray(v, dtype=np.float32)


def index_segments(conn: psycopg.Connection, content: LoadedContent, embedder: Embedder, *,
                   batch: int = 64) -> int:
    """Embed every segment (en and zh) lacking a row for this exact model."""
    eid, ver = embedder.id, embedder.model_version
    done = {r["segment_id"] for r in fetch_all(
        conn, "select segment_id from segment_embedding where content_version = %s "
              "and embedder_id = %s and model_version = %s", (content.version_id, eid, ver))}
    todo = [s for s in content.graph.segments if s.id not in done]
    with conn.transaction():
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            vecs = embedder.embed_documents([segment_text(content, s) for s in chunk])
            for s, v in zip(chunk, vecs, strict=True):
                conn.execute(
                    "insert into segment_embedding (content_version, embedder_id, "
                    "model_version, dim, segment_id, concept_id, kind, lang, embedding) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (content.version_id, eid, ver, embedder.dim, s.id, s.concept_id,
                     s.kind.value, s.lang.value, _vec(v, embedder.dim)))
    return len(todo)


def search_segments(conn: psycopg.Connection, content_version: str, embedder: Embedder,
                    query: str, *, k: int, lang: Lang | None = None) -> list[tuple[str, float]]:
    """Cosine search among rows of the same (embedder_id, model_version) only."""
    q = _vec(embedder.embed_query(query), embedder.dim)
    code = lang.value if lang else None
    rows = fetch_all(
        conn,
        "select segment_id, 1 - (embedding <=> %(q)s) as sim from segment_embedding "
        "where content_version = %(cv)s and embedder_id = %(eid)s and model_version = %(ver)s "
        "and dim = %(dim)s and (%(lang)s::text is null or lang = %(lang)s) "
        "order by embedding <=> %(q)s, segment_id limit %(k)s",
        {"q": q, "cv": content_version, "eid": embedder.id, "ver": embedder.model_version,
         "dim": embedder.dim, "lang": code, "k": k})
    return [(r["segment_id"], float(r["sim"])) for r in rows]
