"""GraphSaved / GroupsNamed (plan T2): reducer, replay from stored JSON equals the runtime state,
old logs (no graph events) still replay with an empty graph, and the pure sync helpers."""

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from evowiki.nodes import GraphChange, NodeGraph, NodeRec, Relation, member_key
from evowiki.wikiapp.events import (
    ConceptName,
    ConceptsNamed,
    GraphSaved,
    GroupName,
    GroupsNamed,
    WikiEvent,
    WikiEventBody,
)
from evowiki.wikiapp.graph_sync import graph_apply, names_sync
from evowiki.wikiapp.state import WikiState, apply_event, replay
from tests.wikiapp.test_state import script

TS = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
_n = iter(range(100_000))


def ev(body) -> WikiEvent:
    return WikiEvent(event_id=f"g{next(_n)}", ts=TS, body=body)


def graph() -> NodeGraph:
    nodes = [NodeRec(id="la", name="线性代数", x=0.1, y=0.2),
             NodeRec(id="c1", name="矩阵", definition="数排成的表", x=0.15, y=0.25),
             NodeRec(id="c2", name="矩阵乘法", x=0.2, y=0.2)]
    rels = [Relation(kind="part_of", src="c1", dst="la", source="curator"),
            Relation(kind="part_of", src="c2", dst="la", phrase="线性代数的一部分"),
            Relation(kind="prerequisite", src="c1", dst="c2", source="migrated"),
            Relation(kind="related", src="c2", dst="c1")]
    change = GraphChange(change_id="g_migrate_1", at=TS, kind="migrate", summary="3 个节点",
                         why="旧数据")
    return NodeGraph.of(nodes, rels, [change])


def stored(e: WikiEvent) -> WikiEvent:
    """What comes back from the wiki_event table (payload JSON)."""
    return WikiEvent.model_validate({"event_id": e.event_id, "ts": e.ts,
                                     "body": e.body.model_dump(mode="json")})


def run(events: list[WikiEvent]) -> WikiState:
    s = WikiState()
    for e in events:
        s = apply_event(s, e)
    return s


def test_graph_saved_and_groups_named_reduce_and_replay_equal():
    key = member_key(["c1", "c2", "la"])
    events = [*script(),
              ev(GraphSaved(graph=graph(), action="migrated")),
              ev(GroupsNamed(names=(GroupName(key=key, name="线代", summary="矩阵与运算"),))),
              ev(GroupsNamed(names=(GroupName(key=key, name="线性代数", summary="矩阵"),
                                    GroupName(key="k2", name="别的", summary="")))),
              ev(GraphSaved(graph=graph().updated(nodes=[graph().node("la")], relations=()),
                            action="user_edit"))]
    runtime = run(events)
    assert runtime.graph.nodes == (NodeRec(id="la", name="线性代数", x=0.1, y=0.2),)
    assert runtime.groups[key].name == "线性代数" and set(runtime.groups) == {key, "k2"}
    assert replay(stored(e) for e in events).to_json() == runtime.to_json()
    mid = run(events[:-1])
    assert mid.graph == graph() and replay(stored(e) for e in events[:-1]).to_json() == \
        mid.to_json()


def test_old_logs_replay_with_an_empty_graph():
    old = replay(stored(e) for e in script())
    assert old.graph == NodeGraph() and old.groups == {}
    assert old.to_json() == run(script()).to_json()
    assert '"graph":{"changes":[],"nodes":[],"relations":[]}' in old.to_json()


def test_bodies_are_in_the_union_and_validate_the_graph():
    ta = TypeAdapter(WikiEventBody)
    body = GraphSaved(graph=graph(), action="split").model_dump(mode="json")
    assert ta.validate_python(body) == GraphSaved(graph=graph(), action="split")
    assert isinstance(ta.validate_python({"type": "groups_named", "names": []}), GroupsNamed)
    cyc = {**body, "graph": {**body["graph"], "relations": [
        *body["graph"]["relations"], {"kind": "prerequisite", "src": "c2", "dst": "c1"}]}}
    with pytest.raises(ValidationError, match="prerequisite_cycle"):
        ta.validate_python(cyc)
    with pytest.raises(ValidationError):
        ta.validate_python({**body, "action": "nope"})


def test_graph_apply_places_new_nodes_and_keeps_old_positions():
    s = run([ev(GraphSaved(graph=graph(), action="migrated"))])
    lost = NodeRec(id="c1", name="矩阵")                        # position dropped by a caller
    new = graph().updated(nodes=(*[r for r in graph().nodes if r.id != "c1"], lost,
                                 NodeRec(id="c3", name="行列式")))
    body = graph_apply(s, new, "user_edit")
    assert body.action == "user_edit"
    got = {r.id: (r.x, r.y) for r in body.graph.nodes}
    assert got["c1"] == (0.15, 0.25) and got["la"] == (0.1, 0.2)
    assert all(v is not None for xy in got.values() for v in xy)


def test_names_sync_only_for_k_concepts_whose_name_differs():
    s = run([ev(ConceptsNamed(names=(ConceptName(concept_id="c1", name="矩阵"),
                                     ConceptName(concept_id="c2", name="乘法",
                                                 subitems=()),)))])
    body = names_sync(s, graph(), {"c1", "c2", "la"})
    assert body == ConceptsNamed(names=(ConceptName(concept_id="c2", name="矩阵乘法"),
                                        ConceptName(concept_id="la", name="线性代数")))
    assert names_sync(s, graph(), {"c1"}) is None
