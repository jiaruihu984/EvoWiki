"""What the wire shows of a user's materials (materials task 7): the materials of a page, the §
labels of path rows and the sections a goal's materials leave uncovered. A 读懂 goal (reading-mode
spec §5): a point a unit teaches is labelled 「在《T》的第 N 段里读」 for the section that unit
reads (not 「§3.2」), and the sections its units read (a skipped one's not) count as covered. The
readers take an open connection and raise on a database error (fail closed: never a result
missing its material info); `graph_out` / `path_items` stay pure and receive the result."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

import psycopg

from evowiki.goals.regions import GoalMap
from evowiki.sources import labels, store
from evowiki.sources.exam_store import exam_counts
from evowiki.wikiapp.events import PageRecord
from evowiki.wikiapp.path_order import shown
from evowiki.wikiapp.reading_view import reading_path_id
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import GoalSourceOut, PageSourceOut


@dataclass(frozen=True)
class SourceInfo:
    """`labels_by_node`: node id -> 「§3.2」 labels (「讲义 §3.2」 when its goal has several
    materials); `goals`: map id and path id -> that goal's materials."""

    labels_by_node: dict[str, list[str]] = field(default_factory=dict)
    goals: dict[str, list[GoalSourceOut]] = field(default_factory=dict)
    exam_counts: dict[str, int] = field(default_factory=dict)   # node id -> exam items (常考)
    # source id -> section id -> 「§3.2」 (a 读懂 goal's unit locations, reading_view.reading_out)
    section_labels: dict[str, dict[str, str]] = field(default_factory=dict)


def read_in(title: str, order: int) -> str:
    """A section a 读懂 goal's unit reads, as its label shows (reading-mode spec §5)."""
    return f"在《{title}》的第 {order} 段里读"


def _read_at(state: WikiState) -> dict[tuple[str, str, str], str]:
    """(node, material, section) -> 「在《T》的第 N 段里读」 for each point a unit teaches from a
    section it reads (the unit its `source_link(by='reading')` came from)."""
    out: dict[tuple[str, str, str], str] = {}
    for r in state.readings.values():
        for u in r.units:
            for t in u.teaches:
                for sp in u.spans:
                    out.setdefault((t, r.source_id, sp.section_id), read_in(r.title, u.order))
    return out


def _read_sections(state: WikiState, map_id: str) -> set[tuple[str, str]]:
    """(material, section) the units of goal `map_id`'s reading read (a skipped unit's are not)."""
    r = state.reading_of(map_id)
    if r is None:
        return set()
    return {(r.source_id, sp.section_id) for u in r.units if not u.skipped for sp in u.spans}


def reading_labels(conn: psycopg.Connection, user_id: str, state: WikiState,
                   map_id: str) -> dict[str, str]:
    """section_id -> 「§3.2」 of goal `map_id`'s reading material ({} for a goal with no reading
    or a deleted material): its units' locations on GET /wiki/maps."""
    r = state.reading_of(map_id)
    if r is None:
        return {}
    row = store.get_source(conn, user_id, r.source_id)
    if row is None or row.deleted_at is not None:
        return {}
    return labels.section_names(store.sections(conn, user_id, r.source_id))


def page_sources_out(conn: psycopg.Connection, user_id: str,
                     record: PageRecord) -> list[PageSourceOut]:
    """The materials the page was written from; a deleted one stays listed with `deleted`."""
    out = []
    for ps in record.sources:
        row = store.get_source(conn, user_id, ps.source_id)
        if row is None:
            continue
        out.append(PageSourceOut(id=ps.source_id, title=ps.title, current_title=row.title,
                                 kind=row.kind, origin=row.origin,
                                 deleted=row.deleted_at is not None))
    return out


def _goal_nodes(state: WikiState, m: GoalMap) -> set[str]:
    """The map's nodes, its regions' members and the nodes of the pages of its paths."""
    out = {n.id for n in m.nodes}
    for n in m.nodes:
        out.update(n.members)
        rec = state.paths.get(n.path_id) if n.path_id is not None else None
        if rec is not None:
            out.update(i for it in shown(rec) for i in it.node_ids)
    return out


