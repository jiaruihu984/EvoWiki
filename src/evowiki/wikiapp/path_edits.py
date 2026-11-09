"""Path edits and node actions in the wiki app (unified node graph plan T7, wire contract §4).

- `save_paths` / `save_path`: the one place a path edit is stored (one PathSaved(user_edit) per
  changed path, with any events that belong in the same commit; who made each edit is on the
  edit, `PathEdit.by`) and announced to the `on_path_changed` hooks — whoever makes it (user,
  assistant, Bot).
- `commit_paths` / `commit_path_edit`: keyed edits checked (`_check`), stamped with their
  `source` and stored through `save_paths`. Every function below goes through them; the Bot's
  weekly-review edits (`bot_edited`, B5b) store through `save_path` directly.
- `edit_path`: move / remove / add on a path as the user sees it (indices 0-based into the shown
  path), through `commit_path_edit`.
- `split_row` 「拆成多页」: the row's node is split (the shared one-level split, wikiapp.node_split,
  source path_row) and the children take the row's place — graph change, K and the path edit in one commit. A row that covers several nodes is
  replaced by one row per node instead (no LLM, no graph change). `undo_split_row` reverses
  either.
- `skip_plan` 「这个我早会了」: the learner's words as evidence on every cell of the node — a
  `Declare(know)` plus an `AddNote` 「你说：我会了」 per cell, the same pair as the cell
  declaration (`kchange.declare_flow`), then the K-change pass on the current page.
- `want_node` 「我想学这个」: a page of the user's own for the node on the goal's path (nothing
  changes when the path already shows a page for it).

A page the user adds for a node is stored as a path page (key `u-…`, it teaches that node), so
「下一页」 can start it; a title-only page is shown but never started (nothing to teach).
0 LLM calls except the K-change pass (which degrades without a gateway like any declaration) and
`split_row` (one split call)."""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from evowiki.belief.aspects import AddNote, AspectEvent, Declare
from evowiki.goals.contracts import cell_id
from evowiki.goals.path_edit import (
    AddEdit,
    MoveEdit,
    PathBy,
    PathEdit,
    RemoveEdit,
    ReplaceEdit,
    ReplaceItem,
    RetitleEdit,
    SkipEdit,
    UnremoveEdit,
    UnreplaceEdit,
    WantEdit,
    apply_edits,
    own_concept,
    own_page,
    user_key,
    visible,
)
from evowiki.nodes import UndoConflict
from evowiki.nodes.model import NodeGraph, NodeRec, Relation
from evowiki.wikiapp.events import PageSaved, PathRecord, PathSaved, WikiEvent
from evowiki.wikiapp.graph_changes import NOT_UNDOABLE, undo_split
from evowiki.wikiapp.graph_sync import graph_events
from evowiki.wikiapp.kchange import k_changed
from evowiki.wikiapp.kwrite import Ids, apply_in_memory
from evowiki.wikiapp.llm import CountingGateway, LLMUnavailable
from evowiki.wikiapp.node_split import ask_split, split_events, split_step
from evowiki.wikiapp.path_order import page_needs, stored_index
from evowiki.wikiapp.paths import _record, path_items, shown
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict, WikiInvalid
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import (
    NodeActionOut,
    PathAddIn,
    PathEditIn,
    PathEditOut,
    PathMoveIn,
    PathRemoveIn,
    PathRetitleIn,
    PathUnremoveIn,
)

SKIP_WORDS = "你说：我会了"
log = logging.getLogger(__name__)

PathSource = PathBy             # who made a path edit (`PathEdit.by`)
PathChangedHook = Callable[[str, str, PathRecord, PathRecord, PathSource], None]
_hooks: list[PathChangedHook] = []


def on_path_changed(fn: PathChangedHook) -> PathChangedHook:
    """Registers `fn(user_id, path_id, before, after, source)`, called after every committed path
    edit (user, assistant or Bot alike) — the place to re-order the write queue, cancel or mark
    stale the runs of changed rows. A plain in-process registry (also usable as a decorator)."""
    if fn not in _hooks:
        _hooks.append(fn)
    return fn


def off_path_changed(fn: PathChangedHook) -> None:
    if fn in _hooks:
        _hooks.remove(fn)


