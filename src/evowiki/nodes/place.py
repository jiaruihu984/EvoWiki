"""Stable placement (spec §4: the map is one picture whose positions never jump).

A node gets x, y once, when it first appears, and keeps them. Units are the page's (graph.html
`layout`): a node is drawn at centre + x·spread·1.15, y·spread, so a small graph sits in about
[-1, 1]; a large one may reach beyond (the page pans).

- New parts of a placed node (a split's children, `part_of` it) grow beside it as graph.html's
  openSplit draws them: an arc on the parent's outer side (away from the centre of its other placed
  neighbours, so inside / at the edge of its group, not across it), one child per row ARC_ROW apart
  so their names never meet, wide enough that the names clear the parent's (`_arc`); the other side
  or a wider arc when that spot is taken.
- Other nodes that come with an already placed neighbour (any relation kind) are placed one at a time,
  smallest id first among those touching a placed node, near the centroid of their placed
  neighbours with a small jitter derived from the id, nudged off collisions: a new node lands in its
  group's region.
- Nodes without any placed neighbour form islands: the connected components of the new nodes (all
  new nodes without a relation pooled into one island, as `nodes.cluster` pools them in one loose
  group). An island is laid out like graph.html draws a view: its automatic level-1 groups
  (`nodes.cluster` on the island) each on a ring around the group's own centre (graph.html: radius
  sc·(0.05 + 0.012·n), each node at 0.8–1.1 of it, y squashed 0.8; a group of more than RING_MAX a
  sunflower disc of the same density), the groups packed without overlap (largest first, each next
  one as close as it fits to the groups it has relations with), then the island put where it keeps
  ISLAND_GAP from every other island and every placed node. Unrelated topics are separate regions.
- `relayout` lays out the whole graph from scratch this way (the one-time fix of graphs placed by
  the older centre spiral, `wikiapp.graph_layout`).
No randomness and no `hash()` (salted per process): the same graph always gives the same result."""

import hashlib
import math
from collections.abc import Collection

import networkx as nx

from evowiki.nodes.groups import cluster
from evowiki.nodes.model import NodeGraph, NodeRec

# Bump when the layout of `relayout` changes so graphs placed by an older one get one relayout.
LAYOUT_VERSION = 7       # 2: the fixed world map, goal by goal and block by block (`wikiapp.graph_layout.world_layout`)
# 7: a block has room for all its dots; a cloud with no relations is a scatter, not a sunflower

MIN_DIST = 0.1
JITTER = 0.16
ATTEMPTS = 400
GOLDEN = math.pi * (3 - math.sqrt(5))

# graph.html: x = cx0 + x·spread·1.15, y = cy0 + y·spread, spread = sc·0.34·scale. Packing works in
# "screen" units (X = 1.15·x, Y = y), where a circle looks round.
SPREAD = 0.34
X_STRETCH = 1.15
RING_MAX = 12
GROUP_MARGIN = 0.15     # a group's name above and its summary line below its ring
GROUP_GAP = 0.1
ISLAND_GAP = 0.5        # between unrelated topics: clearly separate regions
PACK_STEP = 0.03
PACK_TRIES = 40000


def ring_radius(n: int) -> float:
    """graph.html's group radius / (sc·scale) for n members; beyond RING_MAX it grows like a disc
    of the same density (the page uses the same formula, graph-page/draw.ts `groupRadius`)."""
    if n <= RING_MAX:
        return 0.05 + 0.012 * n
    return (0.05 + 0.012 * RING_MAX) * math.sqrt(n / RING_MAX)


def _unit(node_id: str, salt: str = "") -> tuple[float, float]:
    """Two deterministic numbers in [0, 1) from the id."""
    d = hashlib.sha256(f"{salt}{node_id}".encode()).digest()
    return int.from_bytes(d[:8]) / 2**64, int.from_bytes(d[8:16]) / 2**64


def _free(p: tuple[float, float], placed: list[tuple[float, float]], d: float = MIN_DIST) -> bool:
    return all(math.dist(p, q) >= d for q in placed)


