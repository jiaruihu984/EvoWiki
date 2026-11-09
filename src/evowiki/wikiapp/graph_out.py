"""GET /wiki/graph (plan 2026-10-13-unified-node-graph §4): the whole node graph as the page draws
it. Pure, 0 LLM calls.

- Nodes: evidence from K (`nodes.evidence.node_evidence`) over the bands of each node's own cells
  (`direct`) and of all its part_of descendants' cells (`aggregate`), both kept; `mix` / `band` weight
  direct cells 2:1. A part with no cells gives `{}`. `evidence`: the K notes of the node's own cells
  as plain lines (a note's display label when it has one, else a sentence built from its kind:
  「答了一道题，选了「…」」, 「你说：我会」, …; 清除声明 and notes of unknown kinds are left out;
  each line once). `kind`: the phrase of the node's first kind_of relation, or 「一种{parent}」 when
  the phrase is empty. A node without a stored position is placed here with `nodes.place_missing`
  (deterministic; nothing is stored — the graph's own writers store positions).
- Groups: inside a goal its real blocks (`goal_groups`: level 2 the goal, level 1 its blocks, with their real names; a
  point on two goals is a member of a block of each). The points on no goal are clustered (`nodes.cluster`), each group's
  name the stored one for the exact member set (`state.groups[member_key]`), else one inherited from a named group of
  the same level whose members are >= INHERIT_JACCARD similar (`inherited_name`), else a readable
  fallback: the two most central members (most relations inside the group; ties by name, then id)
  joined by 、, plus 「等」 when the group has more members; empty summary. x, y = the centroid of
  the members.
- Goals: one per goal map, in map order. `nodes`: the map's node ids that are graph nodes; `shares`:
  the band of each of those nodes (`nodes.evidence.shares`). `path_id`: the last planned
  (`state.path_order`) path among the map's nodes (the one the learner is on), else None. `path`:
  `paths.path_items` of it, i.e. the order the learner sees, with their edits. `reading`: a 读懂
  goal's units and progress (`reading_view.reading_out`; reading-mode spec §5)."""

from collections.abc import Mapping, Sequence
from datetime import datetime

from evowiki.belief.aspects import AspectK, AspectKView, NoteEntry
from evowiki.goals.contracts import cell_id
from evowiki.goals.regions import goal_name
from evowiki.nodes import Group, NodeGraph, cluster, member_key, node_evidence, place_missing
from evowiki.nodes import shares as share_of
from evowiki.wikiapp.events import GroupName
from evowiki.wikiapp.goal_groups import goal_groups, goal_held, listed_maps, outside_goals, path_of
from evowiki.wikiapp.node_state import grid_known, grid_missed
from evowiki.wikiapp.page_run_store import PageRunRow
from evowiki.wikiapp.paths import path_items
from evowiki.wikiapp.reading_view import reading_out
from evowiki.wikiapp.source_view import SourceInfo
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.views import NOTE_KINDS
from evowiki.wikiapp.wiki_schemas import (
    GoalOut,
    GoalPathOut,
    GraphNodeOut,
    GraphOut,
    GroupOut,
    PathItemOut,
    RelationOut,
)

INHERIT_JACCARD = 0.6
DECLARE_LINE = {"know": "你说：我会", "dont": "你说：我不会"}
REVIEW_LINE = {"recalled": "复习：想起来了", "fuzzy": "复习：有点模糊", "forgot": "复习：没想起来"}


def evidence_line(note: NoteEntry) -> str | None:
    """One K note as a plain line for the node card; None = not shown."""
    if note.label and note.label.strip():
        return note.label.strip()
    text = note.text.strip()
    match NOTE_KINDS.get(note.evidence_ref.partition(":")[0]):
        case "self":
            return f"你说：“{text}”"
        case "quiz" | "check":
            return f"答了一道题，选了「{text}」"
        case "declare":
            return None if text == "clear" else DECLARE_LINE.get(text, f"你说：{text}")
        case "review":
            return REVIEW_LINE.get(text, f"复习：{text}")
        case "feedback":
            return f"段落反馈：{text}"
        case "ask":
            return f"你的提问：{text}"
        case _:
            return None


def evidence_lines(k: AspectK, node_id: str) -> list[str]:
    if node_id not in k.concept_ids():
        return []
    lines = (evidence_line(n) for n in k.notes(node_id))
    return list(dict.fromkeys(x for x in lines if x))


