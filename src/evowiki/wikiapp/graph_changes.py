"""The knowledge graph's structural changes in the RSI change list (参数 tab), through the RSI
session's `teach.change_source.ChangeSource` hook (plan T6): track "G", ids prefixed "g_".

`list`: one row per split `GraphChange` of the user's graph (`LISTED`), newest first,
undoable while nothing later touched what they added. The one-time legacy conversion (`migrate`)
and resets are bookkeeping, not changes the learner made or should judge: they stay in the
graph's change log, the logs and the backups but are not listed (user ruling 2026-10-15); an
undo of one of their ids is still refused (409), as before. `undo`: `nodes.ops.undo` through the repo in one commit (K sync, names,
`GraphSaved(action="undone")`); an unknown id is LookupError (404), a change that cannot be undone
is `GraphUndoRefused` (a ValueError: 409) with a sentence the learner can read."""

from datetime import datetime

from evowiki.goals.path_edit import PathBy, ReplaceEdit, UnreplaceEdit, apply_edits
from evowiki.nodes import UndoConflict, undo
from evowiki.nodes.model import GraphChange, NodeGraph
from evowiki.teach.contracts import ChangeOut
from evowiki.wikiapp.events import GoalMapSaved, WikiEvent
from evowiki.wikiapp.graph_sync import graph_events
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Repo, Snapshot, WikiConflict

PREFIX = "g_"
TARGET = "知识图谱"
UNDOABLE = frozenset({"split"})
LISTED = UNDOABLE
NOT_UNDOABLE = "这次改动之后，相关的点又被改过了（比如子点又被拆细），要先撤销后面那次"


class GraphUndoRefused(ValueError):
    """The change cannot be undone (409)."""


def can_undo(graph: NodeGraph, change: GraphChange) -> bool:
    if change.kind not in UNDOABLE or change.undone:
        return False
    try:
        undo(graph, change.change_id)
    except UndoConflict:
        return False
    return True


def change_out(graph: NodeGraph, change: GraphChange) -> ChangeOut:
    return ChangeOut(patch_id=change.change_id, track="G",
                     status="rolled_back" if change.undone else "personal_accepted",
                     summary=change.summary,
                     why=change.why, signals=(change.words,) if change.words else (),
                     after=change.note, at=change.at, can_undo=can_undo(graph, change),
                     layer=0, target=TARGET)


def undo_split(repo: Repo, snap: Snapshot, change_id: str, *, now: datetime,
               new_id: Ids, source: PathBy = "user") -> Snapshot:
    """Undo a split and its path/map projections in one commit, from either undo entrance.

    Later uses of removed points refuse the operation; unrelated edits and existing reused
    points stay. Path changes use the ordinary commit operation and notify its hooks.
    """
    from evowiki.wikiapp.path_edits import build_path_edit, commit_paths
    from evowiki.wikiapp.paths import shown

    graph = snap.state.graph
    change = next(c for c in graph.changes if c.change_id == change_id)
    back = undo(graph, change_id)
    gone = {n.id for n in graph.nodes} - {n.id for n in back.nodes}
    changes = {}
    for pid, rec in snap.state.paths.items():
        active = {}
        for edit in rec.edits:
            if isinstance(edit, ReplaceEdit):
                active[edit.page_key] = edit
            elif isinstance(edit, UnreplaceEdit):
                active.pop(edit.page_key, None)
        originals = {i.key: i for i in apply_edits(rec.path.pages, rec.edits)}
        edits = [UnreplaceEdit(page_key=key) for key, edit in active.items()
                 if originals[key].node_ids == (change.node_ids[0],)
                 and {i.node_id for i in edit.items} == set(change.node_ids[1:])]
        replaced_keys = {i.page_key for edit in edits for i in active[edit.page_key].items}
        if any(page.page_id in replaced_keys and page_id is not None
               for page, page_id in zip(rec.path.pages, rec.page_ids, strict=True)):
            raise WikiConflict("后来已经开始学习拆出来的页，不能撤销")
        after = build_path_edit(back, rec, edits, by=source)
        if any(gone.intersection(i.node_ids) for i in shown(after)):
            raise WikiConflict("后来路径又用到了拆出来的点，不能撤销")
        if edits:
            changes[pid] = edits
    aspect, wiki = graph_events(snap.state, snap.k, back, "undone", now=now, new_id=new_id)
    for m in snap.state.maps.values():
        if any(n.id in gone and (n.path_id is not None or n.page_id is not None
                                or m.children(n.id)) for n in m.nodes):
            raise WikiConflict("后来已经学习或展开了拆出来的点，不能撤销")
        nodes = tuple(n.model_copy(update={
            "members": tuple(i for i in n.members if i not in gone),
            "prereqs": tuple(i for i in n.prereqs if i not in gone)})
                      for n in m.nodes if n.id not in gone)
        if nodes != m.nodes:
            wiki.append(WikiEvent(event_id=new_id("wev"), ts=now,
                                  body=GoalMapSaved(record=m.model_copy(update={"nodes": nodes}),
                                                    action="edited")))
    _, done = commit_paths(repo, snap, changes, now=now, new_id=new_id, source=source,
                           aspect=tuple(aspect), wiki=tuple(wiki), graph=back)
    return done


class GraphChanges:
    """`ChangeSource` over a wiki repo (`PgRepo` in the service)."""

    prefix = PREFIX
    rsi_related = False

    def __init__(self, repo: Repo, new_id: Ids) -> None:
        self.repo = repo
        self.new_id = new_id

    def list(self, user_id: str) -> list[ChangeOut]:
        graph = self.repo.load(user_id).state.graph
        rows = [change_out(graph, c) for c in graph.changes if c.kind in LISTED]
        return sorted(rows, key=lambda r: r.at, reverse=True)

    def undo(self, user_id: str, change_id: str, *, now: datetime) -> ChangeOut:
        snap = self.repo.load(user_id)
        graph = snap.state.graph
        change = next((c for c in graph.changes if c.change_id == change_id), None)
        if change is None:
            raise LookupError(f"没有这次图谱改动：{change_id}")
        if change.kind not in UNDOABLE:
            raise GraphUndoRefused("这条记录只是说明，不能撤销")
        if change.undone:
            raise GraphUndoRefused("这次改动已经撤销过了")
        try:
            done = undo_split(self.repo, snap, change_id, now=now, new_id=self.new_id)
        except UndoConflict as e:
            raise GraphUndoRefused(NOT_UNDOABLE) from e
        except WikiConflict as e:
            raise GraphUndoRefused(str(e)) from e
        g = done.state.graph
        out = next(c for c in g.changes if c.change_id == change_id)
        return change_out(g, out)
