from collections.abc import Iterable, Sequence
from itertools import pairwise

import networkx as nx

from evowiki.content.models import PUBLISHABLE, Edge, PrereqKind, Relation


class EdgeView:
    """Read-only queries over the publishable subset of a (possibly in-progress) edge list."""

    def __init__(self, edges: Iterable[Edge]):
        self._prereq = nx.DiGraph()
        self._assoc = nx.Graph()
        self._related: set[tuple[str, str]] = set()
        for e in edges:
            if e.review_status not in PUBLISHABLE:
                continue
            if e.relation is Relation.PREREQUISITE_OF:
                self._prereq.add_edge(e.src, e.dst, kind=e.kind)
                self._assoc.add_edge(e.src, e.dst)
            elif e.relation is Relation.RELATED_TO:
                self._assoc.add_edge(e.src, e.dst)
                self._related.add(tuple(sorted((e.src, e.dst))))

    def direct_prereqs(self, cid: str, kind: PrereqKind | None = None) -> set[str]:
        if cid not in self._prereq:
            return set()
        return {p for p in self._prereq.predecessors(cid)
                if kind is None or self._prereq.edges[p, cid]["kind"] is kind}

    def prereq_closure(self, cids: Iterable[str], max_hops: int,
                       kind: PrereqKind | None = None) -> set[str]:
        start = set(cids)
        frontier, seen = set(start), set()
        for _ in range(max_hops):
            nxt = {p for c in frontier for p in self.direct_prereqs(c, kind)} - seen - start
            seen |= nxt
            frontier = nxt
        return seen

    def adjacent(self, a: str, b: str) -> bool:
        return self._assoc.has_edge(a, b)

    def path_valid(self, path: Sequence[str]) -> bool:
        if not 2 <= len(path) <= 4 or len(set(path)) != len(path):
            return False
        return all(self.adjacent(a, b) for a, b in pairwise(path))

    def related_pairs(self) -> list[tuple[str, str]]:
        return sorted(self._related)