def _shares(bands_mix: Mapping[str, float], n: int) -> dict[str, float]:
    return dict(bands_mix) if n else {}


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def inherited_name(members: Sequence[str], level: int,
                   names: Mapping[str, GroupName]) -> GroupName | None:
    """The stored name of this exact member set, else the name of the most similar named group of
    the same level (Jaccard >= INHERIT_JACCARD; ties: the smaller key), else None."""
    exact = names.get(member_key(members))
    if exact is not None:
        return exact
    best: tuple[float, str] | None = None
    for key, g in names.items():
        if g.level != level or not g.members:
            continue
        j = jaccard(members, g.members)
        if j >= INHERIT_JACCARD and (best is None or (-j, key) < (-best[0], best[1])):
            best = (j, key)
    return names[best[1]] if best is not None else None


def fallback_name(graph: NodeGraph, members: Sequence[str]) -> str:
    """The two most central members (relations inside the group), 、-joined, 「等」 when more."""
    inside = set(members)
    degree = dict.fromkeys(members, 0)
    for r in graph.relations:
        if r.src in inside and r.dst in inside:
            degree[r.src] += 1
            degree[r.dst] += 1

    def name(i: str) -> str:
        rec = graph.node(i)
        return rec.name if rec is not None else i

    top = sorted(members, key=lambda i: (-degree[i], name(i), i))[:2]
    return "、".join(name(i) for i in top) + ("等" if len(members) > 2 else "")


def group_label(graph: NodeGraph, g: Group, names: Mapping[str, GroupName]) -> tuple[str, str, bool]:
    """(name, summary, named): the stored / inherited name, else the fallback and ""."""
    known = inherited_name(g.members, g.level, names)
    if known is not None:
        return known.name, known.summary, True
    return fallback_name(graph, g.members), "", False


def placed(graph: NodeGraph) -> NodeGraph:
    """Every node with a position (missing ones placed deterministically, not stored)."""
    if all(r.x is not None and r.y is not None for r in graph.nodes):
        return graph
    return place_missing(graph)


def groups_out(state: WikiState, graph: NodeGraph) -> list[GroupOut]:
    """The groups of the graph as served: a goal and its blocks, the clusters of the rest. `graph`: `placed`."""
    pos = {r.id: (r.x, r.y) for r in graph.nodes}

    def centre(members: Sequence[str]) -> dict[str, float]:
        return {"x": round(sum(pos[m][0] for m in members) / len(members), 6),
                "y": round(sum(pos[m][1] for m in members) / len(members), 6)}

    from evowiki.wikiapp.graph_layout import copy_places

    further = copy_places(state, graph)
    clusters = cluster(outside_goals(state))
    # a cluster all of whose ties reach one goal stands under it (`goal_held`)
    tops = [g for g in clusters if g.level == 2 or g.parent is None]
    held = {t.id: goal_held(state, t.members) for t in tops}
    under = {g.id: held.get(g.parent or g.id) for g in clusters if g.level == 1}
    groups: list[GroupOut] = []
    for goal in goal_groups(state):
        extra = [g for g in clusters if g.level == 1 and under[g.id] == goal.id]
        members = list(dict.fromkeys([*goal.members, *(m for g in extra for m in g.members)]))
        groups.append(GroupOut(id=goal.id, level=2, name=goal.name, summary="", members=members, parent=None,
                               map_id=goal.map_id, **centre(members)))
        groups += [GroupOut(id=b.id, level=1, name=b.name, summary=b.summary, members=list(b.members), parent=goal.id,
                            map_id=goal.map_id, copies=further.get(b.id, {}), **centre(b.members)) for b in goal.blocks]
        for g in extra:
            name, summary, _ = group_label(graph, g, state.groups)
            groups.append(GroupOut(id=g.id, level=1, name=name, summary=summary, members=list(g.members), parent=goal.id,
                                   map_id=goal.map_id, **centre(g.members)))
    for g in clusters:
        if (under[g.id] if g.level == 1 else held[g.id]) is not None:
            continue
        name, summary, _ = group_label(graph, g, state.groups)
        groups.append(GroupOut(id=g.id, level=g.level, name=name, summary=summary, members=list(g.members),
                               parent=g.parent, **centre(g.members)))
    return groups


def left_behind(state: WikiState, graph: NodeGraph) -> set[str]:
    """The points left when a block was deleted (`dropped_points`, with what was split out of them) that no listed goal and no page outside the
    trash uses now. A point just made or just split is not one of them: it stays in the picture."""
    out = set(state.dropped_points)
    for i in state.dropped_points:
        if graph.node(i) is not None:
            out.update(graph.descendants(i))
    out -= {n.id for m in listed_maps(state) for n in m.nodes}
    out -= {i for g in goal_groups(state) for i in g.members}
    for pid, rec in state.pages.items():
        if pid not in state.trashed_pages:
            out.difference_update(rec.concepts, rec.targets)
    return out


