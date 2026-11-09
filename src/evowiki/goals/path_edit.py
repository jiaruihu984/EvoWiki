"""User edits of a learning path (unified node graph plan T7, spec §4 「学习路径可编辑」).

A stored path keeps the pages the planner made (`LearningPath.pages`, stored order) plus the
user's edits, in the order they were made (`PathRecord.edits`). What the user sees is
`apply_edits(pages, edits)`: the edits replayed over the stored pages. Nothing is renumbered:
a page's identity is its `key` = `PathPage.page_id` (`{path_id}-p{i}` for planned pages,
`u-<sha1>` for the pages the user added, `user_key`).

Edits (`op`):
- `move(page_key, to_index)`: take the page out and put it at `to_index` of the shown list
  (0-based, removed pages not counted) — the usual array move.
- `remove(page_key)`: the page is hidden (kept with `removed`, so a replan does not bring it back).
- `unremove(page_key)`: undo of a `remove` — the row is shown again exactly where it was (replay
  only flips the flag: its place in the list never changed).
- `add(page_key, title, node_id, to_index)` / `want(page_key, node_id, title)`: a page of the
  user's own (`mine`), appended at the end (`to_index`: put at that place of the shown list
  instead). With a node it is a real path page (the caller stores a
  `PathPage` with that key, so 「下一页」 can start it); a title-only page is shown but has no
  concept to teach, so it is never started.
- `replace(page_key, items)`: 「拆成多页」: the row is taken out (kept hidden, like `remove`) and
  `items` (each a page of the user's own, `mine`) stand in its place, in order.
- `unreplace(page_key)`: undo of a `replace` — the row comes back where it was and the items that
  replaced it are hidden.
- `skip(page_key, node_ids)`: the learner said 「我会了」 for these nodes; a page is `skipped` once
  every node it teaches was skipped.
- `unskip(page_key, node_ids)`: undo of a `skip` (the assistant's 「我早会了」 was wrong).

An edit on a key that is not there (a page a replan dropped) is ignored on replay; the endpoints
refuse such edits before storing them.

Replanning (`merge_replan`): the planner proposes a new path; the user's work is kept —
started / done pages stay where they are, the user's own pages stay, a removed page stays removed
when the proposal has a page with the same concepts, skipped pages stay skipped, pages the user
moved (and the user's own pages) keep the place the user gave them in the shown list. The
proposal's other pages go in after the pages that teach their prerequisites, around them. The result is stored as a fresh, minimal edit list whose replay gives exactly
the merged order.

Pure: no IO, no LLM."""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Annotated, Literal, Protocol

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field

from evowiki.goals.contracts import Aspect
from evowiki.goals.guardrails import PageConcept
from evowiki.goals.path import LearningPath, PageStatus, PathPage

PathBy = Literal["user", "assistant", "bot"]


