"""One-time relayout of a graph placed by an older layout (bug 2026-10-15: graphs migrated with the
first `place_missing` sat in one blob around the centre, unrelated topics interleaved, so the 知识图谱
page drew every group title on top of each other).

Positions are stored and never move afterwards (spec §4); a graph whose positions came from an
older layout (`WikiState.graph_layout` < `nodes.place.LAYOUT_VERSION`) is laid out again ONCE with
`nodes.place.relayout` and the result recorded as `GraphSaved(action="relayout",
layout=LAYOUT_VERSION)`: the event log says when and why every position changed, replay gives the
same state, and from then on nodes only get added near their group (`place_missing`). A graph made
from scratch by the current code already carries the current version (`graph_sync.graph_apply`), so
it is never relaid. Nothing else changes: nodes, relations, the change log and names are the same.

`ensure_layout` runs where the graph is read (GET /wiki/graph, before `graph_out`). A lost race is
left to the next read; positions are only display, so any failure leaves the stored graph as it was
and the caller serves it (logged).

The map does not depend on the window (2026-11-04): world coordinates, stored on the nodes, the same for 知识图谱, 学点新的 and 今天's
thumbnail; a view only moves its camera. It does change with the data: position = f(data, previous positions). When the set of top groups
changes (a goal added, a cluster changed) `arrange` runs once, on the graph read that first sees it, and stores the result (GraphSaved, so
a reload replays the same map): each top group is a rigid picture; its place comes from stress majorization towards distances given by its
subject's embedding (`group_places`; similar ones next to each other) with an anchor term to where it stood, then overlaps are pushed apart.
A new top group gets its own picture at its best spacing: each block under its title, its points by the relations among them (ForceAtlas2) or,
with none, a Poisson-disc scatter (a sunflower reads as a spiral); the blocks packed organically under the goal's title, each with room for all its dots.
A node added inside a top group keeps the rest where they are (`place_missing`). A point's further dots on other goals (`copy_places`) stand
round their block's middle, in the first free spot."""

import hashlib
import logging
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import networkx as nx
import numpy as np

from evowiki.goals.regions import MapNode
from evowiki.nodes import NodeGraph
from evowiki.nodes.place import LAYOUT_VERSION, place_missing
from evowiki.retrieval.embed import Embedder
from evowiki.wikiapp.events import GraphSaved, WikiEvent
from evowiki.wikiapp.goal_groups import goal_groups
from evowiki.wikiapp.group_places import _vectors, group_text, top_groups
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Commit, Repo, Snapshot
from evowiki.wikiapp.split_layout import split_members, split_positions
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import GroupOut

log = logging.getLogger(__name__)


def needs_relayout(state: WikiState) -> bool:
    return bool(state.graph.nodes) and state.graph_layout < LAYOUT_VERSION


SPACING = 0.14             # between the points of a cloud, in world units (MIN_DIST is what `place_missing` keeps)
PX = 240.0                 # pixels a world unit is drawn at in the usual window: what a title takes of the map
BLOCK_TITLE, GOAL_TITLE = 13.0, 16.0
BLOCK_GAP, TOP_GAP = 0.12, 0.3
GOLDEN = math.pi * (3 - math.sqrt(5))
DOMAIN_PULL = 0.5          # a domain's groups aim this much closer than their subjects' distance says
FORCE_ROUNDS, FORCE_SEED, CLOUD_REACH = 200, 20261104, 1.5
ANCHOR = 1.0               # how hard a top group holds to where it stood, against the pull of all the others
STRESS_ROUNDS, PUSH_ROUNDS = 300, 600
DARTS = 30                 # throws at a disc before it grows


def _title_w(name: str, px: float) -> float:
    return (sum(px if ord(c) > 0x2e7f else px * 0.62 for c in name) + 14) / PX


def _head(px: float) -> float:
    return (px * 1.3 + 16) / PX


@dataclass
class _Box:
    """A rectangle of the map: its size, and where its top-left corner went."""

    w: float
    h: float
    x: float = 0.0
    y: float = 0.0


def _rng(key: str) -> random.Random:
    return random.Random(hashlib.sha256(key.encode()).hexdigest())


