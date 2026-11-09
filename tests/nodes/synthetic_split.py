"""Synthetic multi-topic graph after relayout, with five split children."""

from datetime import UTC, datetime

from evowiki.nodes import NodeGraph, NodeRec, split
from evowiki.nodes.place import relayout
from tests.nodes.synthetic_shape import migrated_graph

PARENT = "node-03"
CHILDREN = [("node-03.part-1", "示例子主题一"), ("node-03.part-2", "示例子主题二"), ("node-03.part-3", "示例子主题三"),
            ("node-03.part-4", "示例子主题四"), ("node-03.part-5", "示例子主题五")]


def relaid() -> NodeGraph:
    return relayout(migrated_graph())


def split_graph(graph: NodeGraph | None = None) -> NodeGraph:
    return split(relaid() if graph is None else graph, PARENT,
                 [NodeRec(id=i, name=n) for i, n in CHILDREN], [],
                 change_id="g_split_1", at=datetime(2026, 1, 1, tzinfo=UTC),
                 why="Synthetic split request", summary="Synthetic split result")
