"""The original learning view's split placement, saved in world coordinates."""

import math
from collections.abc import Collection, Mapping, Sequence

from evowiki.goals.regions import GoalMap, MapNode
from evowiki.nodes.model import NodeGraph

Point = tuple[float, float]


def split_members(m: GoalMap, region: MapNode) -> list[str]:
    """Actual map points in the learning view's order: concepts, then subregions."""
    regions = {n.id for n in m.nodes if n.kind == "region"}
    inside = {n.id for n in m.descendants(region.id)}
    subs = [n for n in m.nodes if n.kind == "region" and n.id in inside]
    out: list[str] = []
    for r in [region, *subs]:
        concepts = [n.id for n in m.nodes if n.kind == "concept" and n.parent == r.id]
        concepts += [i for i in r.members if i not in regions]
        if r is not region:
            out.append(r.id)
        out.extend(concepts)
    return list(dict.fromkeys(out))


def split_positions(anchor: Point, members: Sequence[str], occupied: Sequence[Point] = (),
                    known: Mapping[str, Point] = {}) -> dict[str, Point]:
    """Port of learn-scene's spot(): the hub took slot zero before its children."""
    taken = [anchor, *occupied, *known.values()]
    out: dict[str, Point] = {}
    n = 1
    for member in members:
        if member in known:
            out[member] = known[member]
            continue
        while True:
            d, angle = 0.14 * math.sqrt(n), n * 2.39996
            p = (anchor[0] + d * math.cos(angle), anchor[1] + d * math.sin(angle))
            n += 1
            if n > 400 or all(math.dist(p, q) >= 0.1 for q in taken):
                out[member] = p
                taken.append(p)
                break
    return out


def place_split_children(graph: NodeGraph, maps: Collection[GoalMap], existing: Collection[str]) -> NodeGraph:
    """Place only newly added children; ordinary saves keep all recorded coordinates."""
    nodes = {n.id: n for n in graph.nodes}
    old = set(existing)
    for m in maps:
        for region in m.children(None):
            if region.kind != "region" or region.state != "expanded":
                continue
            parent = nodes.get(region.id)
            members = [i for i in split_members(m, region) if i in nodes]
            if parent is None or parent.x is None or parent.y is None or not any(i not in old for i in members):
                continue
            known = {i: (nodes[i].x, nodes[i].y) for i in members
                     if i in old and nodes[i].x is not None and nodes[i].y is not None}
            occupied = [(n.x, n.y) for i, n in nodes.items()
                        if i not in members and n.x is not None and n.y is not None]
            points = split_positions((parent.x, parent.y), members, occupied, known)
            for i, (x, y) in points.items():
                if i not in old:
                    nodes[i] = nodes[i].model_copy(update={"x": x, "y": y})
    return graph.updated(nodes=nodes.values())
