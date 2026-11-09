"""Graph structure changes: split a node into finer children, undo a change.

Every op returns a new valid graph and appends a `GraphChange` that holds exactly what was added and
removed, so `undo` is exact (the graph is canonically ordered, so "exact" means equal dumps). The
LLM writes the children; this module only applies and records them."""

from collections.abc import Mapping, Sequence

from pydantic import AwareDatetime

from evowiki.nodes.model import (
    GraphChange,
    GraphError,
    NodeGraph,
    NodeRec,
    Relation,
    merge_relation,
)
from evowiki.nodes.place import place_missing

ChildSpec = NodeRec  # a child is a plain node; relations among children are passed as `links`


class UndoConflict(ValueError):
    """The change cannot be undone exactly: unknown, already undone, or something later touched
    what it added."""


def split(
    graph: NodeGraph,
    parent_id: str,
    children: Sequence[NodeRec],
    links: Sequence[Relation],
    *,
    change_id: str,
    at: AwareDatetime,
    why: str,
    summary: str,
    phrases: Mapping[str, str] | None = None,
    note: str = "",
    words: str = "",
    reused: Sequence[str] = (),
) -> NodeGraph:
    """Add `children` as parts of `parent_id` (phrase per child from `phrases`), the existing
    nodes `reused` as parts too (only their part_of is added), plus `links`, each touching a new
    or reused part, between nodes the graph then has. The parent and all its existing relations
    stay unchanged."""
    if graph.node(parent_id) is None:
        raise GraphError("unknown_id", f"no node {parent_id!r} to split")
    if not children and not reused:
        raise GraphError("empty", f"split of {parent_id!r} without children")
    kid_ids = {c.id for c in children}
    for c in children:
        if graph.node(c.id) is not None:
            raise GraphError("duplicate_node", f"child {c.id!r} already exists")
    for r in reused:
        if graph.node(r) is None:
            raise GraphError("unknown_id", f"no node {r!r} to reuse")
        if r == parent_id or r in graph.ancestors(parent_id):
            raise GraphError("cycle", f"{r!r} is {parent_id!r} or above it")
    parts = kid_ids | set(reused)
    known = kid_ids | {n.id for n in graph.nodes}
    for r in links:
        if r.src not in known or r.dst not in known or not ({r.src, r.dst} & parts):
            raise GraphError("unknown_id", f"link {r.src}→{r.dst} is not on a part of the split")
    phrases = phrases or {}
    rels: dict[tuple[str, str, str], Relation] = {}
    new = [
        Relation(
            kind="part_of", src=c, dst=parent_id, phrase=phrases.get(c, ""), source="split"
        )
        for c in (*(c.id for c in children), *reused)
    ]
    new += [r.model_copy(update={"source": "split"}) for r in links]
    for r in new:
        if r.key not in {x.key for x in graph.relations}:
            rels[r.key] = merge_relation(rels.get(r.key), r)
    grown = place_missing(
        graph.updated(
            nodes=(*graph.nodes, *children), relations=(*graph.relations, *rels.values())
        ),
        only=kid_ids,
    )
    change = GraphChange(
        change_id=change_id,
        at=at,
        kind="split",
        summary=summary,
        why=why,
        node_ids=(parent_id, *(c.id for c in children), *reused),
        added_nodes=tuple(grown.node(c.id) for c in children),  # type: ignore[misc]
        added_relations=tuple(rels[k] for k in sorted(rels)),
        note=note,
        words=words,
    )
    return _log(grown, change)


def _log(graph: NodeGraph, change: GraphChange) -> NodeGraph:
    if any(c.change_id == change.change_id for c in graph.changes):
        raise ValueError(f"change id {change.change_id!r} already used")
    return graph.updated(changes=(*graph.changes, change))


def _touched(c: GraphChange) -> tuple[set[str], set[tuple[str, str, str]]]:
    ids = set(c.node_ids) | {r.id for r in (*c.added_nodes, *c.removed_nodes)}
    ids |= {e for r in (*c.added_relations, *c.removed_relations) for e in (r.src, r.dst)}
    return ids, {r.key for r in (*c.added_relations, *c.removed_relations)}


