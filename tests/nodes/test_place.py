import itertools
import math

from evowiki.nodes import NodeGraph, NodeRec, Relation, cluster
from evowiki.nodes.place import X_STRETCH, place_missing, relayout
from tests.nodes.synthetic_shape import NODES, migrated_graph


def _graph(placed: dict[str, tuple[float, float]], free: list[str], rels=()) -> NodeGraph:
    nodes = [NodeRec(id=i, name=i, x=p[0], y=p[1]) for i, p in placed.items()]
    nodes += [NodeRec(id=i, name=i) for i in free]
    return NodeGraph.of(nodes, [Relation(kind=k, src=s, dst=d) for k, s, d in rels])


def test_never_moves_placed_and_fills_all():
    graph = _graph({"a": (0.3, -0.2)}, ["b", "c", "d"], [("part_of", "b", "a")])
    out = place_missing(graph)
    assert out.node("a").x == 0.3 and out.node("a").y == -0.2
    for r in out.nodes:
        assert r.x is not None and -1 <= r.x <= 1 and -1 <= r.y <= 1


def test_near_placed_neighbours():
    graph = _graph(
        {"a": (0.5, 0.5), "b": (0.6, 0.5)},
        ["c"],
        [("part_of", "c", "a"), ("prerequisite", "b", "c")],
    )
    c = place_missing(graph).node("c")
    assert math.dist((c.x, c.y), (0.55, 0.5)) < 0.3