def _dart(cx: float, cy: float, taken: list[tuple[float, float]], apart: float, rng: random.Random) -> tuple[float, float]:
    """A free spot near (cx, cy), at least `apart` from everything taken (dart throwing in a disc that grows until one lands): an even scatter with no pattern."""
    reach = apart
    while True:
        for _ in range(DARTS):
            r, a = reach * math.sqrt(rng.random()), rng.random() * 2 * math.pi
            p = (round(cx + r * math.cos(a), 6), round(cy + r * math.sin(a), 6))
            if all(math.hypot(p[0] - q[0], p[1] - q[1]) >= apart for q in taken):
                return p
        reach += apart / 2


def _scatter(n: int, key: str) -> list[tuple[float, float]]:
    """n points round the origin, spaced as a cloud, the same for the same key."""
    rng = _rng(key)
    pts: list[tuple[float, float]] = []
    for _ in range(n):
        pts.append(_dart(0.0, 0.0, pts, SPACING, rng))
    return pts


def _by_relations(members: Sequence[str], graph: NodeGraph) -> list[tuple[float, float]]:
    """The members' places: ForceAtlas2 over the relations among them, seeded from the scatter (a fixed seed: the same block, the same shape),
    at the cloud's spacing. With no relation among them, the scatter."""
    seed = _scatter(len(members), "|".join(members))
    inside = set(members)
    edges = [(r.src, r.dst) for r in graph.relations if r.src in inside and r.dst in inside and r.src != r.dst]
    if not edges:
        return seed
    g = nx.Graph()
    g.add_nodes_from(members)
    g.add_edges_from(edges)
    at = nx.forceatlas2_layout(g, pos=dict(zip(members, seed, strict=True)), max_iter=FORCE_ROUNDS, seed=FORCE_SEED, linlog=True, strong_gravity=True)
    p = np.array([at[m] for m in members], dtype=float)
    nearest = np.sort(np.linalg.norm(p[:, None] - p[None, :], axis=2), axis=1)[:, 1]
    p *= SPACING * 1.2 / max(float(np.median(nearest)), 1e-9)
    # the force flings what has few ties far out: brought back to the reach of a cloud of this many points
    p -= p.mean(axis=0)
    reach = CLOUD_REACH * SPACING * math.sqrt(len(p))
    away = np.linalg.norm(p, axis=1, keepdims=True)
    p = np.where(away > reach, p * reach / np.maximum(away, 1e-9), p)
    # two points the force left closer than the map allows: pushed apart, the rest stays
    for _ in range(FORCE_ROUNDS):
        moved = False
        for i in range(len(p)):
            for j in range(i + 1, len(p)):
                delta = p[i] - p[j]
                d = float(np.linalg.norm(delta))
                if d < SPACING:
                    step = (delta / d if d > 1e-9 else np.array([1.0, 0.0])) * (SPACING - d) / 2
                    p[i] += step
                    p[j] -= step
                    moved = True
        if not moved:
            break
    return [(float(x), float(y)) for x, y in p]


def _cloud(members: Sequence[str], name: str, px: float, room: int = 0, graph: NodeGraph | None = None,
           splits: dict[str, dict[str, tuple[float, float]]] | None = None) -> tuple[_Box, dict[str, tuple[float, float]]]:
    """A cloud with its title above: its box, and each point from the box's bottom-left (y grows up, as the page draws it). `room`: the points
    drawn across this and other goals, including its further dots when shared."""
    fixed = (splits or {}).get(members[0]) if members else None
    pts = ([fixed[m] for m in members] if fixed is not None else
           _by_relations(members, graph) if graph is not None and len(members) >= room else
           _scatter(max(len(members), room), name + "|" + "|".join(members)))
    pts = pts or [(0.0, 0.0)]
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    w = max(x1 - x0, _title_w(name, px)) + SPACING
    h = y1 - y0 + _head(px) + SPACING
    mid = (x0 + x1) / 2
    return _Box(w, h), {m: (w / 2 + p[0] - mid, SPACING / 2 + p[1] - y0) for m, p in zip(members, pts, strict=False)}


