"""Vector search over forged-tool descriptions (Voyager-style skill retrieval).

Tools are few (tens), so search is brute-force cosine over `forge_tool_vec` rows of one
`model_version` (no pgvector index; tags are never mixed, decision 11)."""

import math
from collections.abc import Iterable, Mapping

import psycopg

from evowiki.forge.registry import ForgedTool
from evowiki.retrieval.embed import Embedder
from evowiki.store import forge as store


def doc_text(tool: ForgedTool, title: str) -> str:
    """title + schema description + argument names/descriptions: what a model would match on."""
    schema = tool.schema
    parts = [title, str(schema.get("description", ""))]
    props = schema.get("properties")
    if isinstance(props, Mapping):
        for arg, spec in props.items():
            desc = spec.get("description", "") if isinstance(spec, Mapping) else ""
            parts.append(f"{arg} {desc}".strip())
    return "\n".join(p for p in parts if p)


def _title(tool: ForgedTool) -> str:
    return str(tool.schema.get("title") or tool.name)


def ensure(conn: psycopg.Connection, embedder: Embedder,
           tools: Iterable[tuple[ForgedTool, str]]) -> None:
    """Embed any tool whose candidate has no vector for embedder.model_version (idempotent)."""
    items = list(tools)
    have = store.vecs(conn, [t.candidate_id for t, _ in items],
                      model_version=embedder.model_version)
    todo = [(t, title) for t, title in items if t.candidate_id not in have]
    if not todo:
        return
    vectors = embedder.embed_documents([doc_text(t, title) for t, title in todo])
    for (t, _), vec in zip(todo, vectors, strict=True):
        store.put_vec(conn, t.candidate_id, embedder=embedder.id,
                      model_version=embedder.model_version, vec=vec)


def _cos(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


def search(conn: psycopg.Connection, embedder: Embedder, query: str,
           tools: Mapping[str, ForgedTool], k: int) -> list[tuple[str, float]]:
    """Names of `tools` ranked by cosine similarity to `query`, best first, ties by name.
    Empty query -> name order with score 0.0. Tools without a vector are embedded first (titled
    by the schema's `title`, else the name)."""
    if k <= 0:
        return []
    if not query.strip():
        return [(n, 0.0) for n in sorted(tools)[:k]]
    ensure(conn, embedder, [(t, _title(t)) for t in tools.values()])
    vmap = store.vecs(conn, [t.candidate_id for t in tools.values()],
                      model_version=embedder.model_version)
    q = embedder.embed_query(query)
    scored = [(n, _cos(q, vmap[t.candidate_id])) for n, t in tools.items()]
    scored.sort(key=lambda s: (-s[1], s[0]))
    return scored[:k]


def nearest(conn: psycopg.Connection, embedder: Embedder, text: str,
            tools: Mapping[str, ForgedTool]) -> tuple[str, float] | None:
    """The single best match, for reuse-before-forge (limits.REUSE_SIM)."""
    got = search(conn, embedder, text, tools, 1)
    return got[0] if got else None
