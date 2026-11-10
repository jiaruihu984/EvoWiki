"""Pure helpers that turn a new node graph into wiki events (spec 2026-10-13, plans T2 / T4).

- `graph_apply`: the `GraphSaved` body for a new graph (every node placed first: positions are
  assigned once, when a node first appears, and never move).
- `names_sync`: the `ConceptsNamed` body that keeps `WikiState.names` equal to the graph's names
  for nodes that are K concepts (the existing code reads concept names from there).
- `add_relations`: relations added in bulk; what the program cannot handle (unknown id, self-loop,
  a part_of / prerequisite cycle) is skipped and described, never raised.
- `graph_events`: a new graph as the events of one commit — K first (`nodes.ksync`: every node a K
  concept, every verified prerequisite a K prereq), then the names, then `GraphSaved`.
No IO."""

import logging
from collections.abc import Callable, Collection, Iterable, Mapping

import networkx as nx

from evowiki.belief.aspects import AspectK, DefineConcept
from evowiki.goals.contracts import Aspect
from evowiki.nodes.ksync import ksync_events
from evowiki.nodes.model import NodeGraph, Relation
from evowiki.nodes.place import LAYOUT_VERSION, place_missing
from evowiki.wikiapp.events import (
    ConceptName,
    ConceptsNamed,
    GraphAction,
    GraphSaved,
    WikiEvent,
)
from evowiki.wikiapp.split_layout import place_split_children
from evowiki.wikiapp.state import WikiState

log = logging.getLogger(__name__)
Ids = Callable[[str], str]


def graph_apply(state: WikiState, graph: NodeGraph, action: GraphAction) -> GraphSaved:
    """The event body that makes `graph` the user's graph. Unplaced nodes get their position now;
    a node already in `state.graph` keeps its position even if `graph` lost it. When no node had a
    position yet (a first graph), the whole layout is the current one (`layout` = LAYOUT_VERSION)."""
    old = {r.id: r for r in state.graph.nodes if r.x is not None and r.y is not None}
    kept = [r.model_copy(update={"x": old[r.id].x, "y": old[r.id].y})
            if r.id in old and r.x is None and r.y is None else r for r in graph.nodes]
    fresh = not old and all(r.x is None and r.y is None for r in kept)
    grown = place_split_children(graph.updated(nodes=kept), state.maps.values(),
                                 {r.id for r in state.graph.nodes})
    return GraphSaved(graph=place_missing(grown), action=action,
                      layout=LAYOUT_VERSION if fresh and kept else 0)


def names_sync(state: WikiState, graph: NodeGraph,
               concept_ids: Collection[str]) -> ConceptsNamed | None:
    """`ConceptsNamed` for the graph's nodes that are K concepts (`concept_ids`) whose recorded
    name is missing or differs (subitems and `atomic` kept); None when every name already matches."""
    ids = set(concept_ids)
    out = []
    for rec in graph.nodes:
        if rec.id not in ids:
            continue
        old = state.names.get(rec.id)
        if old is None or old.name != rec.name:
            out.append(ConceptName(concept_id=rec.id, name=rec.name,
                                   subitems=old.subitems if old is not None else (),
                                   atomic=old.atomic if old is not None else None))
    return ConceptsNamed(names=tuple(out)) if out else None


def add_relations(graph: NodeGraph,
                  rels: Iterable[Relation]) -> tuple[NodeGraph, list[str]]:
    """`graph` with `rels` added in order (deduped on (kind, src, dst): a relation already there
    stays, see `nodes.model.merge_relation`). A relation naming a node the graph lacks, a
    self-loop, or one that would close a part_of / prerequisite cycle is skipped; the second value
    describes each skipped one."""
    ids = {n.id for n in graph.nodes}
    dags = {kind: nx.DiGraph((r.src, r.dst) for r in graph.relations if r.kind == kind)
            for kind in ("part_of", "prerequisite")}
    out = list(graph.relations)
    skipped: list[str] = []
    for r in rels:
        why = None
        dag = dags.get(r.kind)
        if r.src not in ids or r.dst not in ids:
            why = "unknown_id"
        elif r.src == r.dst:
            why = "self_loop"
        elif dag is not None and dag.has_node(r.src) and dag.has_node(r.dst) \
                and nx.has_path(dag, r.dst, r.src):
            why = f"{r.kind}_cycle"
        if why is not None:
            skipped.append(f"{r.kind} {r.src}→{r.dst}（{why}）")
            continue
        if dag is not None:
            dag.add_edge(r.src, r.dst)
        out.append(r)
    return graph.updated(relations=out), skipped


def graph_events(state: WikiState, k: AspectK, graph: NodeGraph, action: GraphAction, *,
                 now, new_id: Ids, aspects: Mapping[str, tuple[Aspect, ...]] | None = None
                 ) -> tuple[list[DefineConcept], list[WikiEvent]]:
    """`graph` as the events of one commit on `state` / `k`: the K events that make every node a
    K concept and every verified prerequisite a K prereq, the names of nodes `state.names` lacks or names
    differently, and `GraphSaved(action)`. `aspects`: a new node's aspects (else term + purpose;
    ignored for nodes K already has)."""
    saved = graph_apply(state, graph, action)
    aspect = ksync_events(saved.graph, k, ts=now, new_id=new_id, aspects=aspects)
    known = set(k.concept_ids()) | {e.concept_id for e in aspect}
    wiki: list[WikiEvent] = []
    named = names_sync(state, saved.graph, known)
    if named is not None:
        wiki.append(WikiEvent(event_id=new_id("wev"), ts=now, body=named))
    wiki.append(WikiEvent(event_id=new_id("wev"), ts=now, body=saved))
    return aspect, wiki


def same_structure(a: NodeGraph, b: NodeGraph) -> bool:
    """Same nodes (ids and names) and relations; positions and the change log ignored."""
    return ([(n.id, n.name, n.definition) for n in a.nodes]
            == [(n.id, n.name, n.definition) for n in b.nodes] and a.relations == b.relations)