def graph_out(state: WikiState, k: AspectK, now: datetime, *,
              source_info: SourceInfo | None = None,
              runs: Mapping[str, PageRunRow] = {}) -> GraphOut:
    graph = placed(state.graph)
    kv = AspectKView(k, now)
    cells: dict[str, list[str]] = {}

    def bands_of(node_id: str) -> list[str]:
        if node_id not in cells:
            cells[node_id] = [kv.cell(cell_id(node_id, a)).band.value
                              for a in kv.aspects(node_id)]
        return cells[node_id]

    nodes: list[GraphNodeOut] = []
    band: dict[str, str] = {}
    behind = left_behind(state, graph)
    grid = {b for m in state.maps.values() for b in grid_known(state, m)}
    missed = {b for m in state.maps.values() for b in grid_missed(state, m, kv)}
    for rec in graph.nodes:
        ev = node_evidence(graph, rec.id, bands_of)
        if rec.id in grid:                    # as 学习路径 shows it (`maps.node_out`)
            whole = {b: float(b == "known") for b in ev.mix}
            ev = ev.model_copy(update={"band": "known", "mix": whole})
        elif rec.id in missed:
            ev = ev.model_copy(update={"band": "gap", "mix": {b: float(b == "gap") for b in ev.mix}})
        n_direct = len(bands_of(rec.id))
        n_agg = sum(len(bands_of(d)) for d in graph.descendants(rec.id))
        kinds = [r for r in graph.relations if r.kind == "kind_of" and r.src == rec.id]
        kind = ""
        if kinds:
            parent = graph.node(kinds[0].dst)
            kind = kinds[0].phrase or (f"一种{parent.name}" if parent is not None else "")
        band[rec.id] = ev.band
        assert rec.x is not None and rec.y is not None
        lines = evidence_lines(k, rec.id)
        # a deleted block's point nothing is known about stays in the shared graph, out of the picture; back on a goal it shows again
        if rec.id in behind and ev.band == "none" and not lines:
            continue
        nodes.append(GraphNodeOut(
            id=rec.id, name=rec.name, definition=rec.definition, band=ev.band,
            mix=_shares(ev.mix, n_direct + n_agg), direct=_shares(ev.direct, n_direct),
            aggregate=_shares(ev.aggregate, n_agg), parents=list(graph.parents(rec.id)),
            evidence=lines, kind=kind, x=rec.x, y=rec.y,
            can_split=not graph.has_children(rec.id)))

    served = {n.id for n in nodes}
    for n in nodes:
        n.parents = [q for q in n.parents if q in served]
    groups = [g.model_copy(update={"members": [m for m in g.members if m in served]}) for g in groups_out(state, graph)]
    groups = [g for g in groups if g.members or g.map_id is not None]

    info = source_info or SourceInfo()
    goals: list[GoalOut] = []
    for m in listed_maps(state):
        mid = m.map_id
        subset = list(dict.fromkeys(n.id for n in m.nodes if n.id in band))
        pid = path_of(state, m)
        reading = state.reading_of(mid)

        def items(path_id: str) -> list[PathItemOut]:
            return path_items(state, state.paths[path_id], k, now, section_labels=info.labels_by_node,
                              exam_counts=info.exam_counts, runs=runs)

        goals.append(GoalOut(
            map_id=mid, goal=m.goal, name=goal_name(m), nodes=subset,
            shares=share_of([band[i] for i in subset]), path_id=pid,
            path=items(pid) if pid is not None else [],
            paths=[GoalPathOut(path_id=n.path_id, name=n.name, path=items(n.path_id)) for n in m.nodes
                   if n.path_id is not None and n.path_id in state.paths],
            sources=info.goals.get(mid, info.goals.get(pid, []) if pid is not None else []),
            reading=(reading_out(state, kv, reading,
                                 labels=info.section_labels.get(reading.source_id, {}))
                     if reading is not None else None)))

    return GraphOut(
        nodes=nodes,
        relations=[RelationOut(kind=r.kind, src=r.src, dst=r.dst, phrase=r.phrase,
                               verified=r.verified,
                               split=r.kind == "part_of" and r.source == "split")
                   for r in graph.relations if r.src in served and r.dst in served],
        groups=groups, goals=goals)
