"""GET /wiki/graph leaves out a deleted block's points while nothing uses them and nothing is known about them (user 2026-11-04): they stay in the
shared graph and show again on a goal. A point just made or just split out is shown, on a goal or not."""

from datetime import UTC, datetime

from evowiki.goals.regions import GoalMap, MapNode
from evowiki.nodes import NodeGraph, NodeRec, place_missing
from evowiki.nodes.model import Relation
from evowiki.wikiapp.events import GoalMapSaved, PageRecord, WikiEvent
from evowiki.wikiapp.graph_out import graph_out
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.state import WikiState, apply_event

T0 = datetime(2026, 1, 1, tzinfo=UTC)
REGION = MapNode(id="r", kind="region", name="块", members=("a", "b"))
A, B = MapNode(id="a", kind="concept", name="A"), MapNode(id="b", kind="concept", name="B")


def goal(*nodes):
    return GoalMap(map_id="m", goal="g", title="目标", created_at=T0, nodes=nodes)


def saved(s, m):
    return apply_event(s, WikiEvent(event_id="e", ts=T0, body=GoalMapSaved(record=m, action="edited")))


def state(pages=None, trashed=()):
    nodes = tuple(NodeRec(id=i, name=i.upper(), definition="") for i in ["r", "a", "b", "c", "a1"])
    rels = (Relation(kind="prerequisite", src="a", dst="b"), Relation(kind="part_of", src="a1", dst="a", source="split"))
    return WikiState(graph=place_missing(NodeGraph(nodes=nodes, relations=rels)), pages=pages or {}, trashed_pages=frozenset(trashed))


def ids(s):
    return {n.id for n in graph_out(s, MemoryRepo(["u"]).load("u").k, T0).nodes}


def test_a_point_never_on_a_goal_and_one_just_split_out_are_shown():
    assert ids(state()) == {"r", "a", "b", "c", "a1"}


def test_a_deleted_block_leaves_the_picture_with_what_was_split_out_of_it_and_its_lines():
    s = saved(saved(state(), goal(REGION, A, B)), goal(MapNode(id="other", kind="region", name="别的")))
    o = graph_out(s, MemoryRepo(["u"]).load("u").k, T0)
    assert {n.id for n in o.nodes} == {"c"}
    assert o.relations == [] and all(set(g.members) <= {"c"} for g in o.groups)


def test_back_on_a_goal_it_shows_again():
    gone = saved(saved(state(), goal(REGION, A, B)), goal())
    assert "a" not in ids(gone)
    assert {"r", "a", "b", "a1"} <= ids(saved(gone, goal(REGION, A, B)))


def test_a_deleted_blocks_point_with_a_page_stays_until_the_page_is_in_the_trash():
    page = PageRecord(page_id="p", title="页", goal="g", concepts=("a",), targets=(), created_at=T0)
    used = saved(saved(state(pages={"p": page}), goal(REGION, A, B)), goal())
    assert "a" in ids(used)
    trashed = saved(saved(state(pages={"p": page}, trashed=("p",)), goal(REGION, A, B)), goal())
    assert "a" not in ids(trashed)
