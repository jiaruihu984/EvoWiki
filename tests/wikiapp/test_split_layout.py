"""A saved split and a freshly arranged split match the original learning picture."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from evowiki.goals.regions import GoalMap, MapNode
from evowiki.nodes.model import NodeGraph, NodeRec, Relation
from evowiki.nodes.place import LAYOUT_VERSION
from evowiki.wikiapp.graph_layout import arrange, ensure_layout
from evowiki.wikiapp.graph_sync import graph_apply
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.split_layout import split_members
from evowiki.wikiapp.state import WikiState

NOW = datetime(2026, 11, 10, tzinfo=UTC)
# Captured old spot() offsets, in concept-first display order, rather than graph id order.
OLD = [(-0.1032313374993762, 0.09456897460631518),
       (0.017308136372527805, -0.19723191530609338),
       (0.14754044743226868, 0.1924365255648884),
       (-0.27572040593299973, -0.04876738410189596),
       (0.26413447401145734, -0.1680267229897995),
       (-0.08901931390837897, 0.33117300879039263)]


def scene():
    parent = NodeRec(id="hub", name="Hub", x=2.0, y=3.0)
    children = [NodeRec(id=f"p{i}", name=f"Point {i}") for i in range(6)]
    m = GoalMap(map_id="m", goal="Goal", created_at=NOW, nodes=(
        MapNode(id="hub", kind="region", name="Hub", state="expanded"),
        *(MapNode(id=c.id, kind="region" if i == 4 else "concept", name=c.name, parent="hub")
          for i, c in enumerate(children))))
    before = NodeGraph.of([parent], [])
    grown = NodeGraph.of([parent, *children], [Relation(kind="part_of", src=c.id, dst="hub")
                                             for c in children])
    return WikiState(graph=before, maps={"m": m}, map_order=("m",), graph_layout=LAYOUT_VERSION), grown, m


def offsets(graph):
    hub = graph.node("hub")
    return [(graph.node(i).x - hub.x, graph.node(i).y - hub.y)
            for i in ["p0", "p1", "p2", "p3", "p5", "p4"]]


def test_new_children_save_the_old_picture_and_later_saves_do_not_move_it():
    state, graph, m = scene()
    assert split_members(m, m.node("hub")) == ["p0", "p1", "p2", "p3", "p5", "p4"]
    saved = graph_apply(state, graph, "split").graph
    for actual, expected in zip(offsets(saved), OLD, strict=True):
        assert actual == pytest.approx(expected)
    assert saved.node("hub") == state.graph.node("hub")
    # A repeat save, or a rename, preserves every world coordinate.
    state = state.model_copy(update={"graph": saved})
    assert graph_apply(state, saved, "user_edit").graph == saved


def test_one_more_child_keeps_old_positions_and_uses_the_next_free_old_slot():
    state, graph, m = scene()
    saved = graph_apply(state, graph, "split").graph
    extra = NodeRec(id="extra", name="Extra")
    m = m.replace(MapNode(id=extra.id, kind="region", name=extra.name, parent="hub"))
    state = state.model_copy(update={"graph": saved, "maps": {m.map_id: m}})
    grown = saved.updated(nodes=(*saved.nodes, extra), relations=(
        *saved.relations, Relation(kind="part_of", src=extra.id, dst="hub")))
    placed = graph_apply(state, grown, "split").graph
    assert all(placed.node(n.id) == n for n in saved.nodes)
    hub, child = placed.node("hub"), placed.node(extra.id)
    assert (child.x - hub.x, child.y - hub.y) == pytest.approx((-0.17072978267741346, -0.32871163853280766))


@pytest.mark.parametrize("fresh", [False, True])
def test_arrangement_keeps_the_hidden_hub_in_its_children_picture(fresh):
    state, graph, _ = scene()
    saved = graph_apply(state, graph, "split").graph
    state = state.model_copy(update={"graph": saved, "graph_arranged": ("goal-m",)})
    arranged, _ = arrange(state, fresh=fresh)
    for actual, expected in zip(offsets(arranged), OLD, strict=True):
        assert actual == pytest.approx(expected, abs=2e-6)
    assert len(arranged.nodes) == 7


def test_an_ordinary_graph_read_does_not_rearrange_a_saved_split():
    state, graph, _ = scene()
    saved = graph_apply(state, graph, "split").graph
    state = state.model_copy(update={"graph": saved, "graph_arranged": ("goal-m",)})
    repo = MemoryRepo(["u1"])
    snap = replace(repo.load("u1"), state=state)
    assert ensure_layout(repo, snap, now=NOW, new_id=lambda p: p) is snap


@pytest.mark.parametrize("entry", ["enter", "split"])
def test_real_split_entry_saves_concept_first_positions_before_saving_its_map(entry):
    from evowiki.wikiapp.events import (
        ConceptName,
        ConceptsNamed,
        GoalMapSaved,
        GraphSaved,
        WikiEvent,
    )
    from evowiki.wikiapp.goal import enter_region
    from evowiki.wikiapp.llm import require
    from evowiki.wikiapp.node_split import split_node
    from evowiki.wikiapp.repo import Commit
    from tests.wikiapp.graph_seed import MM, seed
    from tests.wikiapp.script import Script
    from tests.wikiapp.split_answers import PURPOSE

    repo, ids = seed()
    snap = repo.load("u1")
    # Keep unrelated library points far away, so this checks the exact original six slots.
    graph = snap.state.graph.updated(nodes=[n.model_copy(update={"x": 100.0, "y": 100.0})
                                            if n.id == MM else n for n in snap.state.graph.nodes])
    m = GoalMap(map_id="split-map", goal="Goal", created_at=NOW, nodes=(
        MapNode(id=MM, name=graph.node(MM).name, kind="region"),))
    bodies = [GraphSaved(graph=graph, action="user_edit"), GoalMapSaved(record=m, action="planned"),
              ConceptsNamed(names=(ConceptName(concept_id=MM, name=graph.node(MM).name, atomic=False),))]
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=b) for b in bodies]))
    script = Script().push(PURPOSE, {"parts": [
        {"name": f"Leaf {i}", "summary": f"Item {i}", "atomic": i != 4} for i in range(6)]})
    gateway = require(script.gateway())
    if entry == "split":
        split_node(repo, gateway, "u1", MM, reason="requested", source="chat",
                   map_id=m.map_id, now=NOW, new_id=ids)
    else:
        enter_region(repo, gateway, "u1", m.map_id, MM, now=NOW, new_id=ids)
    state = repo.load("u1").state
    m = state.maps[m.map_id]
    children = [n for n in m.nodes if n.parent == MM]
    assert [n.kind for n in children] == ["concept"] * 4 + ["region", "concept"]
    parent = state.graph.node(MM)
    for child_id, expected in zip(split_members(m, m.node(MM)), OLD, strict=True):
        child = state.graph.node(child_id)
        assert (child.x - parent.x, child.y - parent.y) == pytest.approx(expected)
