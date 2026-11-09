"""The fixed map (`wikiapp.graph_layout`): position = f(data, previous positions): stable while the data stands, similar subjects next to
each other when it changes, old points moving only as far as they must; a point on two goals has a place in each."""

from datetime import UTC, datetime

import numpy as np

from evowiki.goals.regions import GoalMap, MapNode
from evowiki.nodes import NodeGraph, NodeRec, place_missing
from evowiki.wikiapp.graph_layout import SPACING, _scatter, arrange, copy_places, needs_arranging
from evowiki.wikiapp.state import WikiState

T0 = datetime(2026, 1, 1, tzinfo=UTC)
FISH, SHRIMP, NET = [1.0, 0.0, 0.0], [0.8, 0.6, 0.0], [0.0, 0.0, 1.0]


class Vectors:
    id = "fake"
    model_version = "1"

    def embed_documents(self, texts):
        by = {"孔雀鱼": FISH, "黑壳虾": SHRIMP, "神经网络": NET}
        return [by[t.split("：")[0]] for t in texts]


def goal(key, title, n=4):
    return GoalMap(map_id=key, goal="我要学" + title, title=title, created_at=T0,
                   nodes=tuple(MapNode(id=f"{key}-{i}", kind="region", name=f"{title}{i}") for i in range(n)))


def state_of(*maps, before=None, arranged=()):
    new = {n.id: n.name for m in maps for n in m.nodes if before is None or before.node(n.id) is None}
    nodes = [NodeRec(id=i, name=name, definition="") for i, name in new.items()]
    graph = place_missing(NodeGraph(nodes=(*(before.nodes if before else ()), *nodes), relations=()))
    return WikiState(graph=graph, maps={m.map_id: m for m in maps}, map_order=[m.map_id for m in maps], graph_arranged=arranged)


def centre(graph, m):
    return np.mean([[graph.node(n.id).x, graph.node(n.id).y] for n in m.nodes], axis=0)


def box_of(graph, m):
    xs, ys = zip(*((graph.node(n.id).x, graph.node(n.id).y) for n in m.nodes), strict=True)
    return min(xs), max(xs), min(ys), max(ys)


def test_two_fish_goals_stand_next_to_each_other_and_a_third_subject_does_not_come_between():
    shrimp, net, fish = goal("s", "黑壳虾", 12), goal("n", "神经网络", 8), goal("f", "孔雀鱼", 6)
    first, ids = arrange(state_of(shrimp, net), Vectors())
    graph, _ = arrange(state_of(shrimp, net, fish, before=first, arranged=ids), Vectors())
    near = np.linalg.norm(centre(graph, shrimp) - centre(graph, fish))
    far = np.linalg.norm(centre(graph, fish) - centre(graph, net))
    assert near < far


def test_old_points_move_rigidly_with_their_group_and_only_as_much_as_needed():
    shrimp, net = goal("s", "黑壳虾", 12), goal("n", "神经网络", 8)
    first, ids = arrange(state_of(shrimp, net), Vectors())
    second, _ = arrange(state_of(shrimp, net, goal("f", "孔雀鱼", 6), before=first, arranged=ids), Vectors())
    moves = np.array([[second.node(n.id).x - first.node(n.id).x, second.node(n.id).y - first.node(n.id).y] for n in shrimp.nodes])
    assert np.allclose(moves, moves[0], atol=1e-5)                    # a rigid picture
    xs = [r.x for r in first.nodes]
    assert np.linalg.norm(moves[0]) < (max(xs) - min(xs))             # not across the map


def test_nothing_moves_until_the_top_groups_change():
    shrimp, net = goal("s", "黑壳虾", 12), goal("n", "神经网络", 8)
    first, ids = arrange(state_of(shrimp, net), Vectors())
    state = WikiState(graph=first, maps={"s": shrimp, "n": net}, map_order=["s", "n"], graph_arranged=ids)
    assert not needs_arranging(state)
    grown = state.model_copy(update={"maps": {**state.maps, "f": goal("f", "孔雀鱼", 2)}, "map_order": ["s", "n", "f"],
                                     "graph": place_missing(first.model_copy(update={"nodes": (*first.nodes, *(NodeRec(id=f"f-{i}", name="x", definition="") for i in range(2)))}))})
    assert needs_arranging(grown)


def test_no_two_top_groups_on_each_other():
    maps = [goal("a", "黑壳虾", 20), goal("b", "神经网络", 9), goal("c", "孔雀鱼", 9)]
    graph, _ = arrange(state_of(*maps), Vectors())
    boxes = [box_of(graph, m) for m in maps]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a[1] < b[0] or b[1] < a[0] or a[3] < b[2] or b[3] < a[2]