def notify_path_changed(user_id: str, before: PathRecord, after: PathRecord,
                        source: PathSource) -> None:
    """Runs the hooks. The edit is committed already, so a failing hook is logged with its
    traceback and the others still run; it is never raised into the edit's caller."""
    for fn in tuple(_hooks):
        try:
            fn(user_id, after.path.path_id, before, after, source)
        except Exception:
            log.exception("on_path_changed hook %r failed (path %s)", fn, after.path.path_id)


def _saved(rec: PathRecord, *, now: datetime, new_id: Ids) -> WikiEvent:
    return WikiEvent(event_id=new_id("wev"), ts=now, body=PathSaved(record=rec,
                                                                    action="user_edit"))


def _node_of(graph: NodeGraph, node_id: str):
    node = graph.node(node_id)
    if node is None:
        raise LookupError(f"没有这个节点：{node_id}")
    return node


def _store_pages(graph: NodeGraph, rec: PathRecord,
                 rows: list[tuple[str, str, str | None, str]]) -> PathRecord:
    """The record with a stored path page (and its concept) for every (key, title, node) row that
    has a node and no stored page yet, so 「下一页」 can start it."""
    pages, concepts, ids = rec.path.pages, rec.path.concepts, rec.page_ids
    have = {p.page_id for p in pages}
    for key, title, node_id, question in rows:
        if node_id is None or key in have:
            continue
        node = _node_of(graph, node_id)
        if node_id not in {c.id for c in concepts}:
            concepts = (*concepts, own_concept(node_id, node.name, graph.prereqs(node_id)))
        pages, ids = (*pages, own_page(key, node_id, title, question)), (*ids, None)
    if pages is rec.path.pages:
        return rec
    path = rec.path.model_copy(update={"pages": pages, "concepts": concepts})
    return rec.model_copy(update={"path": path, "page_ids": ids})


def _with(graph: NodeGraph, rec: PathRecord, edit: PathEdit) -> PathRecord:
    """The record after `edit`: appended to the edit list; the pages of the user's own that have
    a node are stored as path pages."""
    match edit:
        case AddEdit() | WantEdit():
            rec = _store_pages(graph, rec, [(edit.page_key, edit.title, edit.node_id,
                                       edit.question if isinstance(edit, AddEdit) else "")])
        case ReplaceEdit():
            rec = _store_pages(graph, rec, [(i.page_key, i.title, i.node_id, i.question)
                                       for i in edit.items])
    return rec.model_copy(update={"edits": (*rec.edits, edit)})


def _node(state: WikiState, node_id: str):
    return _node_of(state.graph, node_id)


def _has_node(rec: PathRecord, node_id: str) -> bool:
    return any(node_id in it.node_ids for it in shown(rec))


def _add_own(state: WikiState, rec: PathRecord, title: str, node_id: str | None, *,
             want: bool, to_index: int | None = None) -> PathRecord:
    """The record with a page of the user's own added (at the end, or at `to_index` of the shown
    path). Unchanged when the path already shows a page for the node."""
    if node_id is not None and _has_node(rec, node_id):
        return rec
    key = user_key(title, node_id, [i.key for i in apply_edits(rec.path.pages, rec.edits)]
                   + [p.page_id for p in rec.path.pages])
    if node_id is not None:
        _node(state, node_id)
    edit: PathEdit = (WantEdit(page_key=key, node_id=node_id, title=title)
                      if want and node_id is not None
                      else AddEdit(page_key=key, title=title, node_id=node_id, to_index=to_index))
    return _with(state.graph, rec, edit)


def _index(items: list, i: int) -> None:
    if not 0 <= i < len(items):
        raise WikiInvalid(f"路径里没有第 {i + 1} 页")


