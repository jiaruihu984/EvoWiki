from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from evowiki.content.graph import ContentGraph

DEFAULT_PHASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("LA", ("la", "prob")),
    ("ML", ("ml",)),
    ("DL", ("dl",)),
)


@dataclass(frozen=True)
class Phase:
    name: str
    concept_ids: tuple[str, ...]


@dataclass(frozen=True)
class Curriculum:
    phases: tuple[Phase, ...]

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(c for p in self.phases for c in p.concept_ids)

    def phase_of(self, concept_id: str) -> str:
        for p in self.phases:
            if concept_id in p.concept_ids:
                return p.name
        raise KeyError(concept_id)


def scripted_curriculum(
    graph: ContentGraph,
    phases: tuple[tuple[str, tuple[str, ...]], ...] = DEFAULT_PHASES,
    exclude: AbstractSet[str] = frozenset(),
) -> Curriculum:
    """LA → ML → DL script. Within a phase, concepts are ordered by prerequisite depth.

    Sorting by (size of prerequisite closure, difficulty, id) is a topological order: every
    prerequisite has a strictly smaller closure than the concepts that depend on it.
    """
    n = len(graph.concept_ids)
    depth = {c: len(graph.prerequisite_closure(c, max_hops=n)) for c in graph.concept_ids}
    seen: set[str] = set()
    out: list[Phase] = []
    for name, domains in phases:
        if seen & set(domains):
            raise ValueError(f"domain listed in two phases: {sorted(seen & set(domains))}")
        seen |= set(domains)
        ids = sorted(
            (c for c in graph.concept_ids
             if graph.concept(c).domain in domains and c not in exclude),
            key=lambda c: (depth[c], graph.concept(c).difficulty, c),
        )
        out.append(Phase(name, tuple(ids)))
    return Curriculum(tuple(out))