def _near(node_id: str, cx: float, cy: float,
          placed: list[tuple[float, float]]) -> tuple[float, float]:
    a, r = _unit(node_id)
    angle, radius = a * 2 * math.pi, JITTER * (0.5 + r)
    for k in range(ATTEMPTS):
        p = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
        if _free(p, placed):
            return p
        angle += GOLDEN
        radius += MIN_DIST * 0.3 / (1 + k / 8)
    u, v = _unit(node_id, "full")  # crowded beyond belief: a deterministic point nearby
    return (cx + u - 0.5, cy + v - 0.5)


# ---- islands -------------------------------------------------------------------------------

def _ring(gid: str, members: list[str]) -> dict[str, tuple[float, float]]:
    """Screen-unit offsets of a group's members around its centre (graph.html's ring; a disc
    beyond RING_MAX)."""
    n = len(members)
    if n == 1:
        return {members[0]: (0.0, 0.0)}
    rad = ring_radius(n)
    base = _unit(gid, "ring")[0] * 2 * math.pi
    out = {}
    for i, k in enumerate(members):
        if n <= RING_MAX:
            ang, ring = i / n * 2 * math.pi + base, rad * (0.8 + 0.3 * _unit(k, "ring")[0])
        else:
            ang, ring = i * GOLDEN + base, rad * math.sqrt((i + 0.5) / n)
        out[k] = (math.cos(ang) * ring / SPREAD, math.sin(ang) * ring * 0.8 / SPREAD)
    return out


def _reach(n: int) -> float:
    """A group's radius in screen units, with room for its name and summary."""
    return ring_radius(n) / SPREAD + GROUP_MARGIN


def _spot(anchor: tuple[float, float], fits) -> tuple[float, float]:
    """The first point on a golden spiral around `anchor` that `fits` (deterministic)."""
    for j in range(PACK_TRIES):
        r, a = PACK_STEP * math.sqrt(j), j * GOLDEN
        p = (anchor[0] + r * math.cos(a), anchor[1] + r * math.sin(a))
        if fits(p):
            return p
    r = PACK_STEP * math.sqrt(PACK_TRIES)
    return (anchor[0] + r, anchor[1])


def _sub(graph: NodeGraph, ids: set[str]) -> NodeGraph:
    return NodeGraph.of([NodeRec(id=r.id, name=r.name) for r in graph.nodes if r.id in ids],
                        [r for r in graph.relations if r.src in ids and r.dst in ids])


Circle = tuple[tuple[float, float], float]


def _island(graph: NodeGraph, ids: set[str],
            fine: list[tuple[str, ...]]) -> tuple[dict[str, tuple[float, float]], list[Circle]]:
    """Screen-unit positions of an island's nodes around (0, 0) and its groups' circles. `fine`:
    the whole graph's level-1 groups (the ones the page draws), cut to the island."""
    sub = _sub(graph, ids)
    groups = sorted((cut for m in fine if (cut := [i for i in m if i in ids])),
                    key=lambda m: (-len(m), m[0]))
    gid = {m: i for i, ms in enumerate(groups) for m in ms}
    links: dict[tuple[int, int], int] = {}
    for r in sub.relations:
        a, b = gid[r.src], gid[r.dst]
        if a != b:
            key = (min(a, b), max(a, b))
            links[key] = links.get(key, 0) + 1
    centres: list[tuple[float, float]] = []
    reach = [_reach(len(m)) for m in groups]
    for i in range(len(groups)):
        linked = [(centres[j], w) for (a, b), w in links.items() for j in (a, b)
                  if i in (a, b) and j != i and j < len(centres)]
        total = sum(w for _, w in linked)
        anchor = ((sum(c[0] * w for c, w in linked) / total, sum(c[1] * w for c, w in linked)
                   / total) if total else (0.0, 0.0))
        centres.append(_spot(anchor, lambda p, i=i: all(
            math.dist(p, c) >= reach[i] + reach[j] + GROUP_GAP for j, c in enumerate(centres))))
    pos: dict[str, tuple[float, float]] = {}
    for i, members in enumerate(groups):
        cx, cy = centres[i]
        for k, (dx, dy) in _ring(f"{i}:{members[0]}", members).items():
            pos[k] = (cx + dx, cy + dy)
    # centre the island on its groups' extent
    lo_x = min(c[0] - reach[i] for i, c in enumerate(centres))
    hi_x = max(c[0] + reach[i] for i, c in enumerate(centres))
    lo_y = min(c[1] - reach[i] for i, c in enumerate(centres))
    hi_y = max(c[1] + reach[i] for i, c in enumerate(centres))
    mx, my = (lo_x + hi_x) / 2, (lo_y + hi_y) / 2
    pos = {k: (p[0] - mx, p[1] - my) for k, p in pos.items()}
    return pos, [((c[0] - mx, c[1] - my), reach[i]) for i, c in enumerate(centres)]