def edited(state: WikiState, rec: PathRecord, edit: PathEditIn) -> PathRecord:
    """The record after one user edit (indices into the shown path). A bad index → WikiInvalid,
    an unknown node → LookupError."""
    items = shown(rec)
    match edit:
        case PathMoveIn():
            _index(items, edit.from_)
            _index(items, edit.to)
            if edit.from_ == edit.to:
                return rec
            return _with(state.graph, rec,
                         MoveEdit(page_key=items[edit.from_].key, to_index=edit.to))
        case PathRemoveIn():
            _index(items, edit.index)
            return _with(state.graph, rec, RemoveEdit(page_key=items[edit.index].key))
        case PathUnremoveIn():
            return _with(state.graph, rec, UnremoveEdit(page_key=edit.page_key))
        case PathAddIn():
            if edit.node_id is not None:
                _node(state, edit.node_id)
            if edit.to_index is not None and edit.to_index > len(items):
                raise WikiInvalid(f"路径里放不下第 {edit.to_index + 1} 页的位置")
            return _add_own(state, rec, edit.title, edit.node_id, want=False,
                            to_index=edit.to_index)
    raise WikiInvalid(f"不认识的路径修改：{edit!r}")


def _check(rec: PathRecord, edit: PathEdit) -> None:
    """A page the path does not show is refused before anything is stored (replay would ignore
    it); so is a key that already exists. Adds and a replace's items bring their own keys."""
    rows = apply_edits(rec.path.pages, rec.edits)
    every = {i.key for i in rows}
    shown_keys = {i.key for i in visible(rows)}
    match edit:
        case AddEdit() | WantEdit():
            if edit.page_key in every:
                raise WikiInvalid(f"路径里已经有这一页：{edit.page_key}")
        case ReplaceEdit():
            if edit.page_key not in shown_keys:
                raise WikiInvalid(f"路径里没有这一页：{edit.page_key}")
            kids = [i.page_key for i in edit.items]
            if len(set(kids)) != len(kids) or set(kids) & every:
                raise WikiInvalid("拆出来的页和路径里已有的页重名")
        case UnremoveEdit():
            last = next((e for e in reversed(rec.edits) if e.page_key == edit.page_key), None)
            if edit.page_key not in every - shown_keys or not isinstance(last, RemoveEdit):
                raise WikiInvalid(f"这一页不是被删掉的：{edit.page_key}")
        case UnreplaceEdit():
            if edit.page_key not in every - shown_keys:
                raise WikiInvalid(f"这一页没有被拆开：{edit.page_key}")
        case _:
            if edit.page_key not in shown_keys:
                raise WikiInvalid(f"路径里没有这一页：{edit.page_key}")


def build_path_edit(graph: NodeGraph, rec: PathRecord, edits: list[PathEdit], *,
                    by: PathSource | None = None) -> PathRecord:
    """The record after `edits` (checked one by one; an add for a node the path already shows is
    left out). `graph`: where the nodes of new pages are read. `by`: who makes them, stamped on
    every edit (`PathEdit.by`; None: as given)."""
    for e in edits:
        if by is not None and e.by != by:
            e = e.model_copy(update={"by": by})
        if isinstance(e, AddEdit | WantEdit) and e.node_id and _has_node(rec, e.node_id):
            continue                      # the path already shows a page for that node
        _check(rec, e)
        rec = _with(graph, rec, e)
    return rec


def _started(rec: PathRecord, started: Mapping[str, str]) -> PathRecord:
    """The record with `started` (row key -> wiki page id) set on the rows it stores a page for."""
    ids = list(rec.page_ids)
    for n, p in enumerate(rec.path.pages):
        if p.page_id in started:
            ids[n] = started[p.page_id]
    return rec if tuple(ids) == rec.page_ids else rec.model_copy(update={"page_ids": tuple(ids)})


def save_paths(repo: Repo, snap: Snapshot, records: Sequence[PathRecord], *, now: datetime,
               new_id: Ids, source: PathSource, aspect: tuple[AspectEvent, ...] = (),
               wiki: tuple[WikiEvent, ...] = (),
               extra: Sequence[Callable[[Any], None]] = ()) -> Snapshot:
    """THE storage point of every path edit: the extra `aspect` / `wiki` events first, then one
    PathSaved(user_edit) per record (who made each edit is on the edit, `PathEdit.by`), ONE
    commit with `extra` (the action ledger's exactly-once effect); then the `on_path_changed`
    hooks run once per record that differs from the snapshot's (`source`: who made the change).
    Commits what it is given — the caller decides there is something to store. WikiConflict when
    the wiki moved since `snap`. Returns the snapshot after."""
    before = {r.path.path_id: snap.state.paths.get(r.path.path_id) for r in records}
    events = [*wiki, *(_saved(r, now=now, new_id=new_id) for r in records)]
    done = repo.commit(snap, Commit(aspect=list(aspect), wiki=events, extra=list(extra)))
    for r in records:
        old = before[r.path.path_id]
        if old is not None and old != r:
            notify_path_changed(snap.user_id, old, r, source)
    return done