class _Edit(BaseModel):
    """`by`: who made the edit. The Bot only moves and adds (B5 spec §1.3); its edits are shown
    like the user's but a replan may re-order them (they are not the user's fixed choices). The
    reading assistant acts on the user's request in chat: its edits ("assistant") count as the
    user's own (fixed through a replan, never touched by the Bot)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_key: str = Field(min_length=1)
    by: PathBy = "user"


class MoveEdit(_Edit):
    op: Literal["move"] = "move"
    to_index: int = Field(ge=0)


class RemoveEdit(_Edit):
    op: Literal["remove"] = "remove"


class UnremoveEdit(_Edit):
    op: Literal["unremove"] = "unremove"


class AddEdit(_Edit):
    op: Literal["add"] = "add"
    title: str = Field(min_length=1)
    node_id: str | None = None
    to_index: int | None = Field(default=None, ge=0)     # None: the end of the shown list
    question: str = ""      # the question the page answers (the row's line); the assistant's new pages set it


class ReplaceItem(BaseModel):
    """One row that takes the place of a replaced row (a page of the user's own)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    node_id: str | None = None
    question: str = ""      # the question the page answers (the row's line), when the split wrote one


class ReplaceEdit(_Edit):
    op: Literal["replace"] = "replace"
    items: tuple[ReplaceItem, ...] = Field(min_length=1)


class UnreplaceEdit(_Edit):
    op: Literal["unreplace"] = "unreplace"


class RetitleEdit(_Edit):
    """He renamed the page: the row's title, whatever the plan called it."""

    op: Literal["retitle"] = "retitle"
    title: str = Field(min_length=1)


class SkipEdit(_Edit):
    op: Literal["skip"] = "skip"
    node_ids: tuple[str, ...] = Field(min_length=1)


class UnskipEdit(_Edit):
    op: Literal["unskip"] = "unskip"
    node_ids: tuple[str, ...] = Field(min_length=1)


class WantEdit(_Edit):
    op: Literal["want"] = "want"
    node_id: str = Field(min_length=1)
    title: str = Field(min_length=1)


PathEdit = Annotated[MoveEdit | RemoveEdit | UnremoveEdit | AddEdit | SkipEdit | UnskipEdit
                     | WantEdit | ReplaceEdit | UnreplaceEdit | RetitleEdit,
                     Field(discriminator="op")]
OwnEdit = AddEdit | WantEdit


@dataclass(frozen=True)
class Item:
    """One page as the user sees it. `page`: the stored path page (None: a title-only page of
    the user's own); `node_ids`: the nodes it teaches (the page's targets)."""

    key: str
    title: str
    node_ids: tuple[str, ...]
    page: PathPage | None = None
    question: str = ""               # the row's line: the question the page answers
    mine: bool = False
    removed: bool = False
    moved: bool = False              # moved by the user (a replan keeps the place)
    skipped_nodes: frozenset[str] = field(default_factory=frozenset)
    bot_moved: bool = False          # moved by the Bot only (a replan may re-order it)

    @property
    def skipped(self) -> bool:
        return bool(self.node_ids) and set(self.node_ids) <= self.skipped_nodes


def _own_item(e: OwnEdit | ReplaceItem, pages: dict[str, PathPage]) -> Item:
    node = e.node_id
    page = pages.get(e.page_key)
    question = getattr(e, "question", "") or (page.question if page is not None else "")
    return Item(key=e.page_key, title=e.title, node_ids=(node,) if node else (), page=page,
                question=question, mine=getattr(e, "by", "user") != "bot")   # ReplaceItem has no `by`: mine


def own_edits(edits: Iterable[PathEdit]) -> dict[str, OwnEdit]:
    """The edit that brings each page of the user's own into the list (a replace's items as
    `add`s), by key."""
    out: dict[str, OwnEdit] = {}
    for e in edits:
        if isinstance(e, AddEdit | WantEdit):
            out[e.page_key] = e
        elif isinstance(e, ReplaceEdit):
            out.update({i.page_key: AddEdit(page_key=i.page_key, title=i.title, node_id=i.node_id,
                                        question=i.question)
                        for i in e.items})
    return out


def _item(p: PathPage) -> Item:
    return Item(key=p.page_id, title=p.title or p.objective, node_ids=p.targets, page=p,
                question=p.question)


def visible(items: Iterable[Item]) -> list[Item]:
    """The pages shown (removed ones hidden)."""
    return [i for i in items if not i.removed]


def _place(rest: list[Item], item: Item, to_index: int) -> list[Item]:
    """`item` put at `to_index` of the shown list `rest` (removed pages not counted)."""
    shown = [n for n, i in enumerate(rest) if not i.removed]
    pos = shown[to_index] if to_index < len(shown) else len(rest)
    return rest[:pos] + [item] + rest[pos:]


def _move(items: list[Item], key: str, to_index: int, by: PathBy = "user") -> list[Item]:
    """A move by the user (or the assistant, at the user's request) pins the page (`moved`); a
    move by the Bot only marks it `bot_moved` (a replan may re-order it)."""
    at = next(n for n, i in enumerate(items) if i.key == key)
    item = replace(items[at], bot_moved=True) if by == "bot" else replace(items[at], moved=True)
    return _place(items[:at] + items[at + 1:], item, to_index)


def apply_edits(pages: Sequence[PathPage], edits: Sequence[PathEdit]) -> list[Item]:
    """The stored pages with the edits replayed: every page once, in the order the user sees,
    removed pages included (flagged; `visible` hides them). The user's own pages enter at their
    add edit (a stored page with the same key gives them their content)."""
    own = set(own_edits(edits))
    by_key = {p.page_id: p for p in pages}
    items = [_item(p) for p in pages if p.page_id not in own]
    replaced: dict[str, ReplaceEdit] = {}
    for e in edits:
        at = next((n for n, i in enumerate(items) if i.key == e.page_key), None)
        match e:
            case AddEdit() | WantEdit():
                if at is None:
                    item = _own_item(e, by_key)
                    to = e.to_index if isinstance(e, AddEdit) else None
                    items = items + [item] if to is None else _place(items, item, to)
            case ReplaceEdit() if at is not None:
                kids = [_own_item(i, by_key) for i in e.items
                        if i.page_key not in {x.key for x in items}]
                items[at] = replace(items[at], removed=True)
                items[at + 1:at + 1] = kids
                replaced[e.page_key] = e
            case UnreplaceEdit() if at is not None and e.page_key in replaced:
                gone = {i.page_key for i in replaced.pop(e.page_key).items}
                items = [replace(i, removed=True) if i.key in gone else i for i in items]
                items[at] = replace(items[at], removed=False)
            case MoveEdit() if at is not None:
                items = _move(items, e.page_key, e.to_index, e.by)
            case RetitleEdit() if at is not None:
                page = items[at].page
                items[at] = replace(items[at], title=e.title,
                                    page=None if page is None else page.model_copy(update={"title": e.title}))
            case RemoveEdit() if at is not None:
                items[at] = replace(items[at], removed=True)
            case UnremoveEdit() if at is not None:
                items[at] = replace(items[at], removed=False)
            case SkipEdit() if at is not None:
                items[at] = replace(items[at],
                                    skipped_nodes=items[at].skipped_nodes | set(e.node_ids))
            case UnskipEdit() if at is not None:
                items[at] = replace(items[at],
                                    skipped_nodes=items[at].skipped_nodes - set(e.node_ids))
    return items


def user_key(title: str, node_id: str | None, taken: Iterable[str]) -> str:
    """A key for a page the user adds: `u-` + 8 hex of sha1(title|node), `-2`, `-3`… when taken."""
    used = set(taken)
    base = "u-" + hashlib.sha1(f"{title}|{node_id or ''}".encode()).hexdigest()[:8]
    key, n = base, 1
    while key in used:
        n += 1
        key = f"{base}-{n}"
    return key


def own_page(key: str, node_id: str, title: str, question: str = "") -> PathPage:
    """The stored path page of a page the user added for a node: it teaches that node."""
    return PathPage(page_id=key, targets=(node_id,), concepts=(node_id,), objective=title,
                    new_cells=0, title=title, question=question)


def own_concept(node_id: str, name: str, prereqs: Sequence[str] = (),
                subitems: dict[Aspect, tuple[str, ...]] | None = None) -> PageConcept:
    return PageConcept(id=node_id, name=name or node_id, prereqs=tuple(prereqs),
                       subitems=subitems or {})


# --- replanning ------------------------------------------------------------------------------

class RecordLike(Protocol):
    """What merge_replan reads of a stored path (wikiapp.events.PathRecord)."""

    @property
    def path(self) -> LearningPath: ...

    @property
    def page_ids(self) -> tuple[str | None, ...]: ...

    @property
    def edits(self) -> tuple[PathEdit, ...]: ...


@dataclass(frozen=True)
class Merged:
    """The replanned path to store: `path.pages` (stored order), the wiki page ids aligned with
    them, and the edit list whose replay is the merged order (`order`: the keys as shown,
    removed ones included). `dropped`: keys of old pages the proposal no longer has."""

    path: LearningPath
    page_ids: tuple[str | None, ...]
    edits: tuple[PathEdit, ...]
    order: tuple[str, ...]
    dropped: tuple[str, ...]


def _started(item: Item, wiki: dict[str, str | None]) -> bool:
    return item.page is not None and (wiki.get(item.key) is not None
                                      or item.page.status is not PageStatus.NOT_STARTED)


def _fresh_key(want: str, path_id: str, used: set[str]) -> str:
    if want not in used:
        return want
    nums = [int(m.group(1)) for k in used if (m := re.fullmatch(rf"{re.escape(path_id)}-p(\d+)", k))]
    n = max(nums, default=0) + 1
    while f"{path_id}-p{n}" in used:
        n += 1
    return f"{path_id}-p{n}"


def merge_replan(old: RecordLike, proposal: LearningPath,
                 edits: Sequence[PathEdit] | None = None) -> Merged:
    """Merges a new plan into a path the user has worked on (module doc). `edits`: the user's
    edits (default: `old.edits`). The proposal's pages match old ones by key (pages a replan
    carried over) or by the same set of target concepts; a proposal page whose concepts the
    kept pages already teach is left out."""
    edits = tuple(old.edits if edits is None else edits)
    path_id = old.path.path_id
    wiki = dict(zip((p.page_id for p in old.path.pages), old.page_ids, strict=True))
    items = apply_edits(old.path.pages, edits)
    own = own_edits(edits)

    def fixed(i: Item) -> bool:
        return i.mine or i.moved or i.removed or bool(i.skipped_nodes) or _started(i, wiki)

    keep: dict[str, Item] = {i.key: i for i in items if fixed(i)}
    started = {i.key for i in items if _started(i, wiki)}
    by_targets = {frozenset(i.node_ids): i.key for i in items
                  if not i.mine and i.page is not None and i.key not in started}
    matched: set[str] = set()
    free: list[Item] = []
    used = {i.key for i in items}
    for p in proposal.pages:
        if p.page_id in keep and (keep[p.page_id].mine or (
                p.page_id in started and set(p.targets) == set(keep[p.page_id].node_ids))):
            continue                      # a started page the planner carried over (kept as is)
        old_key = by_targets.get(frozenset(p.targets))
        if old_key is not None and old_key not in matched:
            matched.add(old_key)
            page = p.model_copy(update={"page_id": old_key, "status": PageStatus.NOT_STARTED,
                                        "done_by": None})
            if old_key in keep:
                keep[old_key] = replace(keep[old_key], page=page, title=page.title or
                                        page.objective)
            else:
                free.append(_item(page))
            continue
        covered = {n for i in keep.values() if not i.removed for n in i.node_ids}
        if set(p.targets) <= covered:
            continue
        key = _fresh_key(p.page_id, path_id, used)
        used.add(key)
        free.append(_item(p.model_copy(update={"page_id": key})))
    # removed pages the proposal no longer has are gone; everything else the user fixed stays
    kept = [keep[i.key] for i in items if i.key in keep
            and not (i.removed and not i.mine and i.key not in matched and i.key not in started)]
    kept = [i for i in kept if not (i.removed and i.mine)]
    final = _insert(kept, free, started, [i.key for i in visible(items)])
    dropped = tuple(i.key for i in items if i.key not in {x.key for x in final})
    return _canonical(old, proposal, final, own, wiki, dropped)


def _insert(kept: list[Item], free: list[Item], started: set[str],
            view: Sequence[str] = ()) -> list[Item]:
    """The proposal's pages, in its order, each after the last started page, the previous
    proposal page and every page teaching one of its prerequisites. Then every page the user
    moved or added (not removed) goes back to the place it had in the list the user saw (`view`: the shown
    keys before the replan): user edits always win, no proposal order — a soft prerequisite or a
    recommended edge included — moves a page the user moved."""
    where = {k: n for n, k in enumerate(view)}
    pins = sorted(((where[i.key], i) for i in kept if (i.moved or i.mine) and not i.removed
                   and i.key in where), key=lambda x: x[0])
    pinned = {i.key for _, i in pins}
    out = [i for i in kept if i.key not in pinned]
    last = max((n for n, i in enumerate(out) if i.key in started), default=-1)
    for item in _prereqs_first(free):
        assert item.page is not None
        need = set(item.page.concepts) - set(item.page.targets)
        after = max([last, *(n for n, i in enumerate(out) if need & set(i.node_ids))])
        out.insert(after + 1, item)
        last = after + 1
    for want, item in pins:                  # ascending: the user's relative order is kept
        at, seen = len(out), 0
        for n, i in enumerate(out):
            if seen == want:
                at = n
                break
            seen += not i.removed
        out.insert(at, item)
    return out


def _prereqs_first(free: list[Item]) -> list[Item]:
    """The proposal's pages with every page after the pages teaching its prerequisites (ties and
    cycles: the proposal's order)."""
    pos = {i.key: n for n, i in enumerate(free)}
    graph = nx.DiGraph()
    graph.add_nodes_from(pos)
    for i in free:
        assert i.page is not None
        need = set(i.page.concepts) - set(i.page.targets)
        graph.add_edges_from((j.key, i.key) for j in free if j is not i and need & set(j.node_ids))
    if not nx.is_directed_acyclic_graph(graph):
        return free
    by_key = {i.key: i for i in free}
    return [by_key[k] for k in nx.lexicographical_topological_sort(graph, key=pos.__getitem__)]


def _canonical(old: RecordLike, proposal: LearningPath, final: list[Item],
               own: dict[str, OwnEdit], wiki: dict[str, str | None],
               dropped: tuple[str, ...]) -> Merged:
    stored = [i.page for i in final if i.page is not None and not i.mine]
    stored += [i.page for i in final if i.page is not None and i.mine]
    edits: list[PathEdit] = [RemoveEdit(page_key=i.key) for i in final if i.removed]
    shown = visible(final)
    for n, i in enumerate(shown):
        if i.mine:
            edits.append(own[i.key])
        if i.mine or i.moved:
            edits.append(MoveEdit(page_key=i.key, to_index=n))
    edits += [SkipEdit(page_key=i.key, node_ids=tuple(sorted(i.skipped_nodes)))
              for i in final if i.skipped_nodes]
    have = {c.id for c in proposal.concepts}
    concepts = proposal.concepts + tuple(c for c in old.path.concepts if c.id not in have)
    if not stored:
        raise ValueError("replanned path has no pages")
    path = proposal.model_copy(update={"path_id": old.path.path_id, "goal_key": old.path.goal_key,
                                       "pages": tuple(stored), "concepts": concepts,
                                       "split": len(stored) > 1})
    order = tuple(i.key for i in final)
    replay = apply_edits(path.pages, edits)
    if [i.key for i in visible(replay)] != [i.key for i in shown] \
            or {i.key for i in replay if i.removed} != {i.key for i in final if i.removed}:
        raise ValueError("replanned edits do not reproduce the merged order")
    return Merged(path=path, page_ids=tuple(wiki.get(p.page_id) for p in stored),
                  edits=tuple(edits), order=order, dropped=dropped)
