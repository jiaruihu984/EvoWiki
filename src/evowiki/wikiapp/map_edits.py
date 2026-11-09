"""Editing a goal map's 学习路径 card (chat-first blocks, spec 2026-10-23 §B.5; plan B1.2): drag to
reorder the blocks, ⋯ 改名 / 删除, 加一块, and 撤销 — POST /wiki/maps/{map_id}/edits.

The ops (wiki_schemas.MapEditIn) apply in order, all or none, as ONE `GoalMapSaved(action=
"edited")`; nothing changed → nothing written. 0 LLM calls.
- reorder {ids}: the top-level blocks in their new order — exactly every one of them once. The
  learner's order wins: a same-level prerequisite between two of them that now points backwards
  is dropped from the map (the graph keeps its relations).
- drop {id}: the block leaves the map with what hangs under it (its graph node and K stay). The
  last top-level block cannot be dropped; a block that is being learned (a path / page on it or
  under it, or entered) cannot be dropped (WikiConflict STARTED); a block K shows known can.
- rename {id, name}: the block's name on the map and, when it is a graph node, the node's name
  (`GraphSaved("user_edit")`: one thing is one node, and the map shows a node by its K name).
- add {name, covers}: a new collapsed block at the end with no members and a goal-local id
  (`rg-…`); it becomes a graph node when it is entered (`goal.enter_region`: the scoped intake
  hangs its parts under it).
- insert {node, under, index, links}: the inverse of a drop (撤销): the dropped block back at its
  place with what hung under it, and the relations other blocks had to them (`links`). Refused
  (WikiConflict STALE) when its id or its name is on the map again.
A drop / rename / reorder that touches a block whose diagnosis is still open (a region draft) is
refused (WikiConflict DIAGNOSING): the draft would be left asking about a block that moved or is
gone.

`MapEdited.undo` is the inverse ops of what was applied, in reverse order (drop → insert, rename →
the old name, reorder → the old order, add → drop), each with the relations it took off other
blocks (`links`, D35): 撤销 undoes exactly that edit, never one made after it (the old whole-map
restore did) — a relation is put back only to a block still on the map, and what later edits
added stays.

`edit_blocks` is the chat's way in (`path_blocks` in the ledger, bot.executors_assistant): blocks named, each
op on its own, a refused one reported next to what was done, all of it one commit whose undo is one row's.

An added block's id is opaque (`new_id("rg")`, D36): its name is only shown, so a name renamed
away is free again.

LookupError: no such map; WikiInvalid: an id that is not a block of this map, a name another
block already has, a reorder that is not every block once."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evowiki.goals.regions import GoalMap, MapNode
from evowiki.plain_text import norm_name
from evowiki.wikiapp.events import GoalMapSaved
from evowiki.wikiapp.goal import _Work
from evowiki.wikiapp.graph_sync import graph_events
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Repo, WikiConflict, WikiInvalid
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import (
    MapAddIn,
    MapDropIn,
    MapEditOpIn,
    MapInsertIn,
    MapLinksIn,
    MapRenameIn,
    MapReorderIn,
)

DIAGNOSING = "这一块正在摸底，先答完或跳过"
STARTED = "这一块已经开始学了，不能删"
STALE = "后面又改过，不能这样撤销"


@dataclass(frozen=True)
class MapEdited:
    """The map before the edits and after them, and the inverse ops (the 撤销 of these edits)."""

    before: GoalMap
    after: GoalMap
    undo: tuple[MapEditOpIn, ...] = ()


def _block(m: GoalMap, node_id: str) -> MapNode:
    n = m.node(node_id)
    if n is None or n.kind != "region":
        raise WikiInvalid("这张学习地图上没有这一块", public_message="This learning map has no such block.",
                          code="map_block_missing")
    return n


def _top(m: GoalMap) -> list[MapNode]:
    return [n for n in m.nodes if n.kind == "region" and n.parent is None]


def _name_free(m: GoalMap, name: str, but: str = "") -> None:
    key = norm_name(name)
    if any(n.kind == "region" and n.id != but and norm_name(n.name) == key for n in m.nodes):
        raise WikiInvalid(f"已经有一块叫「{name}」")


def _diagnosing(state: WikiState, m: GoalMap) -> set[str]:
    """The regions of this map whose diagnosis (a region draft) is still open."""
    return {d.region_id for d in state.drafts.values()
            if d.map_id == m.map_id and d.region_id is not None
            and d.draft_id not in state.closed_drafts}


def _not_diagnosing(open_: set[str], ids) -> None:
    if any(i in open_ for i in ids):
        raise WikiConflict(DIAGNOSING)


def _links(before: GoalMap, after: GoalMap) -> list[MapLinksIn]:
    """What `after` took off the blocks it kept (prerequisites / members), with their order before."""
    out = []
    for n in after.nodes:
        was = before.node(n.id)
        if was is None:
            continue
        lost_p = [p for p in was.prereqs if p not in n.prereqs]
        lost_m = [c for c in was.members if c not in n.members]
        if lost_p or lost_m:
            out.append(MapLinksIn(id=n.id, prereqs=lost_p, members=lost_m,
                                  before_prereqs=list(was.prereqs), before_members=list(was.members)))
    return out


def _put_back(now: tuple[str, ...], lost: list[str], before: list[str], here: set[str]) -> tuple[str, ...]:
    """`now` with `lost` (still on the map) back at their places in `before`; what came later stays."""
    back = {x for x in lost if x in here and x not in now}
    if not back:
        return now
    ordered = [x for x in before if x in now or x in back]
    return (*ordered, *(x for x in now if x not in ordered))


def _relink(m: GoalMap, links: Sequence[MapLinksIn]) -> GoalMap:
    here = {x.id for x in m.nodes}
    for k in links:
        n = m.node(k.id)
        if n is None:
            continue
        m = m.replace(n.model_copy(update={
            "prereqs": _put_back(n.prereqs, k.prereqs, k.before_prereqs, here),
            "members": _put_back(n.members, k.members, k.before_members, here)}))
    return m


def _reorder(m: GoalMap, op: MapReorderIn, open_: set[str]) -> tuple[GoalMap, MapEditOpIn]:
    top = _top(m)
    if len(op.ids) != len(set(op.ids)) or set(op.ids) != {n.id for n in top}:
        raise WikiInvalid("新的顺序要正好包含每一块一次")
    old = [n.id for n in top]
    _not_diagnosing(open_, (i for k, i in enumerate(op.ids) if old[k] != i))
    rank = {i: k for k, i in enumerate(op.ids)}
    by_id = {n.id: n for n in top}
    queue = iter(op.ids)
    nodes = []
    for n in m.nodes:
        if n.id in rank:                     # the top-level slots, filled in the new order
            n = by_id[next(queue)]
            n = n.model_copy(update={"prereqs": tuple(
                p for p in n.prereqs if p not in rank or rank[p] < rank[n.id])})
        nodes.append(n)
    after = _relink(m.model_copy(update={"nodes": tuple(nodes)}), op.links)
    return after, MapReorderIn(op="reorder", ids=old, links=_links(m, after))


def _started(n: MapNode) -> bool:
    return (n.path_id is not None or n.page_id is not None
            or (n.kind == "region" and n.state != "collapsed"))


def _drop(m: GoalMap, op: MapDropIn, open_: set[str]) -> tuple[GoalMap, MapEditOpIn]:
    n = _block(m, op.id)
    under = m.descendants(n.id)
    gone = {n.id, *(d.id for d in under)}
    _not_diagnosing(open_, gone)
    if any(_started(x) for x in (n, *under)):
        raise WikiConflict(STARTED)
    if n.parent is None and not [t for t in _top(m) if t.id != n.id]:
        raise WikiInvalid("至少要留一块")
    siblings = [x.id for x in m.nodes if x.kind == "region" and x.parent == n.parent]
    keep = [x.model_copy(update={"prereqs": tuple(p for p in x.prereqs if p not in gone),
                                 "members": tuple(c for c in x.members if c not in gone)})
            for x in m.nodes if x.id not in gone]
    after = m.model_copy(update={"nodes": tuple(keep)})
    return (after, MapInsertIn(op="insert", node=n, under=list(under), index=siblings.index(n.id),
                               links=_links(m, after)))


def _insert(m: GoalMap, op: MapInsertIn) -> tuple[GoalMap, MapEditOpIn]:
    n = op.node
    back = [n, *op.under]
    if n.kind != "region" or any(m.node(x.id) is not None for x in back) or \
            any(x.kind == "region" and norm_name(x.name) == norm_name(n.name) for x in m.nodes):
        raise WikiConflict(STALE)
    if n.parent is not None and m.node(n.parent) is None:
        raise WikiConflict(STALE)
    here = {x.id for x in m.nodes} | {x.id for x in back}
    n = n.model_copy(update={"prereqs": tuple(p for p in n.prereqs if p in here)})
    siblings = [x for x in m.nodes if x.kind == "region" and x.parent == n.parent]
    nodes = list(m.nodes)
    if op.index < len(siblings):
        at = nodes.index(siblings[op.index])
    elif siblings:
        at = nodes.index(siblings[-1]) + 1
    else:
        at = len(nodes)
    nodes[at:at] = [n]
    nodes += op.under
    return (_relink(m.model_copy(update={"nodes": tuple(nodes)}), op.links),
            MapDropIn(op="drop", id=n.id))


def _rename(m: GoalMap, op: MapRenameIn, renames: dict[str, str],
            open_: set[str]) -> tuple[GoalMap, MapEditOpIn]:
    n = _block(m, op.id)
    _not_diagnosing(open_, [n.id])
    _name_free(m, op.name, but=n.id)
    renames[n.id] = op.name
    return (m.replace(n.model_copy(update={"name": op.name})),
            MapRenameIn(op="rename", id=n.id, name=n.name))


def _add(m: GoalMap, op: MapAddIn, new_id: Ids) -> tuple[GoalMap, MapEditOpIn]:
    _name_free(m, op.name)
    node_id = new_id("rg")               # opaque: the name is only shown (D36)
    covers = [c.strip() for c in op.covers if c.strip()]
    # the goal record (D07): a stated beginner's goal never diagnoses a block, an added one too
    record = {"diagnose": False, "said": (m.beginner,)} if m.beginner else {}
    return (m.replace(MapNode(id=node_id, kind="region", name=op.name,
                              scope="、".join(dict.fromkeys(covers)), **record)),
            MapDropIn(op="drop", id=node_id))


def _apply(m: GoalMap, op: MapEditOpIn, renames: dict[str, str],
           open_: set[str], new_id: Ids) -> tuple[GoalMap, MapEditOpIn]:
    if isinstance(op, MapReorderIn):
        return _reorder(m, op, open_)
    if isinstance(op, MapDropIn):
        return _drop(m, op, open_)
    if isinstance(op, MapRenameIn):
        return _rename(m, op, renames, open_)
    if isinstance(op, MapAddIn):
        return _add(m, op, new_id)
    return _insert(m, op)


def _commit(repo: Repo, snap: Any, after: GoalMap, renames: dict[str, str], user_id: str, *, now: datetime,
            new_id: Ids, extra: Sequence[Callable[[Any], None]]) -> None:
    w = _Work(snap, None, user_id, now=now, new_id=new_id)
    g = w.state.graph
    final = {n.id: n.name for n in after.nodes}
    renames = {i: final.get(i, name) for i, name in renames.items()}
    if any(n.id in renames and n.name != renames[n.id] for n in g.nodes):
        nodes = [n.model_copy(update={"name": renames[n.id]}) if n.id in renames else n
                 for n in g.nodes]
        aspect, wiki = graph_events(w.state, w.k, g.updated(nodes=nodes), "user_edit",
                                    now=now, new_id=new_id)
        w.add_aspect(aspect)
        w.add_events(wiki)
    w.add(GoalMapSaved(record=after, action="edited"))
    w.commit(repo, extra)


def edit_map(repo: Repo, user_id: str, map_id: str, ops: Sequence[MapEditOpIn], *,
             now: datetime, new_id: Ids, extra: Sequence[Callable[[Any], None]] = ()) -> MapEdited:
    """Apply `ops` to the map (module doc) in one commit with `extra`; `MapEdited.undo` is their inverse."""
    snap = repo.load(user_id)
    m = snap.state.maps.get(map_id)
    if m is None:
        raise LookupError(f"没有这张学习地图：{map_id}")
    open_ = _diagnosing(snap.state, m)
    after = m
    renames: dict[str, str] = {}
    undo: list[MapEditOpIn] = []
    for op in ops:
        after, inverse = _apply(after, op, renames, open_, new_id)
        undo.insert(0, inverse)
    if after == m:
        return MapEdited(m, m)
    _commit(repo, snap, after, renames, user_id, now=now, new_id=new_id, extra=extra)
    return MapEdited(m, after, tuple(undo))


class PathBlockOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["drop", "rename", "add", "place"] = Field(
        description="drop=这一块不学了；rename=改名；add=加一块；place=把一块挪到第几个（从某一块开始就是挪到 1）")
    name: str = Field(max_length=120, description="那一块的名字，照学习路径上的写；add：新块的名字")
    new_name: str = Field(default="", max_length=120, description="rename：新名字")
    covers: list[str] = Field(default_factory=list, max_length=50, description="add：新块包含的小主题，可省略")
    position: int | None = Field(default=None, ge=1, description="place：挪到第几块（从 1 数）；add：放在第几块（省略=最后）")


@dataclass(frozen=True)
class BlocksEdited:
    after: GoalMap
    undo: tuple[MapEditOpIn, ...]
    renames: dict[str, str]
    done: tuple[str, ...]
    refused: tuple[str, ...]


def _named(m: GoalMap, name: str) -> MapNode:
    for n in _top(m):
        if norm_name(n.name) == norm_name(name):
            return n
    raise WikiInvalid(f"学习路径上没有「{name}」，现在的块：{'、'.join(n.name for n in _top(m))}")


def placed(m: GoalMap, name: str, position: int) -> MapReorderIn:
    """The reorder that puts the block named `name` at `position` (from 1)."""
    n = _named(m, name)
    ids = [x.id for x in _top(m) if x.id != n.id]
    ids.insert(min(position - 1, len(ids)), n.id)
    return MapReorderIn(op="reorder", ids=ids)


def _block_op(m: GoalMap, op: PathBlockOp, go: Callable[[MapEditOpIn], GoalMap]) -> str:
    if op.op == "drop":
        n = _named(m, op.name)
        go(MapDropIn(op="drop", id=n.id))
        return f"删掉了「{n.name}」"
    if op.op == "rename":
        if not op.new_name.strip():
            raise WikiInvalid("没有给新名字")
        n = _named(m, op.name)
        go(MapRenameIn(op="rename", id=n.id, name=op.new_name.strip()))
        return f"「{n.name}」改名为「{op.new_name.strip()}」"
    if op.op == "add":
        added = go(MapAddIn(op="add", name=op.name.strip(), covers=op.covers))
        if op.position is not None:
            go(placed(added, op.name, op.position))
        return f"加了「{op.name.strip()}」"
    if op.position is None:
        raise WikiInvalid("没有说挪到第几块")
    go(placed(m, op.name, op.position))
    return f"「{op.name}」挪到了第 {op.position} 块"


@dataclass
class _Trial:
    """One chat op on a copy of the fold's state: an add and its place stand or fall together."""

    at: GoalMap
    undo: list[MapEditOpIn]
    renames: dict[str, str]
    open_: set[str]
    new_id: Ids

    def go(self, one: MapEditOpIn) -> GoalMap:
        self.at, inverse = _apply(self.at, one, self.renames, self.open_, self.new_id)
        self.undo.insert(0, inverse)
        return self.at