def save_path(repo: Repo, snap: Snapshot, new: PathRecord, *, now: datetime, new_id: Ids,
              extra: Sequence[Callable[[Any], None]] = (), source: PathSource = "user"):
    """Commit one edited path record (`save_paths` for one record): the Bot's moves / inserts and
    their undo (B5b) store through here, with `source="bot"`. `extra`: writes that land in the
    same commit. WikiConflict when the wiki moved since `snap`."""
    return save_paths(repo, snap, [new], now=now, new_id=new_id, source=source, extra=extra)


def commit_paths(repo: Repo, snap: Snapshot, changes: dict[str, list[PathEdit]], *, now: datetime,
                 new_id: Ids, source: PathSource, aspect: tuple[AspectEvent, ...] = (),
                 wiki: tuple[WikiEvent, ...] = (), graph: NodeGraph | None = None,
                 started: Mapping[str, str] | None = None,
                 extra: Sequence[Callable[[Any], None]] = ()
                 ) -> tuple[dict[str, PathRecord], Snapshot]:
    """Checks and applies `changes` (path id → keyed edits, each stamped `by=source`) and stores
    them through `save_paths` as ONE commit: the extra `aspect` / `wiki` events first, then one
    PathSaved(user_edit) per changed path; then the `on_path_changed` hooks run (once per changed
    path). `graph`: the graph the new pages' nodes are read from (default: the snapshot's). A bad
    edit raises before anything is written; nothing to write commits nothing. Returns the records
    now stored and the snapshot after.
    `started` (row key -> wiki page id): rows that get their wiki page in this commit (the page
    record's own events go in `wiki`), so they count as started at once. `extra`: writes that must
    land with the commit (`Commit.extra`: an action ledger's effect); like the events they ride
    on, none runs when there is nothing to commit."""
    graph = graph or snap.state.graph
    before = {pid: _record(snap.state, pid) for pid in changes}
    after = {pid: _started(build_path_edit(graph, before[pid], es, by=source), started or {})
             for pid, es in changes.items()}
    changed = [pid for pid in changes if after[pid] != before[pid]]
    if not changed and not aspect and not wiki:
        return after, snap
    done = save_paths(repo, snap, [after[pid] for pid in changed], now=now, new_id=new_id,
                      source=source, aspect=aspect, wiki=wiki, extra=extra)
    return after, done


def commit_path_edit(repo: Repo, user_id: str, path_id: str, edits: list[PathEdit], *,
                     now: datetime, new_id: Ids, source: PathSource) -> PathRecord:
    """`commit_paths` for the edits of one path. Every path edit goes through here or
    `commit_paths` — the user's (`edit_path`), the assistant's and the Bot's (`source`). An edit
    on a page the path does not show → WikiInvalid; unknown path or node → LookupError; a no-op
    commits nothing. Returns the record now stored."""
    snap = repo.load(user_id)
    after, _ = commit_paths(repo, snap, {path_id: edits}, now=now, new_id=new_id, source=source)
    return after[path_id]


