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
)

AT = datetime(2026, 10, 13, tzinfo=UTC)


def base() -> NodeGraph:
    nodes = [NodeRec(id=i, name=i.upper(), x=0.1 * k, y=0.0) for k, i in enumerate("abcde")]
    rels = [
        Relation(kind="part_of", src="b", dst="a"),
        Relation(kind="part_of", src="c", dst="a", phrase="c in a"),
        Relation(kind="prerequisite", src="d", dst="b"),
        Relation(kind="prerequisite", src="b", dst="c"),
        Relation(kind="related", src="e", dst="c"),
        Relation(kind="prerequisite", src="d", dst="c"),
    ]
    return NodeGraph.of(nodes, rels)


def kids():
    return [NodeRec(id="b1", name="B1"), NodeRec(id="b2", name="B2", definition="two")]


def do_split(graph, cid="s1"):
    return split(
        graph,
        "b",
        kids(),
        [Relation(kind="prerequisite", src="b1", dst="b2", phrase="first")],
        phrases={"b1": "b1 is part of b"},
        change_id=cid,
        at=AT,
        why="confusing",
        summary="split b",
    )


def core(graph):
    d = graph.model_dump()
    return d["nodes"], d["relations"]


def test_split_adds_children_keeps_parent():
    g0 = base()
    g1 = do_split(g0)
    assert set(g1.children("b")) == {"b1", "b2"}
    assert g1.node("b") == g0.node("b")
    assert set(g0.relations) <= set(g1.relations)
    assert g1.node("b1").x is not None
    pr = next(r for r in g1.relations if r.kind == "part_of" and r.src == "b1")
    assert pr.phrase == "b1 is part of b" and pr.source == "split"
    ch = g1.changes[-1]
    assert ch.kind == "split" and {r.id for r in ch.added_nodes} == {"b1", "b2"}
    assert len(ch.added_relations) == 3


def test_split_refusals():
    with pytest.raises(GraphError) as e:
        split(base(), "zz", kids(), [], change_id="s", at=AT, why="", summary="")
    assert e.value.code == "unknown_id"
    with pytest.raises(GraphError) as e:
        split(
            base(), "b", [NodeRec(id="c", name="C")], [], change_id="s", at=AT, why="", summary=""
        )
    assert e.value.code == "duplicate_node"
    with pytest.raises(GraphError) as e:
        split(base(), "b", [], [], change_id="s", at=AT, why="", summary="")
    assert e.value.code == "empty"
    with pytest.raises(GraphError) as e:
        split(
            base(),
            "b",
            kids(),
            [Relation(kind="related", src="b1", dst="zz")],
            change_id="s",
            at=AT,
            why="",
            summary="",
        )
    assert e.value.code == "unknown_id"
    with pytest.raises(GraphError) as e:          # a link that touches no part of the split
        split(base(), "b", kids(), [Relation(kind="recommended", src="d", dst="e")],
              change_id="s", at=AT, why="", summary="")
    assert e.value.code == "unknown_id"
    with pytest.raises(GraphError) as e:          # reusing the node's ancestor as its part
        split(base(), "b", kids(), [], change_id="s", at=AT, why="", summary="", reused=["a"])
    assert e.value.code == "cycle"


def test_split_reuses_an_existing_node_and_its_undo_takes_only_the_part_of():
    g0 = base()
    g1 = split(g0, "b", kids()[:1], [Relation(kind="recommended", src="e", dst="b1")],
               change_id="s1", at=AT, why="", summary="", reused=["e"])
    assert {r.src for r in g1.relations if r.kind == "part_of" and r.dst == "b"} == {"b1", "e"}
    ch = g1.changes[-1]
    assert ch.node_ids == ("b", "b1", "e") and [n.id for n in ch.added_nodes] == ["b1"]
    g2 = undo(g1, "s1")
    assert core(g2) == core(g0)


def test_split_undo_restores_exactly():
    g0 = base()
    g2 = undo(do_split(g0), "s1")
    assert core(g2) == core(g0)
    assert g2.changes[-1].undone
    with pytest.raises(UndoConflict):
        undo(g2, "s1")
    with pytest.raises(UndoConflict):
        undo(g2, "nope")


def test_undo_conflict_when_later_change_touched_added():
    g1 = do_split(base())
    g2 = split(g1, "b1", [NodeRec(id="b11", name="B11")], [], change_id="s2", at=AT, why="",
               summary="")
    with pytest.raises(UndoConflict):
        undo(g2, "s1")
    g3 = undo(g2, "s2")
    g4 = undo(g3, "s1")
    assert core(g4) == core(base())


def test_undo_conflict_when_relation_outside_changes_touches_added_node():
    g1 = do_split(base()).with_relation(Relation(kind="related", src="b1", dst="e"))
    with pytest.raises(UndoConflict):
        undo(g1, "s1")


def test_split_of_independent_node_still_undoable():
    g1 = do_split(base())
    g2 = split(
        g1, "e", [NodeRec(id="e1", name="E1")], [], change_id="s2", at=AT, why="", summary=""
    )
    g3 = undo(g2, "s1")
    assert g3.node("b1") is None and g3.node("e1") is not None


def test_split_keeps_the_models_note_and_the_learners_words():
    g1 = split(base(), "b", kids(), [], change_id="s1", at=AT, why="你点了看不懂", summary="s",
               note="b 里其实有两件事", words="完全看不懂")
    c = g1.changes[-1]
    assert (c.note, c.words) == ("b 里其实有两件事", "完全看不懂")
