import pytest
from pydantic import ValidationError

from evowiki.nodes import GraphError, NodeGraph, NodeRec, Relation, validate


def n(i: str, **kw) -> NodeRec:
    return NodeRec(id=i, name=i.upper(), **kw)


def g(*rels: tuple[str, str, str], nodes: str = "abcdef") -> NodeGraph:
    return NodeGraph.of([n(i) for i in nodes], [Relation(kind=k, src=s, dst=d) for k, s, d in rels])


def test_multi_parent_and_lookups():
    graph = g(
        ("part_of", "c", "a"),
        ("part_of", "c", "b"),
        ("part_of", "d", "c"),
        ("prerequisite", "e", "c"),
    )
    assert set(graph.parents("c")) == {"a", "b"}
    assert graph.children("a") == ("c",)
    assert graph.prereqs("c") == ("e",)
    assert [(r.src, r.dst) for r in graph.prereq_edges()] == [("e", "c")]
    assert set(graph.descendants("a")) == {"c", "d"}
    assert set(graph.ancestors("d")) == {"a", "b", "c"}
    assert graph.has_children("c") and not graph.has_children("d")
    assert graph.node("a").name == "A" and graph.node("zz") is None


def test_descendants_no_duplicates_diamond():
    graph = g(
        ("part_of", "b", "a"), ("part_of", "c", "a"), ("part_of", "d", "b"), ("part_of", "d", "c")
    )
    assert sorted(graph.descendants("a")) == ["b", "c", "d"]


@pytest.mark.parametrize(
    ("rels", "code"),
    [
        ([("part_of", "a", "zz")], "unknown_id"),
        ([("part_of", "a", "b"), ("part_of", "b", "a")], "part_of_cycle"),
        (
            [("prerequisite", "a", "b"), ("prerequisite", "b", "c"), ("prerequisite", "c", "a")],
            "prerequisite_cycle",
        ),
        ([("related", "a", "a")], "self_loop"),
    ],
)
def test_refusals(rels, code):
    with pytest.raises(GraphError) as e:
        g(*rels)
    assert e.value.code == code


def test_mixed_kinds_are_not_a_cycle():
    g(
        ("part_of", "a", "b"),
        ("prerequisite", "b", "a"),
        ("kind_of", "a", "b"),
        ("kind_of", "b", "a"),
    )


def test_empty_and_duplicate():
    with pytest.raises(GraphError) as e:
        NodeGraph.of([NodeRec(id="a", name=" ")], [])
    assert e.value.code == "empty"
    with pytest.raises(GraphError) as e:
        NodeGraph.of([NodeRec(id="", name="x")], [])
    assert e.value.code == "empty"
    with pytest.raises(GraphError) as e:
        NodeGraph.of([n("a"), n("a")], [])
    assert e.value.code == "duplicate_node"


def test_stored_graph_can_never_be_invalid():
    with pytest.raises(ValidationError):
        NodeGraph(nodes=(n("a"),), relations=(Relation(kind="part_of", src="a", dst="b"),))
    with pytest.raises(GraphError):
        validate(NodeGraph.model_construct(nodes=(n("a"), n("a")), relations=(), changes=()))


def test_related_is_unordered():
    r1 = Relation(kind="related", src="b", dst="a")
    assert (r1.src, r1.dst) == ("a", "b")
    graph = g().with_relation(Relation(kind="related", src="a", dst="b"))
    graph = graph.with_relation(r1)
    assert len(graph.relations) == 1


def test_with_relation_dedupe_and_phrase():
    graph = g().with_relation(Relation(kind="prerequisite", src="a", dst="b"))
    graph = graph.with_relation(Relation(kind="prerequisite", src="a", dst="b", phrase="p1"))
    assert graph.relations[0].phrase == "p1"
    graph = graph.with_relation(Relation(kind="prerequisite", src="a", dst="b", phrase="p2"))
    assert graph.relations[0].phrase == "p1" and len(graph.relations) == 1
    with pytest.raises(GraphError) as e:
        graph.with_relation(Relation(kind="prerequisite", src="b", dst="a"))
    assert e.value.code == "prerequisite_cycle"


def test_with_node_keeps_position():
    graph = NodeGraph.of([n("a", x=0.1, y=0.2)], [])
    graph = graph.with_node(NodeRec(id="a", name="Alpha"))
    assert (graph.node("a").x, graph.node("a").y, graph.node("a").name) == (0.1, 0.2, "Alpha")
    graph = graph.with_node(NodeRec(id="a", name="Alpha", x=0.5, y=0.5))
    assert graph.node("a").x == 0.5
    graph = graph.with_node(n("b"))
    assert [r.id for r in graph.nodes] == ["a", "b"]
    with pytest.raises(GraphError):
        graph.with_node(NodeRec(id="c", name=""))


def test_no_limits_on_parents_children_depth():
    ids = [f"n{i}" for i in range(40)]
    graph = NodeGraph.of([n(i) for i in ids], [])
    for i in range(1, 40):
        graph = graph.with_relation(Relation(kind="part_of", src=ids[i], dst=ids[i - 1]))
        graph = (
            graph.with_relation(Relation(kind="part_of", src=ids[i], dst=ids[0]))
            if i > 1
            else graph
        )
    assert len(graph.descendants("n0")) == 39
    assert len(graph.ancestors("n39")) == 39


def test_roundtrip():
    graph = g(("part_of", "c", "a"), ("related", "d", "b"))
    assert NodeGraph.model_validate(graph.model_dump()) == graph


def test_relation_layers_recommended_and_verified():
    """Relation layers (2026-10-14): `recommended` is its own kind (never a cycle refusal: it is
    only an ordering hint); every relation carries `verified` (default False)."""
    graph = NodeGraph.of([n(i) for i in "abc"], [
        Relation(kind="prerequisite", src="a", dst="b", verified=True),
        Relation(kind="prerequisite", src="c", dst="b"),
        Relation(kind="recommended", src="b", dst="a"),
        Relation(kind="recommended", src="a", dst="b"),
    ])
    assert graph.prereqs("b") == ("a", "c")
    assert graph.verified_prereqs("b") == ("a",)
    assert graph.trusted_pairs() == frozenset({("a", "b")})
    assert Relation(kind="part_of", src="a", dst="b").verified is False
    assert Relation(kind="prerequisite", src="a", dst="b", source="expert").source == "expert"


def test_old_snapshot_without_verified_loads_unverified():
    old = {"nodes": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
           "relations": [{"kind": "prerequisite", "src": "a", "dst": "b", "phrase": "p",
                          "source": "migrated"}]}
    graph = NodeGraph.model_validate(old)
    assert graph.relations[0].verified is False and graph.trusted_pairs() == frozenset()


def test_dedupe_never_loses_verification():
    v = Relation(kind="prerequisite", src="a", dst="b", verified=True)
    u = Relation(kind="prerequisite", src="a", dst="b", phrase="why")
    graph = NodeGraph.of([n(i) for i in "ab"], [v]).with_relation(u)
    (r,) = graph.relations
    assert r.verified and r.phrase == "why"
    graph = NodeGraph.of([n(i) for i in "ab"], [u]).with_relation(v)
    (r,) = graph.relations
    assert r.verified and r.phrase == "why"