def edit_path(repo: Repo, user_id: str, path_id: str, edit: PathEditIn, *, now: datetime,
              new_id: Ids) -> PathEditOut:
    """POST /wiki/paths/{path_id}/edits. Unknown path → LookupError. A no-op edit commits
    nothing."""
    snap = repo.load(user_id)
    rec = _record(snap.state, path_id)
    state, graph = snap.state, snap.state.graph
    aspect: tuple[AspectEvent, ...] = ()
    wiki: tuple[WikiEvent, ...] = ()
    if isinstance(edit, PathRetitleIn):
        return _retitle(repo, snap, rec, edit, now=now, new_id=new_id)
    if isinstance(edit, PathAddIn) and edit.node_id is None:
        # D34: a page the learner adds is a real page: it gets a node of its own (named like the
        # title) in the same commit, so 「下一页」 / the goal's run writes it like any other row
        node = NodeRec(id=new_id("n"), name=edit.title)
        # under the block the path hangs on: else it is a stray point, grouped with whatever
        under = next((n.id for m in state.maps.values() for n in m.nodes
                      if n.path_id == path_id and graph.node(n.id) is not None), None)
        part = [Relation(kind="part_of", src=node.id, dst=under, source="user")] if under else []
        graph = graph.updated(nodes=[*graph.nodes, node], relations=[*graph.relations, *part])
        aspect_l, wiki_l = graph_events(state, snap.k, graph, "user_edit", now=now, new_id=new_id)
        aspect, wiki = tuple(aspect_l), tuple(wiki_l)
        state = state.model_copy(update={"graph": graph})
        edit = edit.model_copy(update={"node_id": node.id})
    new = edited(state, rec, edit)
    if new != rec:
        after, snap = commit_paths(repo, snap, {path_id: list(new.edits[len(rec.edits):])},
                                   now=now, new_id=new_id, source="user", graph=graph,
                                   aspect=aspect, wiki=wiki)
        new = after[path_id]
    return PathEditOut(path=path_items(snap.state, new, snap.k, now))


def _retitle(repo: Repo, snap: Snapshot, rec: PathRecord, edit: PathRetitleIn, *, now: datetime,
             new_id: Ids) -> PathEditOut:
    """重命名 of a row: the row, every other path's row on the same wiki page, and the page itself
    in one commit. The page id stays; its note's file follows the title (the plugin's `/wiki/files`)."""
    state = snap.state
    if edit.page_key not in {i.key for i in shown(rec)}:
        raise WikiInvalid(f"路径里没有这一页：{edit.page_key}")
    page_id = dict(zip([p.page_id for p in rec.path.pages], rec.page_ids, strict=True)).get(edit.page_key)
    changes: dict[str, list[PathEdit]] = {rec.path.path_id: [RetitleEdit(page_key=edit.page_key, title=edit.title)]}
    wiki: tuple[WikiEvent, ...] = ()
    if page_id is not None:
        for pid, other in state.paths.items():
            keys = [p.page_id for p, w in zip(other.path.pages, other.page_ids, strict=True)
                    if w == page_id and p.page_id != edit.page_key and p.page_id in {i.key for i in shown(other)}]
            if keys:
                changes.setdefault(pid, []).extend(RetitleEdit(page_key=k, title=edit.title) for k in keys)
        page = state.pages.get(page_id)
        if page is not None and page.title != edit.title:
            wiki = (WikiEvent(event_id=new_id("wev"), ts=now,
                              body=PageSaved(record=page.model_copy(update={"title": edit.title}), action="renamed")),)
    after, snap = commit_paths(repo, snap, changes, now=now, new_id=new_id, source="user", wiki=wiki)
    return PathEditOut(path=path_items(snap.state, after[rec.path.path_id], snap.k, now))


# --- 拆成多页 ---------------------------------------------------------------------------------

ROWS_UNDO = "rows"      # the undo token of a split that changed no graph (a row of several nodes)


def _rows_for(graph: NodeGraph, rec: PathRecord, node_ids,
              questions: Mapping[str, str] | None = None) -> tuple[ReplaceItem, ...]:
    """One new row per node, titled with the node's name, with keys the path does not use;
    `questions`: node id -> the question the split wrote for its page."""
    taken = [i.key for i in apply_edits(rec.path.pages, rec.edits)] \
        + [p.page_id for p in rec.path.pages]
    items: list[ReplaceItem] = []
    for nid in node_ids:
        title = _node_of(graph, nid).name
        key = user_key(title, nid, [*taken, *(i.page_key for i in items)])
        items.append(ReplaceItem(page_key=key, title=title, node_id=nid,
                                 question=(questions or {}).get(nid, "")))
    return tuple(items)


