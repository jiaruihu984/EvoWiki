"""Which existing nodes a decomposition call is shown: the prompt's 「知识图谱里已经有这些节点」 rows
list what is related to this goal, not the whole wiki (bug 2026-11-03: a `python` goal listing eleven
percolation nodes with their evidence came back as seven percolation blocks). What the code knows
is unchanged: ids, `existing_id` and the same-name dedupe still see every node, only the rows shrink.

Related = the goal's words retrieved over each node's text: the meaning (cosine, the service's
embedder) or his words naming it (BM25 over the names, `sources.search.bm25_rank`), plus one hop of the graph around a
hit (its parents and parts: a block and its points come together), plus the other blocks of a goal one
block of it hit, with their parts (a short name against a goal word sits at the threshold either way),
plus `keep` — what the call must see whatever the words say (entering a block: its goal's map and the
block's own neighbours).

RELATED_COSINE is a retrieval setting measured on paraphrase-multilingual-mpnet-base-v2, not a
content rule, set low on purpose: a missed row loses evidence and makes a duplicate node, a wrong
extra row costs little, since the model still decides whether to reuse it
(python → Python 基础语法 0.82, the percolation nodes at most 0.18; 复杂网络的级联失效 → 巨分量 0.68,
无标度网络 0.67, 定向与相依扩展 0.45, 攻击与稳健性 0.44, but 巨分量与阈值 0.26)."""

import math
from collections.abc import Collection, Sequence

from evowiki.goals.intake import IntakeScope
from evowiki.goals.regions import GoalMap, MapNode
from evowiki.retrieval.embed import Embedder, embedder_from_env
from evowiki.sources.search import bm25_rank, tokenize
from evowiki.wikiapp.state import WikiState

RELATED_COSINE = 0.4


def _unit(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _node_texts(state: WikiState) -> dict[str, str]:
    """Every node with what it is about: its name, definition and the names of its parents."""
    g = state.graph
    out = {c: n.name for c, n in state.names.items()}
    for rec in g.nodes:
        ups = [p.name for i in g.parents(rec.id) if (p := g.node(i)) is not None]
        out[rec.id] = " ".join(x for x in (rec.name, rec.definition, *ups) if x)
    return out


def related_nodes(state: WikiState, query: str, *, words: Sequence[str] = (),
                  keep: Collection[str] = (), embedder: Embedder | None = None) -> frozenset[str]:
    """The node ids a call about `query` is shown (module doc). `words`: what he said besides,
    matched by the same words only (a sentence about himself says little by its meaning). An
    embedder error propagates (fail closed)."""
    texts = _node_texts(state)
    if not texts:
        return frozenset(keep)
    ids = list(texts)
    emb = embedder if embedder is not None else embedder_from_env()
    q = _unit(emb.embed_query(query))
    vectors = emb.embed_documents([texts[i] for i in ids])
    hits = {i for i, v in zip(ids, vectors, strict=True)
            if sum(a * b for a, b in zip(q, _unit(v), strict=True)) >= RELATED_COSINE}
    # he named the node: over names only, in words of two characters or more (「是什么」 in a
    # definition, 与 in a name would match nearly every node)
    g = state.graph
    names = [(i, rec.name if (rec := g.node(i)) is not None else state.names[i].name) for i in ids]
    terms = " ".join(t for t in tokenize(" ".join((query, *words))) if len(t) > 1)
    hits |= {i for i, _ in bm25_rank(names, terms, k=len(ids))}
    out = set(hits) | set(keep)
    for i in hits:
        if g.node(i) is not None:
            out |= {*g.parents(i), *g.children(i)}
    for m in state.maps.values():       # a map with a hit: its blocks come together, whatever each one's
        tops = {n.id for n in m.nodes if n.parent is None}  # own cosine (黑壳虾: 日常照顾 0.39, 繁殖与育苗 0.40)
        if tops & hits:
            out |= tops
            for i in tops:
                if g.node(i) is not None:
                    out |= set(g.children(i))
    return frozenset(out)


def entered_nodes(state: WikiState, m: GoalMap | None, node_id: str, scope: IntakeScope,
                  embedder: Embedder | None = None) -> frozenset[str]:
    """What a split of `node_id` is shown: this goal's own nodes (the map's, none off any map), the
    node's place in the graph, and what is related to it by `related_nodes` — another goal's nodes
    too. The query is the goal with the node and what it covers."""
    g = state.graph
    around = ((*g.ancestors(node_id), *g.children(node_id)) if g.node(node_id) is not None else ())
    own: tuple[MapNode, ...] = m.nodes if m is not None else ()
    query = " ".join(x for x in (scope.parent_goal, scope.region_name, scope.scope) if x)
    return related_nodes(state, query, keep={node_id, *around, *(n.id for n in own)},
                         embedder=embedder)
