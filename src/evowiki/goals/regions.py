"""Goal map (big-goal design §Contracts): a big goal as regions that the user enters one at a time.

A region IS a curator topic (big-goal reuse ruling 2026-10-11): its `id` is the topic's (hub) id —
the same on every map and in the catalog's topics — and its `members` are its knowledge points (K
concepts), fixed when the goal is created. A region with no topic (the curator's guardrails refused
one) keeps a goal-local id. Its colour comes from its members' evidence. A region shows collapsed
until the user enters it; then its members become concept nodes under it. Older maps' regions are
K concepts with no members; they stay readable. Whether a block is known is never stored: it is
read from K every time (`block_known`, user ruling 2026-10-28 「整块已会只按 K 算」). Pure data and
ordering, no IO, no LLM."""

import re
from collections.abc import Collection, Sequence
from typing import Literal

import networkx as nx
from pydantic import AwareDatetime, BaseModel, ConfigDict

from evowiki.goals.contracts import KView, cell_id
from evowiki.goals.path import LEARNED_BANDS

NodeState = Literal["collapsed", "expanded"]
GOAL_ASK = re.compile(r"^(?:我?[要想]?学(?:习|点)?|(?i:(?:I (?:want|would like) to )?(?:learn|study|master))\b)\s*")


class MapNode(BaseModel):
    """`prereqs`: same-level edges (siblings under the same parent), acyclic. A node starts
    `collapsed` and becomes `expanded` once this goal entered it (whether a block not entered is
    known is K's call: `block_known`); a concept node of an older map is always `expanded`.
    Whether a node can be split or learned is not its `kind` (wikiapp/node_state, spec
    2026-10-28 §2.2). `path_id` / `page_id`: what opening it produced — on any node (a point's
    path; an older map's region path). `members`: a region's knowledge points (concept ids).
    `reused`: the wiki already had it before this goal (a topic or a concept; same id everywhere:
    one node for one thing). `level`: a chat-first block's level word (基础 / 核心 / 进阶 / 拓展,
    goals/blocks.LEVELS; "" = none: a block added without one, every other node).
    The goal record per block (S1, D09; written when the goal is made): `diagnose` — whether the
    diagnosis may ask about it (False: decided *teach*, never asked, also when it is entered);
    `said` — the learner's own words about it, each quote verbatim (() = none). Its covers are
    `scope` (joined by 、)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Literal["region", "concept"]
    name: str
    parent: str | None = None
    scope: str = ""
    prereqs: tuple[str, ...] = ()
    target: bool = False
    state: NodeState = "collapsed"
    path_id: str | None = None
    page_id: str | None = None
    reused: bool = False
    members: tuple[str, ...] = ()
    level: str = ""
    diagnose: bool = True
    said: tuple[str, ...] = ()


class GoalMap(BaseModel):
    """A goal map. The goal record (S1, D09): `words` — the learner's own words the goal was made
    from (the goal first), written once; `limits` — his conditions for this goal (「尽快读到第一页」),
    from those words, editable in chat; `beginner` — his words saying he starts from zero (D07;
    "" = he did not say so): no block of this goal is diagnosed, also one added after the goal
    was made (map_edits' 加一块)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    map_id: str
    goal: str
    created_at: AwareDatetime
    title: str = ""        # intake's short title: the goal's topic name when no node is marked target
    nodes: tuple[MapNode, ...]
    log: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()               # materials the goal came from (source ids)
    words: tuple[str, ...] = ()
    limits: tuple[str, ...] = ()
    beginner: str = ""
    # how this goal uses its materials (spec 2026-10-28 §2.5; "" = no material, or an older map):
    # reference / follow / understand — the materials' own use rows are kept apart (source_use)
    material_use: Literal["", "reference", "follow", "understand"] = ""

    def node(self, node_id: str) -> MapNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def children(self, node_id: str | None) -> tuple[MapNode, ...]:
        return tuple(n for n in self.nodes if n.parent == node_id)

    def descendants(self, node_id: str) -> tuple[MapNode, ...]:
        out: list[MapNode] = []
        for c in self.children(node_id):
            out += [c, *self.descendants(c.id)]
        return tuple(out)

    def replace(self, *nodes: MapNode) -> "GoalMap":
        """The map with these nodes swapped in by id (unknown ids appended, in order)."""
        new = {n.id: n for n in nodes}
        kept = tuple(new.pop(n.id, n) for n in self.nodes)
        return self.model_copy(update={"nodes": (*kept, *new.values())})


def goal_name(m: GoalMap) -> str:
    """The goal's topic name: intake's short title, else the learner's sentence without its 我要学
    (not a target node: that is one of the goal's blocks, 「鱼缸与水质」 for 我要学孔雀鱼)."""
    return m.title.strip() or GOAL_ASK.sub("", m.goal.strip()) or m.goal


def ordered(nodes: Sequence[MapNode]) -> list[MapNode]:
    """Siblings in prerequisite topological order (ties: the given order). Edges to nodes not in
    `nodes` are ignored; the stored graph is acyclic (intake drops cycles)."""
    pos = {n.id: i for i, n in enumerate(nodes)}
    g = nx.DiGraph()
    g.add_nodes_from(pos)
    g.add_edges_from((p, n.id) for n in nodes for p in n.prereqs if p in pos and p != n.id)
    by = {n.id: n for n in nodes}
    return [by[i] for i in nx.lexicographical_topological_sort(g, key=lambda i: pos[i])]


def region_order(m: GoalMap, parent: str | None = None) -> list[MapNode]:
    """Every region, depth first: siblings in prerequisite order, an expanded region's
    sub-regions right after it."""
    out: list[MapNode] = []
    for n in ordered([c for c in m.children(parent) if c.kind == "region"]):
        out += [n, *region_order(m, n.id)]
    return out


def _begun(m: GoalMap, n: MapNode) -> bool:
    """This goal has started on the node: it has a path / page, or it was entered and is not an
    older map's concept (always `expanded`) — a part entered, opened, or one of its parts begun."""
    if n.path_id is not None or n.page_id is not None:
        return True
    if n.state != "expanded":
        return False
    kids = m.children(n.id)
    return n.kind == "region" or bool(kids) and any(_begun(m, c) for c in kids)


def next_region(m: GoalMap, k: KView, taught: Collection[str] = (), known: Collection[str] = ()) -> str | None:
    """The node to enter next: the first top-level block (prerequisite order) not done — known
    (`block_known`), or with a path or page — and inside an entered one, the part in progress
    (begun, not done), at any depth. An entered node with parts left and none in progress is
    itself the answer: it waits for the learner to pick a part (spec 2026-10-28 §4: never the
    first part by itself); entering it again only shows its parts. Once a part has its page, the
    next part not done is the answer. `taught`: nodes another
    node's path teaches (wikiapp/node_state.taught_on_paths) — not entered, they are no step. `known`: blocks
    the opening diagnosis showed he knows (wikiapp/node_state.grid_known): skipped as K-known ones."""
    def done(n: MapNode) -> bool:
        return (n.path_id is not None or n.page_id is not None or n.id in known or block_known(m, n, k)
                or (n.id in taught and n.state == "collapsed"))

    def inside(n: MapNode) -> str | None:
        if n.state != "expanded":
            return n.id
        if not m.children(n.id):
            # an older map's member (always expanded) is learned on its region's path
            return None if n.kind == "concept" else n.id
        parts = [c for c in ordered(list(m.children(n.id))) if not done(c)]
        if not parts:
            return None
        for c in parts:
            if _begun(m, c) and (found := inside(c)) is not None:
                return found
        if any(c.path_id is not None or c.page_id is not None for c in m.children(n.id)):
            return inside(parts[0])     # a part is done: going on is the next one
        return n.id

    for n in ordered([c for c in m.children(None) if c.kind == "region"]):
        if not done(n) and (found := inside(n)) is not None:
            return found
    return None


def covered(m: GoalMap, n: MapNode) -> tuple[str, ...]:
    """The concept ids whose cells a node stands for: its own id, its members and every
    descendant on the map (a point split further covers its parts too; a topic id has no cells,
    so callers that read cells simply find none for it)."""
    ids = [n.id, *n.members, *(d.id for d in m.descendants(n.id))]
    return tuple(dict.fromkeys(ids))


def block_known(m: GoalMap, n: MapNode, k: KView) -> bool:
    """Any node not entered yet — collapsed, no path, no page — whose every cell K has
    (`covered`: its own, its members', its descendants') is likely or known: it is skipped.
    Read from K every time, never stored (user ruling 2026-10-28): a cell K drops (a wrong
    answer, forgetting) makes it one to learn again. No cells: cannot tell → False. An entered
    node's progress is its path / page / parts' (wikiapp/progress) — unless none of its parts has
    begun and its own cells are learned: he said 这一块我会了 after it was split."""
    if n.path_id or n.page_id:
        return False
    if n.state == "collapsed":
        points = covered(m, n)
    elif n.kind == "region" and not any(_begun(m, c) for c in m.children(n.id)):
        points = (n.id,)
    else:
        return False
    cells = [cell_id(c, a) for c in points for a in k.aspects(c)]
    return bool(cells) and all(k.cell(x).band in LEARNED_BANDS for x in cells)