def split_row(repo: Repo, gw: CountingGateway | None, user_id: str, path_id: str, page_key: str,
              *, now: datetime, new_id: Ids, source: PathSource = "user") -> PathEditOut:
    """POST /wiki/paths/{path_id}/rows/{page_key}/split. A row of one node: one split call (reason
    requested, source path_row, each part's `question` asked) writes its finer children and the
    row is replaced by one row per child, titled with the child's name and its question as the
    row's line; graph change, K, names and the path edit are ONE commit, and `undo` in the answer
    is the graph change for `undo_split_row`. The model may leave the node whole: the path stays,
    only the atomic mark is written, `kept` says why. A row covering several nodes: replaced by
    one row per node (no LLM call, no graph change; `undo` = ROWS_UNDO). Unknown path →
    LookupError; a row that is not shown or has no node → WikiInvalid; no gateway for a
    one-node row → LLMUnavailable; an unusable answer → IntakeError (nothing written); the user's
    logs moved during the call → WikiConflict."""
    snap = repo.load(user_id)
    rec = _record(snap.state, path_id)
    row = next((i for i in shown(rec) if i.key == page_key), None)
    if row is None:
        raise WikiInvalid(f"路径里没有这一页：{page_key}")
    if not row.node_ids:
        raise WikiInvalid("这一页没有对应的点，不能拆成多页")
    if len(row.node_ids) > 1:
        edit = ReplaceEdit(page_key=page_key, items=_rows_for(snap.state.graph, rec, row.node_ids))
        after, done = commit_paths(repo, snap, {path_id: [edit]}, now=now, new_id=new_id,
                                   source=source)
        return PathEditOut(path=path_items(done.state, after[path_id], done.k, now),
                           undo=ROWS_UNDO)
    if gw is None:
        raise LLMUnavailable("请切换模型重试")
    node_id = row.node_ids[0]
    # one run for the call and the commit (current_run_id() at the commit point)
    with split_step(user_id, node_id, source="path_row", reason="requested",
                    path_id=path_id):
        res = ask_split(gw, snap, node_id, reason="requested", source="path_row", now=now,
                        question=True)
        ev = split_events(snap, node_id, res, reason="requested", words="", now=now,
                          new_id=new_id)
        if ev.change is None:
            done = repo.commit(snap, Commit(wiki=ev.wiki)) if ev.wiki else snap
            return PathEditOut(path=path_items(done.state, rec, done.k, now), kept=ev.kept)
        edit = ReplaceEdit(page_key=page_key,
                           items=_rows_for(ev.graph, rec, ev.children, ev.questions))
        after, done = commit_paths(repo, snap, {path_id: [edit]}, now=now, new_id=new_id,
                                   source=source, aspect=ev.aspect, wiki=tuple(ev.wiki),
                                   graph=ev.graph)
    return PathEditOut(path=path_items(done.state, after[path_id], done.k, now),
                       undo=ev.change.change_id)


def undo_split_row(repo: Repo, user_id: str, path_id: str, page_key: str, change_id: str, *,
                   now: datetime, new_id: Ids, source: PathSource = "user") -> PathEditOut:
    """POST /wiki/paths/{path_id}/rows/{page_key}/split/undo: the original row back (its
    replacements hidden) and, for a graph split, `nodes.ops.undo` of its change — one commit.
    Refused (WikiInvalid, nothing written) unless the row's latest replace is still active and
    `change_id` is that split: a "split" change of the row's node whose new nodes are exactly the
    replacements' nodes (ROWS_UNDO: a replace of a several-node row by its own nodes). A graph
    change that cannot be undone any more → WikiConflict (409)."""
    snap = repo.load(user_id)
    rec = _record(snap.state, path_id)
    active: ReplaceEdit | None = None
    for e in rec.edits:
        if isinstance(e, ReplaceEdit) and e.page_key == page_key:
            active = e
        elif isinstance(e, UnreplaceEdit) and e.page_key == page_key:
            active = None
    row = next((i for i in apply_edits(rec.path.pages, rec.edits)
                if i.key == page_key and i.removed), None)
    if active is None or row is None:
        raise WikiInvalid(f"这一页没有被拆开：{page_key}")
    kids = {i.node_id for i in active.items}
    graph = snap.state.graph
    if change_id == ROWS_UNDO:
        if len(row.node_ids) < 2 or kids != set(row.node_ids):
            raise WikiInvalid("这一页不是按它自己的点拆开的，不能这样撤销")
    else:
        change = next((c for c in graph.changes if c.change_id == change_id), None)
        if change is None:
            raise LookupError(f"没有这次图谱改动：{change_id}")
        if (change.kind != "split" or len(row.node_ids) != 1 or row.node_ids[0] != change.node_ids[0]
                or kids != set(change.node_ids[1:])):
            raise WikiInvalid("这次图谱改动不是这一页的拆分")
        try:
            done = undo_split(repo, snap, change_id, now=now, new_id=new_id, source=source)
        except UndoConflict as e:
            raise WikiConflict(NOT_UNDOABLE) from e
        return PathEditOut(path=path_items(done.state, done.state.paths[path_id], done.k, now))
    after, done = commit_paths(repo, snap, {path_id: [UnreplaceEdit(page_key=page_key)]},
                               now=now, new_id=new_id, source=source)
    return PathEditOut(path=path_items(done.state, after[path_id], done.k, now))


