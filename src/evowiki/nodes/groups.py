"""Automatic groups (spec §4, after GraphRAG's hierarchical communities): the far and middle zoom
levels show groups found from the graph's connections, not hand-made regions.

Hierarchical Louvain (networkx, fixed seed) over all relations treated as undirected, part_of
weighted 3 (containment is the strongest tie), the other kinds 1. At most two levels: level 1 is the
finest partition that is not all singletons, level 2 the coarsest. Isolated nodes (no relation)
share one "loose" group per level, so every node is in exactly one group per level. A group's id
comes from its member set (`member_key`), so names cached by it (event `GroupsNamed`) survive as
long as the members do. Deterministic: the same graph gives the same groups."""

import hashlib
from collections.abc import Iterable

import networkx as nx
from pydantic import BaseModel, ConfigDict

from evowiki.nodes.model import NodeGraph

WEIGHTS = {"part_of": 3.0, "prerequisite": 1.0, "recommended": 1.0, "related": 1.0,
           "kind_of": 1.0}
SEED = 20261013


class Group(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    level: int
    members: tuple[str, ...]
    parent: str | None = None


def member_key(members: Iterable[str]) -> str:
    """First 12 hex of sha256 over the sorted member ids."""
    return hashlib.sha256("\n".join(sorted(members)).encode()).hexdigest()[:12]


def _undirected(graph: NodeGraph) -> nx.Graph:
    g = nx.Graph()
    g.add_nodes_from(sorted(r.id for r in graph.nodes))
    for rel in graph.relations:  # already sorted
        w = WEIGHTS[rel.kind]
        if g.has_edge(rel.src, rel.dst):
            g[rel.src][rel.dst]["weight"] += w
        else:
            g.add_edge(rel.src, rel.dst, weight=w)
    return g


def _canon(partition: Iterable[Iterable[str]], isolated: set[str]) -> list[tuple[str, ...]]:
    """Sorted member tuples; isolated singletons pooled into one loose group."""
    parts: list[tuple[str, ...]] = []
    loose: list[str] = []
    for c in partition:
        members = sorted(c)
        if len(members) == 1 and members[0] in isolated:
            loose.append(members[0])
        else:
            parts.append(tuple(members))
    if loose:
        parts.append(tuple(sorted(loose)))
    return sorted(parts)


def cluster(graph: NodeGraph) -> tuple[Group, ...]:
    """Groups at level 1 (fine) and, when different and the graph has >= 4 nodes, level 2
    (coarse). Each level-1 group's parent is the level-2 group holding most of its members."""
    if not graph.nodes:
        return ()
    g = _undirected(graph)
    isolated = {n for n in g.nodes if g.degree(n) == 0}
    raw = list(nx.community.louvain_partitions(g, weight="weight", seed=SEED))
    if not raw:
        raw = [[{n} for n in g.nodes]]
    levels = [_canon(p, isolated) for p in raw]
    fine = next((p for p in levels if any(len(m) > 1 for m in p)), levels[0])
    coarse = levels[-1]
    if fine == coarse or len(graph.nodes) < 4:
        return tuple(Group(id=f"g1-{member_key(m)}", level=1, members=m) for m in fine)
    top = [Group(id=f"g2-{member_key(m)}", level=2, members=m) for m in coarse]
    out: list[Group] = []
    for m in fine:
        counts = [(-len(set(m) & set(t.members)), t.id) for t in top]
        out.append(Group(id=f"g1-{member_key(m)}", level=1, members=m, parent=min(counts)[1]))
    return (*out, *top)
