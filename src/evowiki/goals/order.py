"""Concept order on the page (design D6): lexicographic transitive prerequisite order (a prerequisite
precedes its dependents), ties broken by (K rank, difficulty, id) so known material and easier
concepts come first. The organizer fallback uses it over the page's own prerequisite lists."""

from collections.abc import Callable, Iterable

import networkx as nx

from evowiki.goals.models import KStatus

_K_RANK = {KStatus.MASTERED: 0, KStatus.LIKELY: 1, KStatus.PARTIAL: 2, KStatus.UNKNOWN: 3}


def order_by(statuses: dict[str, KStatus], descendants: Callable[[str], Iterable[str]],
             difficulty: Callable[[str], int]) -> tuple[str, ...]:
    dag = nx.DiGraph()
    dag.add_nodes_from(statuses)
    for a in sorted(statuses):
        dag.add_edges_from((a, b) for b in sorted(descendants(a)) if b in statuses)
    return tuple(nx.lexicographical_topological_sort(
        dag, key=lambda c: (_K_RANK[statuses[c]], difficulty(c), c)))