def undo(graph: NodeGraph, change_id: str) -> NodeGraph:
    """Reverse `change_id` exactly and mark it undone. UndoConflict when it is unknown or already
    undone, when a later non-undone change touched a node or relation it added, or when a relation
    recorded nowhere in the change now hangs on a node the undo would remove."""
    idx = next((i for i, c in enumerate(graph.changes) if c.change_id == change_id), None)
    if idx is None:
        raise UndoConflict(f"no change {change_id!r}")
    change = graph.changes[idx]
    if change.undone:
        raise UndoConflict(f"change {change_id!r} is already undone")
    added_ids = {r.id for r in change.added_nodes}
    added_keys = {r.key for r in change.added_relations}
    for later in graph.changes[idx + 1 :]:
        if later.undone:
            continue
        ids, keys = _touched(later)
        if ids & added_ids or keys & added_keys:
            raise UndoConflict(f"change {later.change_id!r} touched what {change_id!r} added")
    drop_ids = added_ids - {r.id for r in change.removed_nodes}
    relations = [r for r in graph.relations if r.key not in added_keys]
    stray = [r for r in relations if r.src in drop_ids or r.dst in drop_ids]
    if stray:
        raise UndoConflict(f"relations added later hang on {sorted(drop_ids)}: {stray[0].key}")
    nodes = [r for r in graph.nodes if r.id not in added_ids]
    changes = [
        c.model_copy(update={"undone": True}) if i == idx else c
        for i, c in enumerate(graph.changes)
    ]
    try:
        return graph.updated(
            nodes=(*nodes, *change.removed_nodes),
            relations=(*relations, *change.removed_relations),
            changes=changes,
        )
    except GraphError as e:
        raise UndoConflict(f"undo of {change_id!r} would break the graph: {e}") from e


def rewire(
    graph: NodeGraph,
    *,
    remove: Sequence[tuple[str, str, str]] = (),
    add: Sequence[Relation] = (),
) -> NodeGraph:
    """The graph without the relations whose key (kind, src, dst) is in `remove`, plus `add`
    (deduped like `NodeGraph.with_relation`; an added relation replaces a removed one of the
    same key, so a change of phrase or kind of source is remove + add). The learner's edits
    (assistant `node_relation` / `node_move`) go through here and are not logged as a
    `GraphChange`: their undo data lives on the action row (`restore_relations`). GraphError
    when the result is invalid (unknown id, self-loop, part_of / prerequisite cycle);
    GraphError("unknown_id") also for a key to remove that is not there (the learner's picture
    of the graph is stale)."""
    have = {r.key for r in graph.relations}
    for key in remove:
        if tuple(key) not in have:
            raise GraphError("unknown_id", f"no relation {key!r} to remove")
    gone = {tuple(k) for k in remove}
    kept = {r.key: r for r in graph.relations if r.key not in gone}
    for r in add:
        kept[r.key] = merge_relation(kept.get(r.key), r)
    return graph.updated(relations=kept.values())


def restore_relations(
    graph: NodeGraph, *, added: Sequence[Relation], removed: Sequence[Relation]
) -> NodeGraph:
    """Exact undo of a `rewire`: takes `added` out and puts `removed` back. UndoConflict when a
    later change touched them (an added relation is gone or different now, or a removed one's key
    is taken again by a different relation) or the result would be invalid."""
    cur = {r.key: r for r in graph.relations}
    for r in added:
        if cur.get(r.key) != r:
            raise UndoConflict(f"relation {r.key!r} was changed after the edit")
    for r in removed:
        if r.key in cur and r.key not in {a.key for a in added}:
            raise UndoConflict(f"relation {r.key!r} exists again")
    for r in added:
        del cur[r.key]
    for r in removed:
        cur[r.key] = r
    try:
        return graph.updated(relations=cur.values())
    except GraphError as e:
        raise UndoConflict(f"undo would break the graph: {e}") from e