def test_deterministic_and_no_collisions():
    ids = [f"n{i}" for i in range(60)]
    rels = [("part_of", ids[i], ids[i // 3]) for i in range(1, 60)]
    graph = _graph({}, ids, rels)
    a, b = place_missing(graph), place_missing(graph)
    assert a == b
    pts = [(r.x, r.y) for r in a.nodes]
    close = sum(1 for i in range(len(pts)) for j in range(i) if math.dist(pts[i], pts[j]) < 0.03)
    assert close == 0


def test_idempotent():
    graph = place_missing(_graph({}, ["a", "b"], [("related", "a", "b")]))
    assert place_missing(graph) == graph


# ---- islands of groups (bug 2026-10-15: a migrated graph drawn as one blob) ----------------------

def _screen(g, i):
    r = g.node(i)
    return (r.x * X_STRETCH, r.y)


def _box(g, members):
    pts = [_screen(g, m) for m in members]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts),
            max(p[1] for p in pts))


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _components(g):
    import networkx as nx
    u = nx.Graph()
    u.add_nodes_from(r.id for r in g.nodes)
    u.add_edges_from((r.src, r.dst) for r in g.relations)
    return [set(c) for c in nx.connected_components(u)]


def test_the_migrated_blob_is_the_bug():
    g = migrated_graph()
    centres = [_centroid(g, x.members) for x in cluster(g) if x.level == 1]
    assert max(math.dist(a, b) for a in centres for b in centres) < 0.3   # titles collide


def _centroid(g, members):
    pts = [_screen(g, m) for m in members]
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def test_relayout_gives_each_group_its_own_region_and_keeps_topics_apart():
    g = relayout(migrated_graph())
    assert {r.id for r in g.nodes} == {i for i, *_ in NODES}
    assert g.relations == migrated_graph().relations
    groups = [x.members for x in cluster(g) if x.level == 1]
    assert len(groups) == 3
    boxes = [_box(g, m) for m in groups]
    assert not any(_overlap(boxes[i], boxes[j]) for i in range(3) for j in range(i))
    centres = [_centroid(g, m) for m in groups]
    assert min(math.dist(centres[i], centres[j]) for i in range(3) for j in range(i)) > 1.0
    # each group sits on its own ring: members close to their centre, far from the others
    for m, c in zip(groups, centres, strict=True):
        assert max(math.dist(_screen(g, i), c) for i in m) < 0.7
    # unrelated topics (connected components) clearly apart
    comps = _components(g)
    assert len(comps) == 2
    a, b = comps
    assert min(math.dist(_screen(g, i), _screen(g, j)) for i in a for j in b) > 0.6
    # it fits the page's canvas like the demo (graph.html's groups sit in about [-1.05, 0.95])
    assert all(abs(r.x) <= 1.2 and abs(r.y) <= 1.2 for r in g.nodes)
    # no two nodes on top of each other
    pts = [_screen(g, r.id) for r in g.nodes]
    assert min(math.dist(pts[i], pts[j]) for i in range(len(pts)) for j in range(i)) > 0.08
    assert relayout(migrated_graph()) == g                 # deterministic


def test_after_relayout_added_nodes_go_to_their_group_and_nothing_moves():
    g = relayout(migrated_graph())
    before = {r.id: (r.x, r.y) for r in g.nodes}
    grown = place_missing(g.with_node(NodeRec(id="c22", name="核糖")).with_relation(
        Relation(kind="prerequisite", src="c22", dst="node-17")))       # next to 核苷酸
    assert {r.id: (r.x, r.y) for r in grown.nodes if r.id != "c22"} == before
    assert math.dist(_screen(grown, "c22"), _screen(grown, "node-17")) < 0.5
    # a new unrelated topic is an island of its own, away from everything placed
    island = grown.with_node(NodeRec(id="z1", name="吉他和弦")).with_node(
        NodeRec(id="z2", name="指法")).with_relation(Relation(kind="prerequisite", src="z2",
                                                              dst="z1"))
    out = place_missing(island)
    assert {r.id: (r.x, r.y) for r in out.nodes if r.id[0] != "z"} == \
        {r.id: (r.x, r.y) for r in grown.nodes}
    old = [_screen(out, r.id) for r in grown.nodes]
    for z in ("z1", "z2"):
        assert min(math.dist(_screen(out, z), p) for p in old) > 0.5
    assert math.dist(_screen(out, "z1"), _screen(out, "z2")) < 0.6


def test_a_split_still_places_only_its_children():
    g = relayout(migrated_graph())
    kids = [NodeRec(id="c5.a", name="碱基部分"), NodeRec(id="c5.b", name="糖部分")]
    nxt = g.updated(nodes=(*g.nodes, *kids), relations=(
        *g.relations, *(Relation(kind="part_of", src=k.id, dst="node-17") for k in kids)))
    out = place_missing(nxt, only={"c5.a"})
    assert out.node("c5.b").x is None and out.node("c5.a").x is not None


# ---- a split's children (bug 2026-10-15, second screenshot: a chain of children across the map) --

def test_split_children_sit_on_an_arc_beside_their_parent_and_nothing_else_moves():
    from tests.nodes.synthetic_split import CHILDREN, PARENT, relaid, split_graph
    before = relaid()
    g = split_graph(before)
    assert split_graph(before) == g                                    # deterministic
    assert {r.id: (r.x, r.y) for r in g.nodes if r.id in {r.id for r in before.nodes}} == \
        {r.id: (r.x, r.y) for r in before.nodes}                       # nothing else moves
    kids = [i for i, _ in CHILDREN]
    p = _screen(g, PARENT)
    dists = [math.dist(_screen(g, k), p) for k in kids]
    assert max(dists) < 0.8 and max(dists) - min(dists) < 1e-6           # one arc, close by
    ys = sorted(_screen(g, k)[1] for k in kids)
    assert min(b - a for a, b in itertools.pairwise(ys)) >= 0.15 - 1e-9   # one name per row
    pts = [_screen(g, r.id) for r in g.nodes]
    assert min(math.dist(pts[i], pts[j]) for i in range(len(pts)) for j in range(i)) > 0.1
    # inside the parent's group: nearer the parent than any node of another group (before the split)
    other = [m for x in cluster(before) if x.level == 1 and PARENT not in x.members for m in x.members]
    for k in kids:
        assert math.dist(_screen(g, k), p) < min(math.dist(_screen(g, k), _screen(g, o)) for o in other)
    # and clustering keeps them with their parent
    [mine] = [x for x in cluster(g) if x.level == 1 and PARENT in x.members]
    assert set(kids) <= set(mine.members)
    # on the outer side of the parent's group (away from the centre of its other neighbours)
    side = [_screen(g, k)[0] - p[0] for k in kids]
    assert all(s > 0 for s in side) or all(s < 0 for s in side)
