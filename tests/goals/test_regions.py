from datetime import UTC, datetime

from evowiki.goals.contracts import Aspect, Band
from evowiki.goals.regions import (
    GoalMap,
    MapNode,
    block_known,
    next_region,
    ordered,
    region_order,
)
from tests.goals.fakes_k import FakeK

T = datetime(2026, 10, 10, tzinfo=UTC)
A = Aspect
NO_K = FakeK({})


def _map(*nodes: MapNode) -> GoalMap:
    return GoalMap(map_id="m", goal="我要学机器学习", created_at=T, nodes=nodes)


def R(id_, prereqs=(), **kw) -> MapNode:
    return MapNode(id=id_, kind="region", name=id_, prereqs=tuple(prereqs), **kw)


def test_regions_in_prerequisite_order_ties_by_node_order():
    m = _map(R("sl", ["la", "op"], target=True), R("la"), R("op", ["ca"]), R("ca"), R("ev", ["sl"]))
    assert [n.id for n in region_order(m)] == ["la", "ca", "op", "sl", "ev"]
    assert next_region(m, NO_K) == "la"


def test_known_and_planned_regions_are_skipped():
    """Known is read from K (every cell likely or known), never from the map."""
    k = FakeK({"la": (A.TERM, A.PURPOSE)}, {"la.term": Band.KNOWN, "la.purpose": Band.LIKELY})
    m = _map(R("la"), R("ca", path_id="path-x", state="expanded"), R("op", ["ca"]))
    assert next_region(m, k) == "op"
    m = m.replace(m.node("op").model_copy(update={"page_id": "pg1"}))
    assert next_region(m, k) is None


def test_block_known_is_every_covered_cell_learned_in_k():
    k = FakeK({"c1": (A.TERM, A.PURPOSE), "m1": (A.TERM,), "m2": (A.TERM,)},
              {"c1.term": Band.LIKELY, "c1.purpose": Band.SHAKY, "m1.term": Band.KNOWN,
               "m2.term": Band.LIKELY})
    c1 = R("c1")                                   # a block that is a K concept itself, no members
    hub = R("hub", members=("m1", "m2"))           # a topic (no cells of its own) with members
    m = _map(c1, hub, R("empty"))
    assert not block_known(m, c1, k)               # one cell shaky: not known (the `me` case)
    assert block_known(m, hub, k)                  # its members' cells are all likely+
    assert not block_known(m, m.node("empty"), k)  # no cells: cannot tell
    k.set("c1.purpose", "likely")
    assert block_known(m, c1, k)
    k.set("m2.term", "gap")                        # K drops a cell: to learn again
    assert not block_known(m, hub, k)
    k.set("m2.term", "known")
    assert not block_known(m, hub.model_copy(update={"state": "expanded"}), k)   # entered
    assert not block_known(m, hub.model_copy(update={"path_id": "p"}), k)


def test_expanded_region_waits_for_a_pick_then_hands_over_to_the_part_begun():
    """Spec 2026-10-28 §4: an entered block whose parts none has begun is itself the next step
    (the learner picks a part, never the first one by itself); once a part has begun, learning
    goes on in it, at any depth."""
    m = _map(R("la", state="expanded"), R("pr"),
             R("mat", parent="la"), R("vec", parent="la"),
             MapNode(id="c1", kind="concept", name="c", parent="la", state="expanded"))
    assert [n.id for n in region_order(m)] == ["la", "mat", "vec", "pr"]
    assert next_region(m, NO_K) == "la"
    m = m.replace(m.node("vec").model_copy(update={"state": "expanded"}))
    assert next_region(m, NO_K) == "vec"
    pt = MapNode(id="p1", kind="concept", name="p", parent="vec")
    m = m.replace(pt)
    assert next_region(m, NO_K) == "vec"                       # vec waits for its pick now
    m = m.replace(pt.model_copy(update={"state": "expanded", "path_id": "path-p1"}),
                  MapNode(id="p2", kind="concept", name="q", parent="vec"))
    assert next_region(m, NO_K) == "p2"                        # p1 has its path: the next part
    assert [n.id for n in m.descendants("la")] == ["mat", "vec", "p1", "p2", "c1"]


def test_an_entered_block_he_said_he_knows_is_known_until_a_part_begins():
    """Live 2026-11-04: 这一块我已经会了 on a split block, confirmed and answered right, stayed the next step."""
    m = _map(R("la", state="expanded"), R("pr"), MapNode(id="p1", kind="concept", name="p", parent="la"))
    k = FakeK({"la": (A.TERM,)}, {"la.term": Band.LIKELY})
    assert block_known(m, m.node("la"), k) and next_region(m, k) == "pr"
    m = m.replace(m.node("p1").model_copy(update={"state": "expanded", "path_id": "path-p1"}))
    assert not block_known(m, m.node("la"), k)


def test_ordered_ignores_edges_outside_the_siblings():
    nodes = [R("b", ["a", "elsewhere"]), R("a")]
    assert [n.id for n in ordered(nodes)] == ["a", "b"]