# --- the Bot's edits (B5b) -------------------------------------------------------------------

def _touched_by_user(i) -> bool:
    return i.moved or i.removed or i.mine or bool(i.skipped_nodes)


def bot_edited(state: WikiState, rec: PathRecord, content: dict) -> PathRecord:
    """The record after the Bot's edit (weekly review, B5 spec §1.3): `op` "move" puts the page
    `page_key` right before `before_key` in the shown order; "add" inserts a page of its own for
    graph node `node_id` (title `title`) right before `before_key`. The Bot only moves and adds:
    it never touches a page the user moved, removed, added or skipped, and never adds a node the
    path already shows. WikiConflict (with a reason for the user) when the edit no longer fits."""
    items = shown(rec)
    keys = [i.key for i in items]
    before = content.get("before_key")
    if before not in keys:
        raise WikiConflict("要放在它前面的那一页已经不在路径里了")
    at = keys.index(before)
    if content.get("op") == "move":
        key = content.get("page_key")
        item = next((i for i in items if i.key == key), None)
        if item is None or item.page is None:
            raise WikiConflict("要挪的那一页已经不在路径里了")
        if _touched_by_user(item):
            raise WikiConflict("这一页你自己调整过，Bot 不动它")
        if keys.index(key) <= at:                    # already before it (or it is that page)
            raise WikiConflict("这一页已经在前面了")
        if rec.page_ids[stored_index(rec, key)] is not None:
            raise WikiConflict("这一页已经开始学了，Bot 不再挪它")
        jumped = items[at:keys.index(key)]
        hit = next((i for i in jumped if page_needs(state, item) & set(i.node_ids)), None)
        if hit is not None:
            raise WikiConflict(f"挪过去会排到它要用到的「{hit.title}」前面，Bot 不挪")
        return _with(state.graph, rec, MoveEdit(page_key=key, to_index=at, by="bot"))
    if content.get("op") == "add":
        node_id = content.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise WikiConflict("要补的概念不清楚")
        node = _node(state, node_id)
        if _has_node(rec, node_id) or any(
                node_id in i.node_ids for i in apply_edits(rec.path.pages, rec.edits)):
            raise WikiConflict("路径里已经有这一页了")
        title = content.get("title") or node.name
        key = user_key(title, node_id, [i.key for i in apply_edits(rec.path.pages, rec.edits)]
                       + [p.page_id for p in rec.path.pages])
        concepts = rec.path.concepts
        if node_id not in {c.id for c in concepts}:
            concepts = (*concepts, own_concept(node_id, node.name, state.graph.prereqs(node_id)))
        path = rec.path.model_copy(update={"pages": (*rec.path.pages, own_page(key, node_id,
                                                                                title)),
                                           "concepts": concepts})
        edits = (*rec.edits, AddEdit(page_key=key, title=title, node_id=node_id, by="bot"),
                 MoveEdit(page_key=key, to_index=at, by="bot"))
        return rec.model_copy(update={"path": path, "page_ids": (*rec.page_ids, None),
                                      "edits": edits})
    raise WikiConflict(f"不认识的路径调整：{content.get('op')!r}")


# --- node actions ----------------------------------------------------------------------------

def map_paths(state: WikiState, map_id: str) -> list[str]:
    """The goal's paths in map order (the path any map node hangs: a point's, an older map's
    region's). Unknown map → LookupError."""
    m = state.maps.get(map_id)
    if m is None:
        raise LookupError(f"没有这个目标：{map_id}")
    return list(dict.fromkeys(n.path_id for n in m.nodes
                              if n.path_id is not None and n.path_id in state.paths))