def _picture(name: str, blocks: Sequence[tuple[str, Sequence[str], int]], graph: NodeGraph,
             splits: dict[str, dict[str, tuple[float, float]]] | None = None) -> tuple[_Box, dict[str, tuple[float, float]]]:
    """A top group at its best spacing: its blocks in a compact organic cluster under its title; or, with one block, a cloud under its title."""
    if len(blocks) == 1:
        return _cloud(blocks[0][1], name, GOAL_TITLE, blocks[0][2], graph, splits)
    clouds = [_cloud(members, title, BLOCK_TITLE, room, graph, splits) for title, members, room in blocks]
    # Measure real clouds and titles, seed compact irregular centres, then keep
    # their measured rectangles clear with the same solver as the whole map.
    boxes = [c[0] for c in clouds]
    seed = np.array(_scatter(len(boxes), "|".join(title for title, _, _ in blocks)))
    scale = math.sqrt(sum(b.w * b.h for b in boxes) / len(boxes)) / SPACING
    centres = _push_apart(seed * scale, boxes, gap=BLOCK_GAP)
    left = min(x - b.w / 2 for (x, _), b in zip(centres, boxes, strict=True))
    top = min(y - b.h / 2 for (_, y), b in zip(centres, boxes, strict=True))
    for (x, y), b in zip(centres, boxes, strict=True):
        b.x, b.y = float(x - b.w / 2 - left), float(y - b.h / 2 - top)
    w, h = max(b.x + b.w for b in boxes), max(b.y + b.h for b in boxes)
    pts = {m: (c[0].x + x, h - c[0].y - c[0].h + y) for c in clouds for m, (x, y) in c[1].items()}
    return _Box(max(w, _title_w(name, GOAL_TITLE)), h + _head(GOAL_TITLE)), pts


def _stood(pts: dict[str, tuple[float, float]], name: str) -> tuple[_Box, dict[str, tuple[float, float]], tuple[float, float]]:
    """A top group as it stands: its box, its points from the box's bottom-left, the box's centre on the map."""
    xs, ys = [p[0] for p in pts.values()], [p[1] for p in pts.values()]
    w = max(max(xs) - min(xs), _title_w(name, GOAL_TITLE)) + SPACING
    h = max(ys) - min(ys) + _head(GOAL_TITLE) + SPACING
    cx = (min(xs) + max(xs)) / 2
    y0 = min(ys) - SPACING / 2
    return _Box(w, h), {m: (p[0] - cx + w / 2, p[1] - y0) for m, p in pts.items()}, (cx, y0 + h / 2)


def _targets(dist: np.ndarray, boxes: Sequence[_Box]) -> np.ndarray:
    """The distances to aim for: subject distance stretched over its measured range (it is the order that carries meaning: unrelated
    subjects all stand near the same cosine), the map's size from what the groups take."""
    n = len(boxes)
    off = dist[~np.eye(n, dtype=bool)]
    lo, hi = float(off.min()), float(off.max())
    span = math.sqrt(sum((b.w + TOP_GAP) * (b.h + TOP_GAP) for b in boxes)) * 1.2
    t = (dist - lo) / (hi - lo) if hi > lo else np.ones_like(dist)
    return (0.25 + 0.75 * t) * span


def _places(target: np.ndarray, start: np.ndarray, anchored: np.ndarray) -> np.ndarray:
    """Stress majorization (SMACOF, weights 1/d², so near pairs count most) with an anchor term to `start` where `anchored`."""
    weights = 1.0 / np.maximum(target, 1e-6) ** 2
    np.fill_diagonal(weights, 0.0)
    pull = ANCHOR * weights.sum(axis=1).mean()
    x = start.copy()
    for _ in range(STRESS_ROUNDS):
        delta = x[:, None, :] - x[None, :, :]
        d = np.maximum(np.linalg.norm(delta, axis=2), 1e-6)
        step = (weights[:, :, None] * (x[None, :, :] + target[:, :, None] * delta / d[:, :, None])).sum(axis=1)
        a = pull * anchored
        x = (step + a[:, None] * start) / (weights.sum(axis=1) + a)[:, None]
    return x