def read_source_info(conn: psycopg.Connection, user_id: str, state: WikiState) -> SourceInfo:
    """Labels for every node of every goal's path and the coverage of every goal's materials,
    and the exam item counts per node."""
    counts = exam_counts(conn, user_id)
    goal_nodes = {mid: _goal_nodes(state, m) for mid, m in state.maps.items()}
    nodes: set[str] = set().union(*goal_nodes.values())
    for rec in state.paths.values():
        nodes.update(i for it in shown(rec) for i in it.node_ids)
    wanted = {s for m in state.maps.values() for s in m.sources}
    wanted.update(s for rec in state.paths.values() for s in rec.sources)
    if not wanted:
        return SourceInfo(exam_counts=counts)
    rows = {sid: store.get_source(conn, user_id, sid) for sid in wanted}
    links = store.links_for_nodes(conn, user_id, sorted(nodes))
    secs = {sid: store.sections(conn, user_id, sid) for sid, r in rows.items()
            if r is not None and r.deleted_at is None}
    titles = {sid: r.title for sid, r in rows.items() if r is not None}
    sec_labels = {sid: labels.section_labels(s) for sid, s in secs.items()}
    multi = set().union(*(goal_nodes[mid] for mid, m in state.maps.items() if len(m.sources) > 1))
    multi.update(i for rec in state.paths.values() if len(rec.sources) > 1
                 for it in shown(rec) for i in it.node_ids)

    read_at = _read_at(state)
    by_node: dict[str, list[str]] = {}
    for link in links:
        label = sec_labels.get(link.source_id, {}).get(link.section_id)
        if label is None:
            continue
        text = f"{titles[link.source_id]} {label}" if link.node_id in multi else label
        if link.by == "reading":
            text = read_at.get((link.node_id, link.source_id, link.section_id), text)
        mine = by_node.setdefault(link.node_id, [])
        if text not in mine:
            mine.append(text)

    goals: dict[str, list[GoalSourceOut]] = {}
    for mid, m in state.maps.items():
        goals[mid] = _goal_sources(m.sources, goal_nodes[mid], rows, secs, links,
                                   _read_sections(state, mid))
    owner = {n.path_id: mid for mid, m in state.maps.items() for n in m.nodes
             if n.path_id is not None}                 # a node's path belongs to its goal's map
    owner.update({reading_path_id(r.reading_id): r.map_id for r in state.readings.values()
                  if r.map_id in state.maps})          # a reading's path belongs to its goal
    for pid, rec in state.paths.items():
        mid = owner.get(pid)
        if mid is not None:                            # coverage is the whole goal's, not one path's
            goals[pid] = goals[mid]
            continue
        goals[pid] = _goal_sources(rec.sources, {i for it in shown(rec) for i in it.node_ids},
                                   rows, secs, links)
    return SourceInfo(labels_by_node=by_node, goals=goals, exam_counts=counts,
                      section_labels=sec_labels)


def _goal_sources(source_ids: Iterable[str], nodes: set[str], rows: dict, secs: dict,
                  links: list, read: Collection[tuple[str, str]] = ()) -> list[GoalSourceOut]:
    out = []
    for sid in source_ids:
        row = rows.get(sid)
        if row is None:
            continue
        if row.deleted_at is not None:
            out.append(GoalSourceOut(id=sid, title=row.title, deleted=True, uncovered=[]))
            continue
        mine = [k for k in links if k.source_id == sid and k.node_id in nodes]
        by_id = {s.section_id: s for s in secs[sid]}
        gaps = labels.uncovered(secs[sid], mine, {sec for src, sec in read if src == sid})
        out.append(GoalSourceOut(id=sid, title=row.title, deleted=False,
                                 uncovered=[by_id[g].title.strip() for g, _ in gaps]))
    return out