def test_a_point_on_two_goals_has_a_place_in_each():
    a = goal("a", "黑壳虾", 4)
    shared = a.nodes[0].id
    b = GoalMap(map_id="b", goal="g", title="孔雀鱼", created_at=T0, nodes=(MapNode(id="b-r", kind="region", name="基础", members=(shared,)), MapNode(id=shared, kind="concept", name="共用", parent="b-r")))
    state = state_of(a, b)
    graph, _ = arrange(state, Vectors())
    further = copy_places(state.model_copy(update={"graph": graph}), graph)
    spot = further["block-b-b-r"][shared]
    assert spot != (graph.node(shared).x, graph.node(shared).y)
    assert all(np.hypot(spot[0] - r.x, spot[1] - r.y) >= 0.09 for r in graph.nodes)


def test_a_block_of_shared_points_has_room_for_them_all_and_its_dots_keep_the_cloud_spacing():
    a = goal("a", "黑壳虾", 9)
    shared = tuple(n.id for n in a.nodes)
    block = MapNode(id="b-r", kind="region", name="基础", members=shared)
    other = MapNode(id="b-s", kind="region", name="别的")
    b = GoalMap(map_id="b", goal="g", title="孔雀鱼", created_at=T0, nodes=(block, other, *a.nodes))
    state = state_of(a, b)
    graph, _ = arrange(state, Vectors())
    spots = list(copy_places(state.model_copy(update={"graph": graph}), graph)["block-b-b-r"].values())
    near = [min(np.hypot(p[0] - q[0], p[1] - q[1]) for q in spots if q != p) for p in spots]
    assert min(near) >= SPACING
    # the other block's point is clear of them: the block's box held all its dots when the goal was laid out
    s = graph.node("b-s")
    assert min(np.hypot(p[0] - s.x, p[1] - s.y) for p in spots) >= SPACING


def test_a_cloud_with_no_relations_is_no_spiral_and_the_same_for_the_same_block():
    pts = _scatter(30, "k")
    assert pts == _scatter(30, "k") and pts != _scatter(30, "other")
    assert min(np.hypot(p[0] - q[0], p[1] - q[1]) for p in pts for q in pts if p != q) >= SPACING


def test_region_scope_text_does_not_reserve_phantom_points_or_change_positions():
    """A region is one real point until it is split; prose punctuation is not geometry."""
    plain = goal("a", "孔雀鱼", 5)
    described = plain.model_copy(update={"nodes": tuple(n.model_copy(update={
        "scope": "外形、习性，环境;饲料,繁殖；健康"}) for n in plain.nodes)})
    first, _ = arrange(state_of(plain), fresh=True)
    second, _ = arrange(state_of(described), fresh=True)
    assert len(first.nodes) == len(second.nodes) == 5
    assert [(n.id, n.x, n.y) for n in first.nodes] == [(n.id, n.x, n.y) for n in second.nodes]
    assert first.relations == second.relations


def test_organic_block_packing_keeps_titles_and_real_clouds_clear():
    from evowiki.wikiapp.graph_layout import BLOCK_GAP, BLOCK_TITLE, _cloud, _picture

    blocks = [(name, [f"r{i}"], 1) for i, name in enumerate([
        "认识孔雀鱼", "水质与环境", "日常喂养", "繁殖和幼鱼照顾", "健康观察"])]
    graph = NodeGraph(nodes=tuple(NodeRec(id=ms[0], name=title, definition="") for title, ms, _ in blocks))
    box, points = _picture("孔雀鱼", blocks, graph)
    assert set(points) == {n.id for n in graph.nodes}
    assert (box, points) == _picture("孔雀鱼", blocks, graph)
    rectangles = []
    for title, members, room in blocks:
        cloud, local = _cloud(members, title, BLOCK_TITLE, room, graph)
        key = members[0]
        x, y = points[key][0] - local[key][0], points[key][1] - local[key][1]
        rectangles.append((x, y, x + cloud.w, y + cloud.h))
    for i, a in enumerate(rectangles):
        for b in rectangles[i + 1:]:
            assert (a[2] + BLOCK_GAP <= b[0] + 1e-6 or b[2] + BLOCK_GAP <= a[0] + 1e-6
                    or a[3] + BLOCK_GAP <= b[1] + 1e-6 or b[3] + BLOCK_GAP <= a[1] + 1e-6)
    # A sparse goal must not collapse into shelves or a single horizontal line.
    assert len({round(y, 5) for _, y in points.values()}) >= 4
