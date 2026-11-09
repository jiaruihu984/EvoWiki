"""The 知识图谱's groups inside a goal (decision 2026-11-04): a goal's points group by its real blocks, as 学点新的 lists
them; only the points on no goal are clustered (`nodes.cluster`). Pure, 0 LLM calls.

A knowledge point keeps one id and one mastery state, and may stand on several goals: it is a member of a block of each.
Inside one goal it stands once: where the map has it (the block 学点新的 shows it in); a graph descendant that is not on
the map stands under the block of its nearest ancestor that is (a split child, a reading unit's parts). A region's own node
is the picture of what it contains: it is a member only when it contains nothing yet.

A goal taken back (`goal_settings.archived`) is not listed: its points go back to the clustering."""

from collections.abc import Iterable
from dataclasses import dataclass

from evowiki.goals.regions import GoalMap, MapNode, goal_name, ordered
from evowiki.nodes import NodeGraph
from evowiki.wikiapp.state import WikiState


@dataclass(frozen=True)
class BlockGroup:
    """One block of a goal as a level-1 group."""

    id: str
    name: str
    summary: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class GoalGroup:
    """One goal as a level-2 group over its blocks."""

    id: str
    map_id: str
    name: str
    blocks: tuple[BlockGroup, ...]

    @property
    def members(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(m for b in self.blocks for m in b.members))


def path_of(state: WikiState, m: GoalMap) -> str | None:
    """The last planned (`state.path_order`) path among the map's nodes (the one the learner is on), else None."""
    order = {pid: i for i, pid in enumerate(state.path_order)}
    mine = {n.path_id for n in m.nodes if n.path_id is not None and n.path_id in state.paths}
    return max(mine, key=lambda p: (order.get(p, -1), p)) if mine else None


def listed_maps(state: WikiState) -> list[GoalMap]:
    """The goals the learner sees, in map order: all but those taken back."""
    def taken_back(m: GoalMap) -> bool:
        return any(s is not None and s.archived
                   for s in (state.goal_settings.get(m.map_id), state.goal_settings.get(path_of(state, m) or "")))

    return [m for m in (state.maps[mid] for mid in state.map_order) if not taken_back(m)]


def top_block(m: GoalMap, n: MapNode) -> MapNode:
    """The top-level block a map node is in: up its parents, or the region whose `members` list it."""
    seen = {n.id}
    while True:
        holder = m.node(n.parent) if n.parent is not None else None
        holder = holder or next((r for r in m.nodes if n.id in r.members), None)
        if holder is None or holder.id in seen:
            return n
        seen.add(holder.id)
        n = holder


def _block_points(graph: NodeGraph, m: GoalMap) -> list[tuple[MapNode, list[str]]]:
    """Each top-level block in prerequisite order with the points drawn in it."""
    tops = ordered([n for n in m.nodes if top_block(m, n).id == n.id])
    on_map = {n.id for n in m.nodes}
    points: dict[str, list[str]] = {t.id: [] for t in tops}
    claimed: set[str] = set()

    def claim(top: str, i: str) -> None:
        if graph.node(i) is not None and i not in claimed:
            claimed.add(i)
            points[top].append(i)

    for n in m.nodes:
        top = top_block(m, n).id
        for i in (n.id, *n.members):
            claim(top, i)
    for t in tops:               # breadth first: a point is under the block of its nearest ancestor on the map
        todo = list(points[t.id])
        while todo:
            for c in graph.children(todo.pop(0)):
                if c not in on_map and c not in claimed:
                    claim(t.id, c)
                    todo.append(c)
    def holds(n: MapNode, mine: set[str]) -> bool:
        return any(c in mine for c in (*graph.children(n.id), *n.members)) or any(c.id in mine for c in m.children(n.id))

    out = []
    for t in tops:
        mine = set(points[t.id])
        containers = {n.id for n in m.nodes if n.kind == "region" and holds(n, mine)}
        if mine:
            out.append((t, [i for i in points[t.id] if i not in containers] or points[t.id]))
    return out


def goal_groups(state: WikiState) -> list[GoalGroup]:
    """Every listed goal with points on the graph, as its group and its blocks."""
    out = []
    for m in listed_maps(state):
        blocks = tuple(BlockGroup(id=f"block-{m.map_id}-{t.id}", name=t.name, summary=t.scope, members=tuple(points))
                       for t, points in _block_points(state.graph, m))
        if blocks:
            out.append(GoalGroup(id=f"goal-{m.map_id}", map_id=m.map_id, name=goal_name(m), blocks=blocks))
    return out


def goal_held(state: WikiState, members: Iterable[str]) -> str | None:
    """The goal group a cluster of points on no goal belongs under: all its relations out of the cluster reach that one goal
    (黑壳虾's 蜕皮, 藻类): a block of its own there with its own name, not a second subject beside the goal. None: none, or several."""
    inside = set(members)
    held: set[str] = set()
    of = [(g.id, set(g.members)) for g in goal_groups(state)]
    for r in state.graph.relations:
        if (r.src in inside) == (r.dst in inside):
            continue
        out = r.dst if r.src in inside else r.src
        held |= {gid for gid, ms in of if out in ms}
    return held.pop() if len(held) == 1 else None


def outside_goals(state: WikiState) -> NodeGraph:
    """The graph of the points on no listed goal: what is clustered. A relation to a goal's point is left out."""
    graph = state.graph
    taken = {i for g in goal_groups(state) for i in g.members}
    taken |= {n.id for m in listed_maps(state) for n in m.nodes}      # a region's own node: its block is the picture of it
    # the topic above a goal's points (黑壳虾 over its blocks) is that goal's subject, not another group beside it
    todo = list(taken)
    while todo:
        for p in graph.parents(todo.pop()):
            if p not in taken:
                taken.add(p)
                todo.append(p)
    return NodeGraph(nodes=tuple(r for r in graph.nodes if r.id not in taken),
                     relations=tuple(r for r in graph.relations if r.src not in taken and r.dst not in taken))