def _goal_path(state: WikiState, map_id: str, node_id: str) -> str | None:
    """The path of the goal a node belongs to: the path of the map node that is the node, else
    one of its part_of ancestors, else the goal's first path."""
    paths = map_paths(state, map_id)
    m = state.maps[map_id]
    own = m.node(node_id)
    if own is not None and own.path_id in paths:
        return own.path_id
    ancestors = {node_id, *state.graph.ancestors(node_id)}
    for n in m.nodes:
        if n.id in ancestors and n.path_id in paths:
            return n.path_id
    return paths[0] if paths else None


def _out(state: WikiState, k, path_id: str | None, now: datetime) -> NodeActionOut:
    if path_id is None:
        return NodeActionOut()
    return NodeActionOut(path_id=path_id,
                         path=path_items(state, state.paths[path_id], k, now))


@dataclass(frozen=True)
class SkipPlan:
    """What 「我会了」 writes (`skip_plan`): the K events and K-change pass, the `skip` edit per
    path, and what an undo needs (`declared`: each cell's declaration before, `notes`: the
    evidence notes written)."""

    snap: Snapshot
    aspect: tuple[AspectEvent, ...]
    wiki: tuple[WikiEvent, ...]
    changes: dict[str, list[PathEdit]]
    declared: dict[str, str | None]
    notes: tuple[str, ...]            # AddNote event ids


def skip_plan(repo: Repo, gw: CountingGateway | None, user_id: str, node_id: str,
              map_id: str | None, *, now: datetime, new_id: Ids) -> SkipPlan:
    """Unknown node or goal → LookupError; a node with no cells in K → WikiConflict (nothing to
    record the learner's words on)."""
    snap = repo.load(user_id)
    _node(snap.state, node_id)
    paths = map_paths(snap.state, map_id) if map_id is not None else []
    if node_id not in snap.k.concept_ids() or not snap.k.aspects(node_id):
        raise WikiConflict(f"这个节点还没有可以记录的格子：{node_id}")
    aspect: list[AspectEvent] = []
    declared: dict[str, str | None] = {}
    notes: list[str] = []
    for a in snap.k.aspects(node_id):
        cell = cell_id(node_id, a)
        declared[cell] = snap.k.belief(cell, now).declared
        note = AddNote(event_id=new_id("aev"), ts=now, cell=cell, text=SKIP_WORDS,
                       evidence_ref=f"skip:{node_id}", label=SKIP_WORDS)
        notes.append(note.event_id)
        aspect += [Declare(event_id=new_id("aev"), ts=now, cell=cell, value="know"), note]
    kc = k_changed(snap, apply_in_memory(snap, aspect), snap.state.current_page, gw,
                   skip=None, now=now, new_id=new_id, action="declare")
    changes: dict[str, list[PathEdit]] = {}
    for pid in paths:
        keys = [it.key for it in shown(snap.state.paths[pid]) if node_id in it.node_ids]
        if keys:
            changes[pid] = [SkipEdit(page_key=key, node_ids=(node_id,)) for key in keys]
    return SkipPlan(snap=snap, aspect=tuple(aspect), wiki=tuple(kc.wiki), changes=changes,
                    declared=declared, notes=tuple(notes))


def want_node(repo: Repo, user_id: str, node_id: str, map_id: str, *, now: datetime,
              new_id: Ids) -> NodeActionOut:
    """「我想学这个」 (module doc). Unknown node or goal → LookupError; a goal without a path →
    WikiConflict."""
    snap = repo.load(user_id)
    node = _node(snap.state, node_id)
    map_paths(snap.state, map_id)
    path_id = _goal_path(snap.state, map_id, node_id)
    if path_id is None:
        raise WikiConflict("这个目标还没有学习路径")
    if any(_has_node(snap.state.paths[p], node_id) for p in map_paths(snap.state, map_id)):
        return _out(snap.state, snap.k, path_id, now)
    rec = snap.state.paths[path_id]
    new = _add_own(snap.state, rec, node.name, node_id, want=True)
    _, done = commit_paths(repo, snap, {path_id: list(new.edits[len(rec.edits):])}, now=now,
                           new_id=new_id, source="user")
    return _out(done.state, done.k, path_id, now)