def _push_apart(x: np.ndarray, boxes: Sequence[_Box], *, gap: float = TOP_GAP) -> np.ndarray:
    """No two boxes on each other, each pair parted along its shorter overlap."""
    x = x.copy()
    n = len(boxes)
    for _ in range(PUSH_ROUNDS):
        moved = False
        for i in range(n):
            for j in range(i + 1, n):
                ox = (boxes[i].w + boxes[j].w) / 2 + gap - abs(x[i, 0] - x[j, 0])
                oy = (boxes[i].h + boxes[j].h) / 2 + gap - abs(x[i, 1] - x[j, 1])
                if ox > 1e-6 and oy > 1e-6:
                    moved = True
                    axis, over = (0, ox) if ox < oy else (1, oy)
                    sign = 1.0 if x[i, axis] > x[j, axis] or (x[i, axis] == x[j, axis] and i < j) else -1.0
                    x[i, axis] += sign * over / 2
                    x[j, axis] -= sign * over / 2
        if not moved:
            break
    return x


def top_ids(state: WikiState) -> tuple[str, ...]:
    """What the map is arranged for: the top groups and the domains that hold them together."""
    from evowiki.wikiapp.graph_out import groups_out, placed

    return tuple(sorted(g.id for g in top_groups(groups_out(state, placed(state.graph))))) + tuple(sorted(d.id for d in state.domains))


def _region_of(state: WikiState, g: GroupOut) -> MapNode | None:
    """The map's node of a goal's block group."""
    if g.map_id is None or not g.id.startswith(f"block-{g.map_id}-"):
        return None
    return state.maps[g.map_id].node(g.id.removeprefix(f"block-{g.map_id}-"))


def arrange(state: WikiState, embedder: Embedder | None = None, *, fresh: bool = False) -> tuple[NodeGraph, tuple[str, ...]]:
    """The map after a change of its top groups (or the first time): (the graph with its positions, the top groups it was arranged for).
    `fresh`: every group's picture made again, as the current layout makes it (a new layout version), none keeping where it stood."""
    from evowiki.wikiapp.graph_out import groups_out, placed

    graph = placed(state.graph)
    groups = groups_out(state, graph)
    tops = top_groups(groups)
    stored = {r.id: (r.x, r.y) for r in graph.nodes}
    names = {r.id: r.name for r in graph.nodes}
    seen: set[str] = set()
    boxes: list[_Box] = []
    pictures: list[dict[str, tuple[float, float]]] = []
    starts: list[tuple[float, float] | None] = []
    for t in tops:
        kids = [g for g in groups if g.level == 1 and g.parent == t.id]
        blocks = []
        splits: dict[str, dict[str, tuple[float, float]]] = {}
        for g in kids or [t]:
            region = _region_of(state, g)
            members = [m for m in g.members if m not in seen]
            # The hidden hub is part of the same rigid picture as its real children.
            if region is not None and region.state == "expanded" and g.map_id is not None:
                children = [i for i in split_members(state.maps[g.map_id], region)
                            if i in stored and i not in seen]
                if children and region.id in stored and region.id not in seen:
                    members = [region.id, *children]
                    splits[region.id] = {region.id: (0.0, 0.0),
                                         **split_positions((0.0, 0.0), children)}
            # a block all of whose points stand on an earlier goal has its place all the same: its own node marks it
            if not members and region is not None and graph.node(region.id) is not None:
                members = [region.id]
            seen.update(members)
            # its further dots (points that stand on an earlier goal) stand round it too: the block has room for all of them
            room = len(g.members)
            blocks.append((g.name, members, room))
        blocks = [b for b in blocks if b[1]]
        mine = [m for _, ms, _ in blocks for m in ms]
        if not fresh and t.id in state.graph_arranged and mine:
            box, pts, at = _stood({m: stored[m] for m in mine}, t.name)
            starts.append(at)
        else:
            box, pts = _picture(t.name, blocks, graph, splits) if blocks else (_Box(_title_w(t.name, GOAL_TITLE), _head(GOAL_TITLE)), {})
            starts.append(None)
        boxes.append(box)
        pictures.append(pts)
    at: dict[str, tuple[float, float]] = {}
    if tops:
        if embedder is not None and len(tops) > 1:
            v = _vectors(embedder, [group_text(g, names) for g in tops])
            dist = np.clip(1.0 - v @ v.T, 0.0, 2.0)
        else:
            dist = np.ones((len(tops), len(tops)))
        np.fill_diagonal(dist, 0.0)
        # the groups of one domain stand next to each other
        place_of = {t.id: i for i, t in enumerate(tops)}
        for d in state.domains:
            kids = [place_of[k] for k in d.kids if k in place_of]
            for i in kids:
                for j in kids:
                    dist[i, j] *= DOMAIN_PULL
        target = _targets(dist, boxes) if len(tops) > 1 else np.zeros((1, 1))
        np.fill_diagonal(target, 0.0)
        start = np.zeros((len(tops), 2))
        known = [i for i, s_ in enumerate(starts) if s_ is not None]
        for i, s_ in enumerate(starts):
            if s_ is not None:
                start[i] = s_
            else:
                # a new group starts beside what it is most like; with nothing to go by, on a spiral
                near = min(known, key=lambda j: (dist[i, j], j)) if known else None
                a, r = i * GOLDEN, float(target[i].mean()) if len(tops) > 1 else 0.0
                base = start[near] if near is not None else np.zeros(2)
                start[i] = base + r * np.array([math.cos(a), math.sin(a)])
        anchored = np.array([1.0 if s_ is not None else 0.0 for s_ in starts])
        x = _push_apart(_places(target, start, anchored) if len(tops) > 1 else start, boxes)
        for i, pts in enumerate(pictures):
            for m, (px, py) in pts.items():
                at[m] = (round(float(x[i, 0] - boxes[i].w / 2 + px), 6), round(float(x[i, 1] - boxes[i].h / 2 + py), 6))
    # what is not drawn (a container) stands beside what it holds
    nodes = tuple(r.model_copy(update=dict(zip(("x", "y"), at.get(r.id, (None, None)), strict=True))) for r in graph.nodes)
    return place_missing(graph.model_copy(update={"nodes": nodes})), top_ids(state)