def fold_blocks(state: WikiState, map_id: str, ops: Sequence[PathBlockOp], new_id: Ids) -> BlocksEdited:
    """`ops` applied to the map one by one, nothing written: an op that cannot be done is reported
    and leaves the map as the op before it did."""
    m = state.maps.get(map_id)
    if m is None:
        raise LookupError(f"没有这个目标：{map_id}")
    open_ = _diagnosing(state, m)
    after, undo, renames, done, refused = m, [], {}, [], []
    for op in ops:
        trial = _Trial(after, list(undo), dict(renames), open_, new_id)
        try:
            done.append(_block_op(after, op, trial.go))
        except (WikiConflict, WikiInvalid, LookupError) as exc:
            refused.append(f"「{op.name}」没有改：{exc}")
            continue
        after, undo, renames = trial.at, trial.undo, trial.renames
    return BlocksEdited(after, tuple(undo), renames, tuple(done), tuple(refused))


def edit_blocks(repo: Repo, user_id: str, map_id: str, ops: Sequence[PathBlockOp], *, now: datetime, new_id: Ids,
                extra: Callable[[BlocksEdited], Sequence[Callable[[Any], None]]]) -> BlocksEdited:
    """`fold_blocks` written in one commit with `extra(what it did)`; with nothing done, nothing written."""
    snap = repo.load(user_id)
    got = fold_blocks(snap.state, map_id, ops, new_id)
    if got.done:
        _commit(repo, snap, got.after, got.renames, user_id, now=now, new_id=new_id, extra=extra(got))
    return got
