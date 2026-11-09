from evowiki.nodes import NodeGraph, NodeRec, Relation
from evowiki.nodes.groups import cluster, member_key


def _graph(ids, rels) -> NodeGraph:
    return NodeGraph.of(
        [NodeRec(id=i, name=i) for i in ids], [Relation(kind=k, src=s, dst=d) for k, s, d in rels]
    )


def _two_blobs():
    ids = [f"a{i}" for i in range(6)] + [f"b{i}" for i in range(6)] + ["iso1", "iso2"]
    rels = [("part_of", f"a{i}", "a0") for i in range(1, 6)]
    rels += [("part_of", f"b{i}", "b0") for i in range(1, 6)]
    rels += [("related", "a1", "a2"), ("prerequisite", "b1", "b2"), ("related", "a5", "b5")]
    return _graph(ids, rels)


def test_member_key_order_free():
    assert member_key(["b", "a"]) == member_key(["a", "b"])
    assert len(member_key(["a"])) == 12


def test_every_node_once_per_level_and_nesting():
    graph = _two_blobs()
    groups = cluster(graph)
    levels = sorted({gr.level for gr in groups})
    assert levels[0] == 1 and len(levels) <= 2
    for lv in levels:
        members = [m for gr in groups if gr.level == lv for m in gr.members]
        assert sorted(members) == sorted(r.id for r in graph.nodes)
    for gr in groups:
        assert gr.members == tuple(sorted(gr.members))
        assert gr.id == f"g{gr.level}-{member_key(gr.members)}"
        if gr.level == 1 and len(levels) == 2:
            assert gr.parent in {p.id for p in groups if p.level == 2}
        else:
            assert gr.parent is None
    # the isolated nodes share one loose group
    loose = [gr for gr in groups if gr.level == 1 and "iso1" in gr.members]
    assert loose[0].members == ("iso1", "iso2")
    # the two blobs are separated at level 1
    a0 = next(gr for gr in groups if gr.level == 1 and "a0" in gr.members)
    assert "b0" not in a0.members


def test_deterministic():
    assert cluster(_two_blobs()) == cluster(_two_blobs())


def test_small_graph_one_level():
    groups = cluster(_graph("abc", [("part_of", "b", "a")]))
    assert {gr.level for gr in groups} == {1}
    assert cluster(NodeGraph()) == ()


def test_loose_nodes_share_one_group():
    assert [g.members for g in cluster(_graph("abcd", []))] == [("a", "b", "c", "d")]