def copy_places(state: WikiState, graph: NodeGraph) -> dict[str, dict[str, tuple[float, float]]]:
    """By block group id: where a point's further dots stand in it, point id → world position. `graph`: `placed`."""
    pos = {r.id: (r.x, r.y) for r in graph.nodes if r.x is not None and r.y is not None}
    taken = list(pos.values())
    first: set[str] = set()
    out: dict[str, dict[str, tuple[float, float]]] = {}
    for goal in goal_groups(state):
        for b in goal.blocks:
            mine = [m for m in b.members if m not in first]
            copies = sorted(m for m in b.members if m in first)
            first.update(mine)
            if not copies:
                continue
            region = pos.get(b.id.split(f"block-{goal.map_id}-", 1)[1])
            anchors = [pos[m] for m in mine] or ([region] if region is not None else [pos[copies[0]]])
            cx, cy = sum(p[0] for p in anchors) / len(anchors), sum(p[1] for p in anchors) / len(anchors)
            out[b.id] = {}
            for m in copies:
                out[b.id][m] = _dart(cx, cy, taken, SPACING, _rng(f"{m} {b.id}"))
                taken.append(out[b.id][m])
    return out


def needs_arranging(state: WikiState) -> bool:
    return bool(state.graph.nodes) and top_ids(state) != state.graph_arranged


def relayout_body(state: WikiState, embedder: Embedder | None = None) -> GraphSaved:
    graph, arranged = arrange(state, embedder, fresh=needs_relayout(state))
    return GraphSaved(graph=graph, action="relayout", layout=LAYOUT_VERSION, arranged=arranged)


def ensure_layout(repo: Repo, snap: Snapshot, *, now: datetime, new_id: Ids, embedder: Embedder | None = None) -> Snapshot:
    """`snap` after the one-time relayout when it needs one (the committed snapshot), else `snap`."""
    if not needs_relayout(snap.state) and not needs_arranging(snap.state):
        return snap
    body = relayout_body(snap.state, embedder)
    done = repo.commit(snap, Commit(wiki=[WikiEvent(event_id=new_id("wev"), ts=now, body=body)]))
    log.info("node graph of %s laid out again (layout %d -> %d, %d nodes)", snap.user_id,
             snap.state.graph_layout, LAYOUT_VERSION, len(body.graph.nodes))
    return done