def _islands(graph: NodeGraph, todo: set[str], placed: set[str]) -> tuple[list[set[str]], set[str]]:
    """(islands, attached): the components of the new nodes without a placed neighbour (the
    relation-less ones pooled), and the new nodes reached from a placed node."""
    g = nx.Graph()
    g.add_nodes_from(sorted(todo))
    touches: set[str] = set()
    degree: dict[str, int] = {}
    for r in graph.relations:
        degree[r.src] = degree.get(r.src, 0) + 1
        degree[r.dst] = degree.get(r.dst, 0) + 1
        if r.src in todo and r.dst in todo:
            g.add_edge(r.src, r.dst)
        elif r.src in todo and r.dst in placed:
            touches.add(r.src)
        elif r.dst in todo and r.src in placed:
            touches.add(r.dst)
    islands, attached, loose = [], set(), set()
    for comp in nx.connected_components(g):
        if comp & touches:
            attached |= comp
        elif len(comp) == 1 and degree.get(next(iter(comp)), 0) == 0:
            loose |= comp
        else:
            islands.append(set(comp))
    if loose:
        islands.append(loose)
    islands.sort(key=lambda c: (-len(c), min(c)))
    return islands, attached


def place_missing(graph: NodeGraph, *, only: Collection[str] | None = None) -> NodeGraph:
    """Assign x, y to nodes without a position; placed nodes never move. `only`: place just these
    ids (a split places its children and nothing else, so undo stays exact)."""
    pos = {r.id: (r.x, r.y) for r in graph.nodes if r.x is not None and r.y is not None}
    todo = {r.id for r in graph.nodes if r.id not in pos and (only is None or r.id in only)}
    if not todo:
        return graph
    islands, attached = _islands(graph, todo, set(pos))
    new: dict[str, tuple[float, float]] = {}

    # islands, in screen units, kept apart from each other and from everything already placed
    obstacles = [(p[0] * X_STRETCH, p[1]) for p in pos.values()]
    homes: list[Circle] = []
    fine = [g.members for g in cluster(graph) if g.level == 1] if islands else []
    for ids in islands:
        local, circles = _island(graph, ids, fine)

        def fits(p: tuple[float, float], circles: list[Circle] = circles,
                 homes: list[Circle] = homes) -> bool:
            for (cx, cy), r in circles:
                c = (p[0] + cx, p[1] + cy)
                if any(math.dist(c, h) < r + hr + ISLAND_GAP for h, hr in homes):
                    return False
                if any(math.dist(c, q) < r + ISLAND_GAP for q in obstacles):
                    return False
            return True

        at = _spot((0.0, 0.0), fits)
        homes += [((at[0] + cx, at[1] + cy), r) for (cx, cy), r in circles]
        for k, (dx, dy) in local.items():
            new[k] = ((at[0] + dx) / X_STRETCH, at[1] + dy)
    if not pos and homes:  # a layout from scratch: centre it on the canvas
        lo = min((h[0][0] - h[1]) / X_STRETCH for h in homes)
        hi = max((h[0][0] + h[1]) / X_STRETCH for h in homes)
        lo_y = min(h[0][1] - h[1] for h in homes)
        hi_y = max(h[0][1] + h[1] for h in homes)
        mx, my = (lo + hi) / 2, (lo_y + hi_y) / 2
        new = {k: (p[0] - mx, p[1] - my) for k, p in new.items()}

    # nodes reached from a placed node: parts of a placed node (a split's children) on an arc beside
    # it; the others one at a time, near their placed neighbours
    neighbours: dict[str, set[str]] = {}
    for rel in graph.relations:
        neighbours.setdefault(rel.src, set()).add(rel.dst)
        neighbours.setdefault(rel.dst, set()).add(rel.src)
    every = {**pos, **new}
    left = set(attached)
    kids: dict[str, list[str]] = {}
    for rel in graph.relations:
        if rel.kind == "part_of" and rel.src in left and rel.dst in pos:
            kids.setdefault(rel.dst, []).append(rel.src)
    taken: set[str] = set()
    for parent in sorted(kids):
        mine = sorted(set(kids[parent]) - taken)
        if not mine:
            continue
        for k, p in _arc(graph, parent, mine, every).items():
            every[k] = new[k] = p
        taken |= set(mine)
        left -= set(mine)
    while left:
        rec_id = min(i for i in left if any(n in every for n in neighbours.get(i, ())))
        near = [every[n] for n in sorted(neighbours[rec_id]) if n in every]
        cx = sum(q[0] for q in near) / len(near)
        cy = sum(q[1] for q in near) / len(near)
        every[rec_id] = _near(rec_id, cx, cy, list(every.values()))
        new[rec_id] = every[rec_id]
        left.discard(rec_id)

    out = [r.model_copy(update={"x": round(new[r.id][0], 6), "y": round(new[r.id][1], 6)})
           for r in graph.nodes if r.id in new]
    return graph.updated(nodes=(*(r for r in graph.nodes if r.id not in new), *out))


