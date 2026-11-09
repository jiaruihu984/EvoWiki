"""Random valid operations keep the graph invariant, and every undo restores the graph exactly."""

import random
from datetime import UTC, datetime

import pytest

from evowiki.nodes import (
    GraphError,
    NodeGraph,
    NodeRec,
    Relation,
    UndoConflict,
    split,
    undo,
    validate,
)
from evowiki.nodes.groups import cluster
from evowiki.nodes.place import place_missing

AT = datetime(2026, 10, 13, tzinfo=UTC)
KINDS = ("part_of", "prerequisite", "kind_of", "related")


def core(graph):
    d = graph.model_dump()
    return d["nodes"], d["relations"]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_operations_keep_invariant(seed):
    rng = random.Random(seed)
    graph = NodeGraph()
    counter = 0
    history: list[str] = []
    undone_exact = 0
    for _ in range(300):
        ids = [r.id for r in graph.nodes]
        op = rng.choice(["node", "rel", "rel", "split", "undo"])
        try:
            if op == "node" or len(ids) < 3:
                counter += 1
                graph = graph.with_node(NodeRec(id=f"n{counter}", name=f"N{counter}"))
            elif op == "rel":
                graph = graph.with_relation(
                    Relation(
                        kind=rng.choice(KINDS),
                        src=rng.choice(ids),
                        dst=rng.choice(ids),
                        phrase=rng.choice(["", "p"]),
                    )
                )
            elif op == "split":
                counter += 1
                kids = [NodeRec(id=f"n{counter}_{k}", name="k") for k in range(rng.randint(1, 3))]
                links = (
                    [Relation(kind=rng.choice(KINDS[1:]), src=kids[0].id, dst=kids[-1].id)]
                    if len(kids) > 1
                    else []
                )
                before = core(graph)
                graph = split(
                    graph,
                    rng.choice(ids),
                    kids,
                    links,
                    change_id=f"c{counter}",
                    at=AT,
                    why="",
                    summary="",
                )
                history.append(f"c{counter}")
                if rng.random() < 0.3:
                    graph = undo(graph, history.pop())
                    assert core(graph) == before
                    undone_exact += 1
            elif op == "undo" and history:
                graph = undo(graph, history.pop(rng.randrange(len(history))))
        except (GraphError, UndoConflict):
            pass
        validate(graph)
        assert NodeGraph.model_validate(graph.model_dump()) == graph
    assert len(graph.nodes) > 10 and undone_exact > 3
    placed = place_missing(graph)
    assert placed == place_missing(graph)
    assert all(r.x is not None for r in placed.nodes)
    assert cluster(graph) == cluster(graph)