# A split's children (graph.html openSplit: they grow out of their parent, inside its group): an arc
# on the parent's outer side (away from the centre of its placed neighbours), one child per row so
# their names, written under each dot, never meet; the arc is wide enough that the middle child's
# name clears the parent's. Screen units; ARC_ROW is a name's line plus its dot at near zoom on a
# ~500 px canvas (graph.html: 1 screen unit = sc·0.34·1.2 px there).
ARC_ROW = 0.15
ARC_MIN = 0.3
ARC_PX = 500 * SPREAD * 1.2        # px per screen unit at near zoom on a 500 px canvas
NAME_PX = 11.5


def _name_half(name: str) -> float:
    """Half the width of a node's name at near zoom, in screen units (CJK = font size)."""
    w = sum(NAME_PX if ord(ch) >= 0x2E80 else NAME_PX * 0.56 for ch in name)
    return w / 2 / ARC_PX


def _arc(graph: NodeGraph, parent: str, kids: list[str],
         every: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    px, py = every[parent][0] * X_STRETCH, every[parent][1]
    around = [every[n] for n in _placed_neighbours(graph, parent, every, set(kids))]
    side = 1.0
    if around:
        cx = sum(q[0] for q in around) / len(around) * X_STRETCH
        side = 1.0 if px >= cx else -1.0
    rec = graph.node(parent)
    names = [graph.node(k).name for k in kids]  # type: ignore[union-attr]
    n = len(kids)
    half = (n - 1) / 2 * ARC_ROW
    reach = max(_name_half(x) for x in names) + _name_half(rec.name if rec else "") + 0.05
    others = [(q[0] * X_STRETCH, q[1]) for k, q in every.items() if k != parent]
    for grow in range(12):
        r = max(ARC_MIN, half * 1.2, reach) + grow * 0.1
        for s in (side, -side):
            pts = []
            for j in range(n):
                dy = (j - (n - 1) / 2) * ARC_ROW
                pts.append((px + s * math.sqrt(max(r * r - dy * dy, 0.0)), py + dy))
            if all(_free(p, others, MIN_DIST * 1.5) for p in pts):
                return {k: (p[0] / X_STRETCH, p[1]) for k, p in zip(kids, pts, strict=True)}
    r = max(ARC_MIN, half * 1.2, reach)
    return {k: ((px + side * math.sqrt(max(r * r - ((j - (n - 1) / 2) * ARC_ROW) ** 2, 0.0)))
                / X_STRETCH, py + (j - (n - 1) / 2) * ARC_ROW) for j, k in enumerate(kids)}


def _placed_neighbours(graph: NodeGraph, node: str, every: dict[str, tuple[float, float]],
                       skip: set[str]) -> list[str]:
    out = set()
    for r in graph.relations:
        for a, b in ((r.src, r.dst), (r.dst, r.src)):
            if a == node and b in every and b not in skip:
                out.add(b)
    return sorted(out)


def relayout(graph: NodeGraph) -> NodeGraph:
    """Every node placed from scratch (islands of groups, see the module docstring)."""
    bare = [r.model_copy(update={"x": None, "y": None}) for r in graph.nodes]
    return place_missing(graph.updated(nodes=bare))
