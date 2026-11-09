"""Executors of the assistant's page / path / goal actions (assistant-actions spec §1.2, §1.3, §7,
§11): `page_split`, `page_merge`, `page_new` (incl. the `summary` mode of a cheat sheet),
`page_practice`, `path_edit` (reorder / postpone), `goal_deadline`, `goal_new`, `goal_pause`.
(`rewrite` keeps `assistant_edit`.)

Every executor follows `AssistantEditExecutor`: the structure change is ONE commit that carries the
ledger's `effect` and the row's `undo` (`Commit.extra`), so it is all-or-nothing. Pages an approve
creates are NOT written by it (page_split, page_merge): each is written when the learner opens it.
Only `page_new` (write_page / cheat_sheet: an explicit ask for a page now) requests the body writes
after the commit (`request_write(user_id, page_ids) -> run ids`, in path order, first page first). A failed request does not undo the structure: the result says `write_error` and the
page's own run shows 「没写成 · 重试」 (§7). Path edits go through `wikiapp.path_edits.commit_paths`
(`source="assistant"`), the single check + commit + notify point.

Content shapes (what Task 5's tools propose; a draft is edited with `revise`, spec §11):
- `pages`: `[{"key", "title", "covers"?, "node_id"?, "concepts"?}]` - the draft's page list. A
  draft handle `key` is not the path row's key (rows get theirs at execution); `revise` accepts
  `{"pages":[{"key"|"title", "covers"?}]}`: reorder, drop (a key left out), add (no key = a new
  page, title only). Title-only pages get a node of their own when they run.
- page_split `{path_id, page_key, pages}`; page_new `{mode: "page"|"summary", path_id?, index?,
  pages}`; page_merge `{title, pages: [{key: <page id>}]}`; page_practice `{page_id, kind,
  covers?, draft}` (draft: `bot.practice.PracticeDraft`, composed when proposed); path_edit `{path_id, op: "reorder"|"postpone", pages: [{key: <row key>}], after?}`;
  goal_deadline `{goal_id, deadline: "YYYY-MM-DD"}`; goal_pause `{goal_id, paused}`; goal_new
  `{goal, words?, source_ids?}` (goal may be empty with source_ids; a blocks draft adds `blocks`,
  `limits`, `beginner?`: the goal record S1 wrote).
Freshness: `ActionRow.page_id` / `page_version` (a `page_fingerprint`) and `content["versions"]`
(page id -> fingerprint) are checked at execution -> `ActionRefused(stale=True)`.

Undo (`ActionRefused` with the reason when a later change depends on it, same style as
`GraphUndoRefused`): cancels the in-flight writes of the new pages, takes the rows out / the
originals back, and puts the new pages in the trash; refused when a later path edit touched the
rows, the path was replanned meanwhile, or a new page was finished."""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError

from evowiki.agent.effects import payload_hash
from evowiki.belief.aspects import AspectEvent
from evowiki.bot.actions import (
    SPLIT_FRESH,
    ActionExecutor,
    ActionLedger,
    ActionRefused,
    ActionRow,
    NotDecidable,
    strictest,
)
from evowiki.bot.settings import Level, Scope
from evowiki.goals.blocks import LEVELS, block_of
from evowiki.goals.path_edit import (
    AddEdit,
    MoveEdit,
    PathEdit,
    RemoveEdit,
    ReplaceEdit,
    ReplaceItem,
    SkipEdit,
    UnremoveEdit,
    UnreplaceEdit,
    user_key,
)
from evowiki.goals.reading import ReadingPlan
from evowiki.nodes.model import NodeGraph, NodeRec
from evowiki.plain_text import norm_name
from evowiki.store import bot as store
from evowiki.store import bot_goals
from evowiki.wikiapp.events import (
    DraftClosed,
    GoalMapSaved,
    GoalSettings,
    GoalSettingsSet,
    MergedFrom,
    PagesTrashed,
    UsageAdded,
    UsageRecord,
    WikiEvent,
)
from evowiki.wikiapp.goal import page_events_for, same_goal
from evowiki.wikiapp.goal_sources import (
    GoalSourceError,
    create_goal_from_blocks_with_sources,
    load_materials,
)
from evowiki.wikiapp.graph_sync import graph_events
from evowiki.wikiapp.kwrite import apply_in_memory
from evowiki.wikiapp.llm import CountingGateway, LLMUnavailable
from evowiki.wikiapp.map_edits import PathBlockOp, edit_blocks, edit_map
from evowiki.wikiapp.page_merge import merged_text
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.path_edits import build_path_edit, commit_paths, map_paths
from evowiki.wikiapp.path_order import shown, stored_index
from evowiki.wikiapp.reading import create_reading_goal_with_source, reading_map_id
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict, WikiInvalid
from evowiki.wikiapp.state import WikiState, apply_event
from evowiki.wikiapp.wiki_schemas import MapEditOpIn

__all__ = [
    "KINDS",
    "SCOPES",
    "SUMMARY_GOAL",
    "GoalDeadlineExecutor",
    "GoalNewExecutor",
    "GoalPauseExecutor",
    "PageMergeExecutor",
    "PageNewExecutor",
    "PagePracticeExecutor",
    "PageSplitExecutor",
    "PathEditExecutor",
    "PathEditRouter",
    "PracticeWriter",
    "SourceUseExecutor",
    "action_level",
    "path_fingerprint",
    "register_assistant_executors",
]

log = logging.getLogger(__name__)

RequestWrite = Callable[[str, list[str]], list[str]]
CancelWrites = Callable[[str, list[str]], None]
Ids = Callable[[str], str]

# the scopes each kind touches (spec §1.2 / §1.3); the strictest level decides (§1.8)
SCOPES: dict[str, tuple[Scope, ...]] = {
    "page_split": ("new_page", "structure"),
    "page_merge": ("delete_merge", "new_page"),
    "page_new": ("new_page",),
    "page_practice": ("page_body",),
    "page_revise": ("page_body",),
    "path_edit": ("path",),                  # B5's scope 「学习路径顺序」
    "goal_deadline": ("structure", "path"),   # it re-orders the path too
    "goal_new": ("structure",),
    "goal_pause": ("structure",),
    "goal_limits": ("preference",),          # his conditions for a goal, said in the chat (S1)
    "path_blocks": ("path",),                # a confirmed goal's blocks, edited in the chat
    # graph / card / rule / settings kinds: their executors are in `bot.executors_graph`
    "node_split": ("structure",),
    "node_relation": ("structure",),
    "node_known": ("structure",),
    "node_move": ("structure",),
    "card_new": ("suggestion",),
    "card_fix": ("suggestion",),
    "review_postpone": ("suggestion",),
    # a preference waiting for his confirmation / one he stated in this message (land_preference)
    "rule": ("teaching_params",),
    "stated_rule": ("teaching_params",),
    "restyle_pages": ("teaching_params",),  # old pages rewritten under a newer rule (T7)
    "settings": ("preference",),
    "exemplar": ("teaching_params",),       # a page / note set as a 样板页 (chat-first)
    # a chat attachment / material marked as past exam papers or kept to learn (materials phase 2)
    "source_use": ("suggestion",),
    # a fact he said about himself, written into his profile note (remember_about_me, stage 3)
    "profile_fact": ("suggestion",),
    "profile_forget": ("suggestion",),      # a line of 我 to take out, after his 确认
    "memory_forget": ("suggestion",),       # what names a fact stops being recalled, after his 确认
}
KINDS = tuple(SCOPES)
# the goal a cheat sheet's page is written under (the organizer reads `PageRecord.goal`)
SUMMARY_GOAL = "做一页速查：每个点一两行，附公式，再给一个例子，不展开讲解"
GOAL_NOT_STARTED = "这个目标还没有开始学，不能设置"


def action_level(ledger: ActionLedger, user_id: str, kind: str) -> tuple[Level, Scope]:
    """The level an action of `kind` runs under now - the strictest of its scopes - and the scope
    that decided it (the row's `scope`)."""
    levels = [(ledger.level(user_id, s), s) for s in SCOPES[kind]]
    level = strictest(lv for lv, _ in levels)
    return level, next(s for lv, s in levels if lv == level)


# --- content helpers ------------------------------------------------------------------------

def _pages(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    pages = content.get("pages")
    if not isinstance(pages, list) or not pages or not all(isinstance(p, dict) for p in pages):
        raise ActionRefused("这条动作里没有页面列表")
    return pages


def _quotes(said: Any) -> list[str]:
    """A block row's `said`: its quotes (a list of strings; a single string is one quote)."""
    raw = [said] if isinstance(said, str) else said if isinstance(said, list) else []
    return [q for q in raw if isinstance(q, str) and q.strip()]


def _blocks_of(content: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """A `goal_new` row's blocks (chat-first blocks), each `{key, name, covers: [..], summary,
    definition, target, existing_id, level}` with the types checked; None when the row has none (no `blocks`
    key), [] when they are malformed (the executor refuses that row: never a guessed block). A
    level word outside goals/blocks.LEVELS (or none: an older row) is "". The goal record's part
    of a block (`diagnose`, `said`, `said_level`) is kept as `goals.blocks.block_of` reads it."""
    raw = content.get("blocks")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        return []
    out = []
    for b in raw:
        if not (isinstance(b, Mapping) and isinstance(b.get("key"), str)
                and isinstance(b.get("name"), str) and b["name"].strip()
                and isinstance(b.get("covers", []), list)
                and all(isinstance(x, str) for x in b.get("covers", []))):
            return []
        out.append({"key": b["key"], "name": b["name"].strip(), "covers": list(b.get("covers", [])),
                    "summary": str(b.get("summary") or ""), "definition": str(b.get("definition") or ""),
                    "target": b.get("target") is True,
                    "existing_id": str(b.get("existing_id") or ""),
                    "level": b.get("level") if b.get("level") in LEVELS else "",
                    "diagnose": b.get("diagnose") is not False,
                    "said": _quotes(b.get("said")), "said_level": str(b.get("said_level") or ""),
                    "atomic": b.get("atomic") is True,
                    "sections": _labels_of(b.get("sections")),
                    "exercises": _labels_of(b.get("exercises"))})
    return out


def _labels_of(v: Any) -> list[str]:
    """A block row's section labels (`sections` / `exercises`): the strings of a list, else []."""
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _draft_levels(draft: Any) -> tuple[list[str | None], Any]:
    """A blocks draft's rows' `level` (None = not sent) taken out of `draft["pages"]`, so the
    shared `revise_pages` sees the page shape; a level that is not one of LEVELS or "" is an
    ActionRefused. A draft that is not a page list goes to `revise_pages` as it is (it refuses)."""
    if not isinstance(draft, Mapping) or not isinstance(draft.get("pages"), list):
        return [], draft
    levels: list[str | None] = []
    pages = []
    for d in draft["pages"]:
        if isinstance(d, Mapping) and "level" in d:
            if d["level"] != "" and d["level"] not in LEVELS:
                raise ActionRefused("分级只能是基础、核心、进阶、拓展")
            levels.append(d["level"])
            d = {k: v for k, v in d.items() if k != "level"}
        else:
            levels.append(None)
        pages.append(d)
    return levels, {**draft, "pages": pages}


def revise_pages(content: Mapping[str, Any], draft: Any, *, allow_new: bool,
                 rename: bool = False, allow_empty: bool = False) -> dict[str, Any]:
    """The content after the user's draft edit (spec §11): `draft["pages"]` is the new list - a
    page with a `key` of the proposal keeps it (a `covers` replaces its own; with `rename`, a
    non-empty `title` renames it, everything else kept), a page without one is new (title only;
    refused unless `allow_new`), a proposed page left out is dropped; an empty list only with
    `allow_empty` (a 读懂 draft's prerequisites: none to 补 first). Every problem is an
    ActionRefused (nothing else may escape `revise`: it would be a 500)."""
    if not isinstance(draft, Mapping) or not isinstance(draft.get("pages"), list):
        raise ActionRefused("草稿里没有页面列表")
    old = {p["key"]: p for p in _pages(content) if "key" in p}
    out: list[dict[str, Any]] = []
    for d in draft["pages"]:
        if not isinstance(d, Mapping) or set(d) - {"key", "title", "covers", "isNew"}:
            raise ActionRefused("草稿里的页面格式不对")
        covers = d.get("covers")
        if covers is not None and not isinstance(covers, str):
            raise ActionRefused("草稿里的页面格式不对")
        key = d.get("key")
        if key is not None:
            if key not in old:
                raise ActionRefused(f"草稿里有不认识的页：{key}")
            if any(p["key"] == key for p in out):
                raise ActionRefused(f"草稿里同一页出现了两次：{key}")
            page = dict(old[key])
            if covers is not None:
                page["covers"] = covers
            title = d.get("title")
            if rename and title is not None:
                if not isinstance(title, str) or not title.strip():
                    raise ActionRefused("改名要给一个名字")
                page["title"] = title.strip()
            out.append(page)
            continue
        title = d.get("title")
        if not allow_new:
            raise ActionRefused("这条动作只能在已有的页里调整，不能加新页")
        if not isinstance(title, str) or not title.strip():
            raise ActionRefused("新加的页需要一个标题")
        n = len(out)
        while (new_key := f"new-{n}") in old or any(p["key"] == new_key for p in out):
            n += 1
        out.append({"key": new_key, "title": title.strip(), "covers": covers or ""})
    if not out and not allow_empty:
        raise ActionRefused("至少要留一页")
    return {**content, "pages": out}


def _specs(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The content's pages, validated for execution."""
    out = []
    for p in _pages(content):
        title = p.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ActionRefused("有一页没有标题")
        node = p.get("node_id")
        if node is not None and not isinstance(node, str):
            raise ActionRefused(f"页面「{title}」的点不对")
        key = p.get("key")
        out.append({"key": key if isinstance(key, str) else None, "title": title.strip(), "covers": str(p.get("covers") or ""),
                    "node_id": node, "concepts": tuple(p.get("concepts") or ())})
    return out


# --- shared machinery -----------------------------------------------------------------------

@dataclass(frozen=True)
class _NewPages:
    """Pages to create in one commit: their (key, page id) in the order given, the events that
    create the pages (and the graph nodes), the K events and the graph the rows are read from."""

    rows: tuple[ReplaceItem, ...]
    page_ids: tuple[str, ...]
    wiki: tuple[WikiEvent, ...]
    aspect: tuple[AspectEvent, ...]
    graph: NodeGraph
    added: tuple[str, ...] = ()       # ids of the graph nodes this created


def _wev(new_id: Ids, now: datetime, body) -> WikiEvent:
    return WikiEvent(event_id=new_id("wev"), ts=now, body=body)


def _state_after(state: WikiState, events: Iterable[WikiEvent]) -> WikiState:
    for ev in events:
        state = apply_event(state, ev)
    return state


class _Base:
    """What the structure executors share: the repo, ids and the write queue."""

    def __init__(self, repo: Repo, clock: Callable[[], datetime], new_id: Ids,
                 request_write: RequestWrite, cancel_writes: CancelWrites):
        self._repo, self._clock, self._new_id = repo, clock, new_id
        self._request_write, self._cancel_writes = request_write, cancel_writes

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    # freshness ---------------------------------------------------------------------------

    @staticmethod
    def _fresh(state: WikiState, action: ActionRow) -> None:
        """Every page the proposal was made against is still the version it saw (§7)."""
        seen = dict(action.content.get("versions") or {})
        if action.page_id is not None and action.page_version is not None:
            seen[action.page_id] = action.page_version
        for page_id, version in seen.items():
            rec = state.pages.get(page_id)
            if rec is None or page_id in state.trashed_pages \
                    or page_fingerprint(rec) != version:
                raise ActionRefused("", stale=True)

    @staticmethod
    def _path(state: WikiState, path_id: Any):
        rec = state.paths.get(path_id) if isinstance(path_id, str) else None
        if rec is None:
            raise ActionRefused("这条学习路径已经不在了")
        return rec

    @staticmethod
    def _path_fresh(rec, content: Mapping[str, Any]) -> None:
        """The path is still as it was when the proposal was made (`content["path_seen"]` =
        `path_fingerprint`, set by whoever proposes). Absent: refused (no fail-open)."""
        seen = content.get("path_seen")
        if seen is None:
            raise ActionRefused("这条动作没有记下提议时的路径，不能确认它还适用")
        if seen != path_fingerprint(rec):
            raise ActionRefused("", stale=True)

    # new pages ---------------------------------------------------------------------------

    def _new_pages(self, snap: Snapshot, specs: Sequence[dict[str, Any]], *, taken: Iterable[str],
                   mode: str = "page", with_rows: bool = True) -> _NewPages:
        """Rows + page records for `specs`. A spec with a `node_id` teaches that node (unknown ->
        refused); one without gets a node of its own (named like the title, K concept with the
        default aspects) in the same commit. `taken`: the row keys the path already uses."""
        now, new_id = self._clock(), self._new_id
        graph = snap.state.graph
        nodes: dict[int, str] = {}
        added: list[NodeRec] = []
        for i, sp in enumerate(specs):
            if sp["node_id"] is not None:
                if graph.node(sp["node_id"]) is None:
                    raise ActionRefused(f"图谱里没有这个点：{sp['node_id']}")
                nodes[i] = sp["node_id"]
            elif with_rows or mode != "summary":   # a teaching page needs a target node, also outside a path (PageSpec refuses none); a summary page has none
                node = NodeRec(id=new_id("n"), name=sp["title"])   # `covers` is the row's question, not a definition
                added.append(node)
                nodes[i] = node.id
        wiki: list[WikiEvent] = []
        aspect: tuple[AspectEvent, ...] = ()
        state, k = snap.state, snap.k
        if added:
            graph = graph.updated(nodes=[*graph.nodes, *added])
            aspect_l, wiki = graph_events(state, k, graph, "user_edit", now=now, new_id=new_id)
            aspect = tuple(aspect_l)
            state, k = _state_after(state, wiki), apply_in_memory(snap, aspect)
        used = list(taken)
        rows: list[ReplaceItem] = []
        page_ids: list[str] = []
        for i, sp in enumerate(specs):
            node_id = nodes.get(i)
            key = user_key(sp["title"], node_id, used)
            used.append(key)
            targets = (node_id,) if node_id else ()
            concepts = tuple(dict.fromkeys((*targets, *sp["concepts"])))
            goal = SUMMARY_GOAL if mode == "summary" else sp["title"]
            if sp["covers"]:
                goal = f"{goal}：{sp['covers']}"
            rec, evs = page_events_for(state, k, title=sp["title"], goal=goal,
                                       concepts=concepts, targets=targets,
                                       now=now, new_id=new_id, user_id=snap.user_id)
            state = _state_after(state, evs)
            wiki += evs
            if node_id:
                rows.append(ReplaceItem(page_key=key, title=sp["title"], node_id=node_id,
                                        question=sp["covers"]))
            page_ids.append(rec.page_id)
        return _NewPages(rows=tuple(rows), page_ids=tuple(page_ids), wiki=tuple(wiki),
                         aspect=aspect, graph=graph, added=tuple(n.id for n in added))

    def _request(self, user_id: str, page_ids: list[str]) -> dict[str, Any]:
        """Asks for the body writes (path order, first page first); a failure is reported, not
        raised: the structure is committed already (§7)."""
        if not page_ids:
            return {"run_ids": []}
        try:
            return {"run_ids": list(self._request_write(user_id, page_ids))}
        except Exception as exc:
            log.exception("write request of %s for %s failed", page_ids, user_id)
            return {"run_ids": [], "write_error": f"{type(exc).__name__}: {exc}"[:300]}

    def _cancel(self, user_id: str, page_ids: list[str]) -> None:
        if page_ids:
            self._cancel_writes(user_id, page_ids)

    # undo --------------------------------------------------------------------------------

    def _mark_undone(self, action: ActionRow) -> Callable[[Any], None]:
        def mark(conn) -> None:
            store.set_action_status(conn, action.action_id, status="undone", at=self._clock(),
                                    from_statuses=("executed",))
        return mark

    @staticmethod
    def _undo_data(action: ActionRow) -> dict[str, Any]:
        if not action.undo:
            raise ActionRefused("这条修改没有保存撤销数据，不能撤销")
        return action.undo

    @staticmethod
    def _not_finished(state: WikiState, page_ids: Iterable[str]) -> None:
        for pid in page_ids:
            rec = state.pages.get(pid)
            if rec is not None and rec.done:
                raise ActionRefused(f"「{rec.title}」已经学完了，不能撤销")

    @staticmethod
    def _untouched_since(rec, u: dict[str, Any], keys: Iterable[str], *,
                         any_order_change: bool = False) -> None:
        """No path edit after the action's own touched one of its rows, and the path was not
        replanned (a replan rewrites the edit list). `any_order_change`: no later edit changed
        the order at all (a reorder's undo depends on every row's place)."""
        n = u["edits_after"]
        if len(rec.edits) < n or _prefix(rec)[:n] != u["edits_prefix"]:
            raise ActionRefused("这条路径之后重新规划过了，不能撤销")
        keys = set(keys)
        for e in rec.edits[n:]:
            if (any_order_change and not isinstance(e, SkipEdit)) or e.page_key in keys or (isinstance(e, ReplaceEdit)
                                      and keys & {i.page_key for i in e.items}):
                raise ActionRefused("后面的路径修改用到了这几页，请先撤销那些修改")

    def _trash_event(self, page_ids: Sequence[str], *, trashed: bool) -> list[WikiEvent]:
        return ([_wev(self._new_id, self._clock(), PagesTrashed(page_ids=tuple(page_ids),
                                                                trashed=trashed))]
                if page_ids else [])

    def _commit_undo(self, user_id: str, action: ActionRow, snap: Snapshot,
                     path_changes: dict[str, list[PathEdit]], wiki: list[WikiEvent], *,
                     aspect: Sequence[AspectEvent] = (), graph: NodeGraph | None = None) -> None:
        """The undo's one commit: the path edits (if any), `wiki` (and the graph change with its K
        events), with the row marked undone."""
        mark = self._mark_undone(action)
        if path_changes:
            commit_paths(self._repo, snap, path_changes, now=self._clock(),
                         new_id=self._new_id, source="assistant", wiki=tuple(wiki),
                         aspect=tuple(aspect), graph=graph, extra=[mark])
        elif wiki or aspect:
            self._repo.commit(snap, Commit(aspect=list(aspect), wiki=wiki, extra=[mark]))

    def _drop_nodes(self, snap: Snapshot, node_ids: Sequence[str], *, own_keys: Iterable[str],
                    own_pages: Iterable[str]) -> tuple[list[AspectEvent], list[WikiEvent],
                                                       NodeGraph | None]:
        """The graph without the nodes this action created (K events and names that go with it).
        K cannot forget a concept (documented in `nodes.ksync`), so the concepts stay there with
        no evidence. Refused when something else uses a node now."""
        nodes = [n for n in node_ids if snap.state.graph.node(n) is not None]
        if not nodes:
            return [], [], None
        own_keys, own_pages = set(own_keys), set(own_pages)
        used = {t for pid, pg in snap.state.pages.items()
                if pid not in own_pages and pid not in snap.state.trashed_pages
                for t in (*pg.targets, *pg.concepts)}
        used |= {n for rec in snap.state.paths.values() for it in shown(rec)
                 if it.key not in own_keys for n in it.node_ids}
        if clash := used & set(nodes):
            raise ActionRefused(f"后来别处用到了这几个点：{', '.join(sorted(clash))}，不能撤销")
        try:
            graph = snap.state.graph.updated(
                nodes=[n for n in snap.state.graph.nodes if n.id not in nodes],
                relations=[r for r in snap.state.graph.relations
                           if r.src not in nodes and r.dst not in nodes])
        except Exception as exc:         # GraphError: something hangs on a node
            raise ActionRefused("图谱里后来有东西接在这些点上，不能撤销") from exc
        aspect, wiki = graph_events(snap.state, snap.k, graph, "undone", now=self._clock(),
                                    new_id=self._new_id)
        return list(aspect), list(wiki), graph


def path_fingerprint(rec) -> str:
    """The path as a proposal saw it: the shown rows and how many edits it has had (a replan resets
    the edit list, a user edit adds to it)."""
    return payload_hash({"shown": [i.key for i in shown(rec)], "edits": len(rec.edits)})


def _made(specs: Sequence[Mapping[str, Any]], page_ids: Sequence[str], rec=None, folder: str = "") -> list[dict]:
    """The result's `made`: for each page this created, the draft `key` it came from, its page id
    and (when it is on a path) its 1-based position among the shown rows - what a chat card needs
    to show each draft line's writing status and number."""
    pos: dict[str, int] = {}
    if rec is not None:
        for n, i in enumerate(shown(rec), 1):
            pid = rec.page_ids[stored_index(rec, i.key)]
            if pid is not None:
                pos[pid] = n
    return [{"key": sp["key"], "page_id": pid, "n": pos.get(pid), "folder": folder}
            for sp, pid in zip(specs, page_ids, strict=True)]


def _order_of(rec, keys: set[str]) -> list[str]:
    """The page ids of the shown rows whose key is in `keys`, in the order shown."""
    return [rec.page_ids[stored_index(rec, i.key)] for i in shown(rec)
            if i.key in keys and rec.page_ids[stored_index(rec, i.key)] is not None]


def _prefix(rec) -> list[dict]:
    return [e.model_dump(mode="json") for e in rec.edits]


# --- page_split / page_new ------------------------------------------------------------------

class PageSplitExecutor(_Base):
    """`page_split`: the new pages replace the original row in the path (`ReplaceEdit`); the
    original page stays in the Wiki. The new pages are only created, not written: each one is
    written when the learner opens it (`POST /wiki/pages/{id}/organize`), or by the Bot's idle
    write-ahead; the executor never requests a write (the learner's ruling 2026-10-29)."""

    def revise(self, action: ActionRow, draft: Any) -> dict[str, Any]:
        return revise_pages(action.content, draft, allow_new=True)

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        try:
            snap = self._repo.load(user_id)
            self._fresh(snap.state, action)
            path_id, key = c.get("path_id"), c.get("page_key")
            rec = self._path(snap.state, path_id)
            if not any(i.key == key for i in shown(rec)):
                raise ActionRefused("", stale=True)
            self._path_fresh(rec, c)
            new = self._new_pages(snap, _specs(c), taken=_taken(rec))
            edit = ReplaceEdit(page_key=key, items=new.rows)
            built = build_path_edit(new.graph, rec, [edit], by="assistant")
            undo = {"path_id": path_id, "page_key": key, "keys": [r.page_key for r in new.rows],
                    "page_ids": list(new.page_ids), "nodes": list(new.added),
                    "edits_after": len(built.edits), "edits_prefix": _prefix(built)}
            ids = dict(zip((r.page_key for r in new.rows), new.page_ids, strict=True))
            after, _ = commit_paths(
                self._repo, snap, {path_id: [edit]}, now=self._clock(), new_id=self._new_id,
                source="assistant", aspect=new.aspect, wiki=new.wiki, graph=new.graph,
                started=ids, extra=[effect, _write_undo(action, undo)])
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        out = {"path_id": path_id, "page_key": key, "new_pages": undo["page_ids"],
               "made": _made(_specs(c), new.page_ids, after[path_id]),
               "wiki_event_ids": [e.event_id for e in new.wiki], "note": SPLIT_FRESH}
        return out      # no write request: each new page is written when the learner opens it

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        try:
            snap = self._repo.load(user_id)
            rec = self._path(snap.state, u["path_id"])
            self._untouched_since(rec, u, [u["page_key"], *u["keys"]])
            self._not_finished(snap.state, u["page_ids"])
            aspect, wiki, graph = self._drop_nodes(snap, u.get("nodes", []), own_keys=u["keys"],
                                                   own_pages=u["page_ids"])
            self._cancel(user_id, u["page_ids"])
            self._commit_undo(user_id, action, snap,
                              {u["path_id"]: [UnreplaceEdit(page_key=u["page_key"])]},
                              [*wiki, *self._trash_event(u["page_ids"], trashed=True)],
                              aspect=aspect, graph=graph)
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"restored": u["page_key"]}


class PageNewExecutor(_Base):
    """`page_new`: write_page / cheat_sheet. With a `path_id` the pages become rows of that path
    (at `index`, else the end), otherwise they are pages of their own. `mode="summary"` writes a
    cheat sheet (`SUMMARY_GOAL`)."""

    def revise(self, action: ActionRow, draft: Any) -> dict[str, Any]:
        return revise_pages(action.content, draft, allow_new=True)

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        mode = c.get("mode", "page")
        if mode not in ("page", "summary"):
            raise ActionRefused(f"不认识的写法：{mode}")
        try:
            snap = self._repo.load(user_id)
            self._fresh(snap.state, action)
            path_id = c.get("path_id")
            rec = self._path(snap.state, path_id) if path_id is not None else None
            specs = _specs(c)
            if rec is not None:
                self._path_fresh(rec, c)
                specs = _without_shown_nodes(rec, specs)      # no stub for a page that is not added
                if not specs:
                    raise ActionRefused("这些点在路径上已经有页了")
            new = self._new_pages(
                snap, specs, taken=_taken(rec) if rec else (), mode=mode,
                with_rows=rec is not None)
            if rec is None:
                undo = {"path_id": None, "keys": [], "page_ids": list(new.page_ids)}
                self._repo.commit(snap, Commit(
                    aspect=list(new.aspect), wiki=list(new.wiki),
                    extra=[effect, _write_undo(action, undo)]))
                return {"new_pages": undo["page_ids"], "run_ids": [],
                        "made": _made(specs, new.page_ids),
                        **self._request(user_id, list(new.page_ids)),
                        "wiki_event_ids": [e.event_id for e in new.wiki]}
            index = c.get("index")
            if index is not None and (not isinstance(index, int) or index < 0):
                raise ActionRefused("放页的位置不对")
            ids = dict(zip((r.page_key for r in new.rows), new.page_ids, strict=True))
            edits: list[PathEdit] = [
                AddEdit(page_key=r.page_key, title=r.title, node_id=r.node_id, question=r.question,
                        to_index=None if index is None else index + n)
                for n, r in enumerate(new.rows)]
            built = build_path_edit(new.graph, rec, edits, by="assistant")
            kept = {e.page_key for e in built.edits[len(rec.edits):]}
            if kept != set(ids):             # cannot happen after the filter: never half-commit
                raise ActionRefused("路径刚刚变了，这条动作作废")
            undo = {"path_id": path_id, "keys": list(ids), "page_ids": list(ids.values()),
                    "nodes": list(new.added), "edits_after": len(built.edits),
                    "edits_prefix": _prefix(built)}
            after, _ = commit_paths(
                self._repo, snap, {path_id: edits}, now=self._clock(), new_id=self._new_id,
                source="assistant", aspect=new.aspect, wiki=new.wiki, graph=new.graph,
                started={k: ids[k] for k in kept}, extra=[effect, _write_undo(action, undo)])
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"path_id": path_id, "new_pages": undo["page_ids"],
                "made": _made(specs, new.page_ids, after[path_id]),
                "wiki_event_ids": [e.event_id for e in new.wiki],
                **self._request(user_id, _order_of(after[path_id], set(undo["keys"])))}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        try:
            snap = self._repo.load(user_id)
            changes: dict[str, list[PathEdit]] = {}
            if u["path_id"] is not None:
                rec = self._path(snap.state, u["path_id"])
                self._untouched_since(rec, u, u["keys"])
                changes[u["path_id"]] = [RemoveEdit(page_key=k) for k in u["keys"]]
            self._not_finished(snap.state, u["page_ids"])
            aspect, wiki, graph = self._drop_nodes(snap, u.get("nodes", []), own_keys=u["keys"],
                                                   own_pages=u["page_ids"])
            self._cancel(user_id, u["page_ids"])
            self._commit_undo(user_id, action, snap, changes,
                              [*wiki, *self._trash_event(u["page_ids"], trashed=True)],
                              aspect=aspect, graph=graph)
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"removed": u["page_ids"]}


def _without_shown_nodes(rec, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`specs` without the pages for a node the path already shows (or an earlier spec names):
    `build_path_edit` would drop such an add, and its page record must not be created either."""
    have = {n for it in shown(rec) for n in it.node_ids}
    out = []
    for sp in specs:
        if sp["node_id"] is not None:
            if sp["node_id"] in have:
                continue
            have.add(sp["node_id"])
        out.append(sp)
    return out


def _taken(rec) -> list[str]:
    """Every row key the path uses (stored and added)."""
    from evowiki.goals.path_edit import apply_edits
    return [i.key for i in apply_edits(rec.path.pages, rec.edits)] \
        + [p.page_id for p in rec.path.pages]


def _write_undo(action: ActionRow, undo: dict[str, Any]) -> Callable[[Any], None]:
    def write(conn) -> None:             # in the structure's commit: an executed action has it
        store.set_action_undo(conn, action.action_id, dict(undo))
    return write


# --- page_merge -----------------------------------------------------------------------------

class PageMergeExecutor(_Base):
    """`page_merge`: the merged page is created (not written: it is written when the learner opens
    it, or by the idle write-ahead), from the originals as the
    learner approved them (`PageRecord.merged_from`, `wikiapp.page_merge`); the originals stay
    untouched until it is written, then `settle` puts them in the trash (`PagesTrashed`; the plugin
    moves their files) and gives their path rows to the merged page, in one commit.
    `settle(user_id, action)` is the "on its commit" step: call it when the new page's write has
    finished (it does nothing until then, and runs once)."""

    def revise(self, action: ActionRow, draft: Any) -> dict[str, Any]:
        return revise_pages(action.content, draft, allow_new=False)

    def _originals(self, state: WikiState, action: ActionRow) -> list[str]:
        ids = [p["key"] for p in _pages(action.content)]
        if len(ids) < 2:
            raise ActionRefused("合并至少要两页")
        for pid in ids:
            if pid not in state.pages or pid in state.trashed_pages:
                raise ActionRefused("", stale=True)
        return ids

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        title = c.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ActionRefused("合并后的页需要一个标题")
        try:
            return self._execute(user_id, action, effect, title.strip())
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc

    def _execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None],
                 title: str) -> dict:
        snap = self._repo.load(user_id)
        self._fresh(snap.state, action)
        originals = self._originals(snap.state, action)
        recs = [snap.state.pages[p] for p in originals]
        concepts = tuple(dict.fromkeys(x for r in recs for x in r.concepts))
        targets = tuple(dict.fromkeys(x for r in recs for x in r.targets))
        goal = f"把这几页合成一页：{'、'.join(r.title for r in recs)}"
        # the job's input: the originals as approved (`_fresh` just checked these versions)
        merged_from = tuple(MergedFrom(page_id=r.page_id, title=r.title,
                                       version=page_fingerprint(r), text=merged_text(r))
                            for r in recs)
        rec, evs = page_events_for(snap.state, snap.k, title=title, goal=goal,
                                   concepts=concepts,
                                   targets=targets, now=self._clock(), new_id=self._new_id,
                                   user_id=user_id, merged_from=merged_from)
        undo = {"new_page": rec.page_id, "originals": originals, "settled": False, "trashed": []}
        self._repo.commit(snap, Commit(wiki=evs, extra=[effect, _write_undo(action, undo)]))
        return {"new_page": rec.page_id, "originals": originals,
                "made": _made([{"key": None}], [rec.page_id], None, recs[0].folder),
                "wiki_event_ids": [e.event_id for e in evs]}

    def settle(self, user_id: str, action: ActionRow) -> bool:
        """Once the merged page is written, ONE commit: the originals go to the trash and, on every
        path that shows them, the merged page takes the first one's row and the others' rows are
        removed. An original changed since the learner approved the merge (its version is not the
        one the job carries) stops it: nothing moves, the row says why (`MERGE_CONFLICT`), the
        learner keeps both and may undo the merge. True: settled (or stopped) now."""
        u = action.undo or {}
        if action.status != "executed" or u.get("settled", True):
            return False
        snap = self._repo.load(user_id)
        new = snap.state.pages.get(u["new_page"])
        if new is None or not new.has_body:      # none yet, or a run is still writing it
            return False
        left = [p for p in u["originals"] if p in snap.state.pages
                and p not in snap.state.trashed_pages]
        seen = {m.page_id: m.version for m in new.merged_from}
        changed = [p for p in left if page_fingerprint(snap.state.pages[p]) != seen.get(p)]
        if changed:
            titles = "、".join(f"「{snap.state.pages[p].title}」" for p in changed)
            self._repo.commit(snap, Commit(
                wiki=[_wev(self._new_id, self._clock(),
                           UsageAdded(record=UsageRecord(action="page_merge_conflict",
                                                         text=action.action_id)))],
                extra=[_write_undo(action, {**u, "settled": True, "conflict": changed}),
                       _write_detail(action, MERGE_CONFLICT.format(titles=titles),
                                     at=action.decided_at or self._clock())]))
            return True
        changes, paths = self._path_changes(snap.state, u["originals"], new)
        done = {**u, "settled": True, "trashed": left, "paths": paths}
        trash = self._trash_event(left, trashed=True)
        if changes:
            commit_paths(self._repo, snap, changes, now=self._clock(), new_id=self._new_id,
                         source="assistant", wiki=tuple(trash),
                         started={p["new_key"]: new.page_id for p in paths},
                         extra=[_write_undo(action, done)])
            return True
        self._repo.commit(snap, Commit(
            wiki=trash or [
                _wev(self._new_id, self._clock(),
                     UsageAdded(record=UsageRecord(action="page_merge_settled",
                                                   text=action.action_id)))],
            extra=[_write_undo(action, done)]))
        return True

    def _path_changes(self, state: WikiState, originals: Sequence[str], new
                      ) -> tuple[dict[str, list[PathEdit]], list[dict[str, Any]]]:
        """Per path that shows rows of the originals: the merged page replaces the first such row
        (teaching that row's node) and the other rows are removed; with the undo data per path."""
        gone = set(originals)
        changes: dict[str, list[PathEdit]] = {}
        undo: list[dict[str, Any]] = []
        for path_id, rec in state.paths.items():
            rows = [i for i in shown(rec)
                    if i.page is not None and rec.page_ids[stored_index(rec, i.key)] in gone]
            if not rows:
                continue
            first = rows[0]
            node = first.node_ids[0] if first.node_ids else (new.targets[0] if new.targets else None)
            key = user_key(new.title, node, _taken(rec))
            edits: list[PathEdit] = [
                ReplaceEdit(page_key=first.key, items=(ReplaceItem(page_key=key, title=new.title,
                                                                   node_id=node),)),
                *(RemoveEdit(page_key=i.key) for i in rows[1:])]
            built = build_path_edit(state.graph, rec, edits, by="assistant")
            changes[path_id] = edits
            undo.append({"path_id": path_id, "replaced": first.key,
                         "removed": [i.key for i in rows[1:]], "new_key": key,
                         "edits_after": len(built.edits), "edits_prefix": _prefix(built)})
        return changes, undo

    def undo(self, user_id: str, action: ActionRow) -> dict:
        """The merged page to the trash, the originals this merge trashed back, and the path rows
        it replaced back in place (refused when a later path edit touched them)."""
        u = self._undo_data(action)
        try:
            snap = self._repo.load(user_id)
            self._not_finished(snap.state, [u["new_page"]])
            changes: dict[str, list[PathEdit]] = {}
            for p in u.get("paths", []):
                rec = self._path(snap.state, p["path_id"])
                self._untouched_since(rec, p, [p["replaced"], *p["removed"], p["new_key"]])
                changes[p["path_id"]] = [UnreplaceEdit(page_key=p["replaced"]),
                                         *(UnremoveEdit(page_key=k) for k in p["removed"])]
            self._cancel(user_id, [u["new_page"]])
            wiki = self._trash_event([u["new_page"]], trashed=True)
            back = [p for p in u.get("trashed", []) if p in snap.state.trashed_pages]   # only ours
            wiki += self._trash_event(back, trashed=False)
            self._commit_undo(user_id, action, snap, changes, wiki)
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"restored": back, "removed": u["new_page"]}


MERGE_CONFLICT = ("合并时{titles}又改过，新页里没有这次改动，所以原页没有移走，两页都留着。"
                  "不要新页可以撤销这次合并。")


def _write_detail(action: ActionRow, detail: str, *, at: datetime) -> Callable[[Any], None]:
    def write(conn) -> None:             # the executed row says why its merge stopped
        store.set_action_status(conn, action.action_id, status="executed", at=at, detail=detail,
                                from_statuses=("executed",))
    return write


# --- page_practice --------------------------------------------------------------------------

class PracticeWriter(Protocol):
    """Adds practice (questions / memory cards / an interactive block, `bot.practice`) to a page and
    takes it out again. The block was composed and checked when the action was proposed (the row's
    `content["draft"]`, a `bot.practice.PracticeDraft`); `add` writes it without a model call and
    calls `extra_for(undo)` to commit the page write TOGETHER with the writes it returns (the
    ledger's effect and the undo data), returning `{"undo": {...}, "block_id": str}`; both raise
    ActionRefused (with a reason shown to the user) when the page changed."""

    def add(self, user_id: str, draft: Any, expect_version: str | None,
            extra_for: Callable[[dict[str, Any]], Sequence[Callable[[Any], None]]]
            ) -> dict[str, Any]: ...

    def remove(self, user_id: str, page_id: str, undo: dict[str, Any],
               extra: Sequence[Callable[[Any], None]]) -> None: ...

    def compose(self, user_id: str, page_id: str, kind: str, covers: str, key: str) -> Any: ...


class PagePracticeExecutor:
    """`page_practice`: the injected `PracticeWriter` writes the drafted block and commits it with
    the ledger's effect and the undo data (one commit); undo takes the block out in one commit with
    the row marked undone. A row without a readable draft is refused (nothing to write)."""

    KINDS = ("question", "card", "interactive")

    def __init__(self, repo: Repo, practice: PracticeWriter | None, clock: Callable[[], datetime],
                 new_id: Ids):
        self._repo, self._practice, self._clock, self._new_id = repo, practice, clock, new_id

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None                # the draft is dropped with the row; the page never changed

    def redraft(self, user_id: str, action: ActionRow, key: str) -> tuple[dict, str]:
        """重试 on a draft that no longer fits the page: the same request (kind, covers) composed
        again against the page as it is now (a model call). (the row's new content, its page
        version); ActionRefused in plain words when no usable practice came back."""
        c = action.content
        page_id, kind = c.get("page_id"), c.get("kind")
        if kind not in self.KINDS or not isinstance(page_id, str):
            raise ActionRefused("这条练习草稿读不出来了，没有保存")
        if self._practice is None:
            raise ActionRefused("加练习的写作还没有接上")
        draft = self._practice.compose(user_id, page_id, kind, str(c.get("covers") or ""), key)
        return ({**c, "draft": draft.model_dump(mode="json")}, draft.base_version)

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        from evowiki.bot.practice import PracticeDraft
        c = action.content
        page_id, kind = c.get("page_id"), c.get("kind")
        if kind not in self.KINDS:
            raise ActionRefused(f"不认识的练习：{kind}")
        if self._practice is None:
            raise ActionRefused("加练习的写作还没有接上")
        try:
            draft = PracticeDraft.model_validate(c.get("draft"))
        except ValidationError as exc:
            raise ActionRefused("这条练习草稿读不出来了，没有保存") from exc
        if draft.page_id != page_id:
            raise ActionRefused("这条练习草稿读不出来了，没有保存")
        # no page-version check: an added practice goes onto the page as it is now (the writer's
        # `add` refuses only when its section is gone or renamed)
        snap = self._repo.load(user_id)
        rec = snap.state.pages.get(page_id)
        if rec is None or not rec.has_body:
            raise ActionRefused("这页还没写好，没法加练习")
        try:
            out = self._practice.add(
                user_id, draft, action.page_version,
                lambda undo: [effect, _write_undo(action, {"page_id": page_id, "block": undo})])
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"page_id": page_id, "kind": kind, "block_id": out.get("block_id", draft.block_id)}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        if not action.undo:
            raise ActionRefused("这条修改没有保存撤销数据，不能撤销")
        if self._practice is None:
            raise ActionRefused("加练习的写作还没有接上")
        action_id = action.action_id

        def mark(conn) -> None:
            store.set_action_status(conn, action_id, status="undone", at=self._clock(),
                                    from_statuses=("executed",))
        try:
            self._practice.remove(user_id, action.undo["page_id"], action.undo["block"], [mark])
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"removed": action.undo["block"]}


# --- path_edit ------------------------------------------------------------------------------

def _moves(current: list[str], target: list[str]) -> list[PathEdit]:
    """MoveEdits that turn the shown order `current` into `target` (same keys)."""
    cur, edits = list(current), []
    for i, key in enumerate(target):
        if cur[i] != key:
            cur.remove(key)
            cur.insert(i, key)
            edits.append(MoveEdit(page_key=key, to_index=i))
    return edits


class PathEditExecutor(_Base):
    """`path_edit`: `reorder` (the listed rows go first, in that order; the rest keep their
    order) or `postpone` (the listed rows go after the row `after`, else to the end). Rows the
    learner placed themselves are not special: this is the learner asking in chat."""

    def revise(self, action: ActionRow, draft: Any) -> dict[str, Any]:
        return revise_pages(action.content, draft, allow_new=False)

    def _target(self, c: Mapping[str, Any], current: list[str]) -> tuple[list[str], list[str]]:
        keys = [p["key"] for p in _pages(c)]
        if len(set(keys)) != len(keys) or set(keys) - set(current):
            raise ActionRefused("", stale=True)       # a listed row is not on the path any more
        rest = [k for k in current if k not in keys]
        op = c.get("op")
        if op == "reorder":
            return [*keys, *rest], keys
        if op == "postpone":
            after = c.get("after")
            if after is not None and (after not in rest):
                raise ActionRefused("", stale=True)
            at = len(rest) if after is None else rest.index(after) + 1
            return [*rest[:at], *keys, *rest[at:]], keys
        raise ActionRefused(f"不认识的路径修改：{op}")

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        try:
            snap = self._repo.load(user_id)
            rec = self._path(snap.state, c.get("path_id"))
            self._path_fresh(rec, c)
            current = [i.key for i in shown(rec)]
            target, moved = self._target(c, current)
            edits = _moves(current, target)
            if not edits:
                raise ActionRefused("路径已经是这个顺序了")
            built = build_path_edit(snap.state.graph, rec, edits, by="assistant")
            undo = {"path_id": c["path_id"], "before": current, "moved": moved,
                    "edits_after": len(built.edits), "edits_prefix": _prefix(built)}
            commit_paths(self._repo, snap, {c["path_id"]: edits}, now=self._clock(),
                         new_id=self._new_id, source="assistant",
                         extra=[effect, _write_undo(action, undo)])
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"path_id": c["path_id"], "order": target}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        u = self._undo_data(action)
        try:
            snap = self._repo.load(user_id)
            rec = self._path(snap.state, u["path_id"])
            self._untouched_since(rec, u, u["moved"], any_order_change=True)
            current = [i.key for i in shown(rec)]
            target = [k for k in u["before"] if k in current] \
                + [k for k in current if k not in u["before"]]
            self._commit_undo(user_id, action, snap,
                              {u["path_id"]: _moves(current, target)}, [])
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"restored": u["before"]}


# --- goals ----------------------------------------------------------------------------------

class _GoalBase:
    def __init__(self, repo: Repo, clock: Callable[[], datetime], new_id: Ids):
        self._repo, self._clock, self._new_id = repo, clock, new_id

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    @staticmethod
    def _goal_ids(state: WikiState, goal_id: Any) -> list[str]:
        """The settings keys of a goal: its path, or - for a big goal - every path of its map
        (the setting is fanned out to them, so the review scheduler and the Bot facts, which read
        paths, see it). A big goal without a path yet is refused (nothing would carry the setting)."""
        if isinstance(goal_id, str) and goal_id in state.paths:
            return [goal_id]
        if isinstance(goal_id, str) and goal_id in state.maps:
            ids = map_paths(state, goal_id)
            if not ids:
                raise ActionRefused(GOAL_NOT_STARTED)
            return ids
        raise ActionRefused("这个目标已经不在了")

    @staticmethod
    def _settings(state: WikiState, gid: str) -> GoalSettings:
        return state.goal_settings.get(gid) or GoalSettings(goal_id=gid)

    def _events(self, settings: Iterable[GoalSettings]) -> list[WikiEvent]:
        return [_wev(self._new_id, self._clock(), GoalSettingsSet(settings=g)) for g in settings]

    def _mark(self, action: ActionRow) -> Callable[[Any], None]:
        action_id = action.action_id

        def mark(conn) -> None:
            store.set_action_status(conn, action_id, status="undone", at=self._clock(),
                                    from_statuses=("executed",))
        return mark


def _jsonable(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date) else v


class GoalDeadlineExecutor(_GoalBase):
    """`goal_deadline`: the goal's exam date (a big goal: on each path of its map).

    The review scheduler pulls the reviews due before it (`goals.review.fading` `deadline_days`);
    the forgetting model is not changed. A deadline is about time only: the stored path order is
    not touched (skill design A7: K and deadlines never re-order a stored path; 可以跳过 is derived
    from K by `wikiapp.paths.path_items`). One commit; undo puts the dates back. A date before
    the user's local today (Bot settings' zone) is refused."""

    def __init__(self, pool, repo: Repo, clock: Callable[[], datetime], new_id: Ids):
        super().__init__(repo, clock, new_id)
        self._pool = pool

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        try:
            deadline = date.fromisoformat(str(c.get("deadline")))
        except ValueError as exc:
            raise ActionRefused("截止日期要写成 年-月-日") from exc
        now = self._clock()
        with self._pool.connection() as conn:
            zone = ZoneInfo(store.get_settings(conn, user_id).timezone)
        if deadline < now.astimezone(zone).date():
            raise ActionRefused("这个日期已经过了")
        try:
            snap = self._repo.load(user_id)
            ids = self._goal_ids(snap.state, c.get("goal_id"))
            new: list[GoalSettings] = []
            goals: dict[str, Any] = {}
            for gid in ids:
                cur = self._settings(snap.state, gid)
                if cur.deadline == deadline:
                    continue
                goals[gid] = {"deadline": deadline.isoformat(),
                              "before": {"deadline": _jsonable(cur.deadline)}}
                new.append(cur.model_copy(update={"deadline": deadline}))
            if not new:
                raise ActionRefused("这个目标已经是这个截止日期了")
            commit_paths(self._repo, snap, {}, now=now, new_id=self._new_id,
                         source="assistant", wiki=tuple(self._events(new)),
                         extra=[effect, _write_undo(action, {"goals": goals})])
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"goal_id": c.get("goal_id"), "deadline": deadline.isoformat(),
                "paths": list(goals)}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        if not action.undo:
            raise ActionRefused("这条修改没有保存撤销数据，不能撤销")
        try:
            snap = self._repo.load(user_id)
            back: list[GoalSettings] = []
            for gid, g in action.undo["goals"].items():
                cur = self._settings(snap.state, gid)
                if _jsonable(cur.deadline) != g["deadline"]:
                    raise ActionRefused("这个目标的截止日期之后又改过了，不能撤销")
                before = g["before"]["deadline"]
                back.append(cur.model_copy(update={
                    "deadline": date.fromisoformat(before) if before else None}))
            commit_paths(self._repo, snap, {},
                         now=self._clock(), new_id=self._new_id, source="assistant",
                         wiki=tuple(self._events(back)), extra=[self._mark(action)])
        except (WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"restored": [g.goal_id for g in back]}


class GoalLimitsExecutor(_GoalBase):
    """`goal_limits` `{goal_id, limits}`: the goal record's `limits` (S1) replaced by his new
    list — one `GoalMapSaved("edited")` in the ledger's transaction; undo puts the old list back
    (refused when they were changed again since)."""

    def _save(self, user_id: str, goal_id: Any, before: tuple[str, ...] | None,
              after: tuple[str, ...], extra: Sequence[Callable[[Any], None]],
              undo: ActionRow | None = None) -> None:
        """`before` None: no check (execute). `undo`: the row whose undo data — the limits this
        commit replaces, read from the same snapshot it commits on — lands in the commit."""
        snap = self._repo.load(user_id)
        m = snap.state.maps.get(goal_id) if isinstance(goal_id, str) else None
        if m is None:
            raise ActionRefused("这个目标已经不在了")
        if before is not None and m.limits != before:
            raise ActionRefused("这个目标的条件之后又改过了，不能撤销")
        evs = [_wev(self._new_id, self._clock(), GoalMapSaved(
            record=m.model_copy(update={"limits": after}), action="edited"))]
        writes = list(extra)
        if undo is not None:
            writes.append(_write_undo(undo, {"goal_id": goal_id, "limits": list(m.limits),
                                             "after": list(after)}))
        self._repo.commit(snap, Commit(wiki=evs, extra=writes))

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        limits = c.get("limits")
        if not isinstance(limits, list) or not all(isinstance(x, str) for x in limits):
            raise ActionRefused("这个目标的条件不对")
        after = tuple(x.strip() for x in limits if x.strip())
        self._save(user_id, c.get("goal_id"), None, after, [effect], undo=action)
        return {"goal_id": c.get("goal_id"), "limits": list(after)}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        if not action.undo:
            raise ActionRefused("这条修改没有保存撤销数据，不能撤销")
        u = action.undo
        self._save(user_id, u["goal_id"], tuple(u["after"]), tuple(u["limits"]),
                   [self._mark(action)])
        return {"goal_id": u["goal_id"], "limits": u["limits"]}


class PathBlocksExecutor(_GoalBase):
    """`path_blocks` `{goal_id, ops}`: a confirmed goal's 学习路径 edited in the chat (`map_edits.edit_blocks`, the
    card's code) in the ledger's transaction; undo applies the inverse ops, refused when later edits are in the way."""

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        ops = [PathBlockOp.model_validate(op) for op in c.get("ops") or ()]
        try:
            got = edit_blocks(self._repo, user_id, c.get("goal_id"), ops, now=self._clock(), new_id=self._new_id,
                              extra=lambda e: [effect, _write_undo(action, {
                                  "goal_id": c.get("goal_id"),
                                  "ops": [op.model_dump(mode="json") for op in e.undo]})])
        except (WikiConflict, WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        if not got.done:
            raise ActionRefused("；".join(got.refused))
        return {"goal_id": c.get("goal_id"), "done": list(got.done),
                **({"note": "；".join(got.refused)} if got.refused else {})}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        if not action.undo:
            raise ActionRefused("这条修改没有保存撤销数据，不能撤销")
        u = action.undo
        try:
            edit_map(self._repo, user_id, u["goal_id"], TypeAdapter(list[MapEditOpIn]).validate_python(u["ops"]),
                     now=self._clock(), new_id=self._new_id, extra=[self._mark(action)])
        except (WikiConflict, WikiInvalid, LookupError) as exc:
            raise ActionRefused(str(exc)) from exc
        return {"goal_id": u["goal_id"]}


class GoalPauseExecutor(_GoalBase):
    """`goal_pause`: `paused` true / false = B5's park / resume of the goal (`store.bot_goals`,
    `bot_goal_state`; the same state GET /wiki/bot/goals and POST .../park|resume use): no 继续
    card and no weekly review for it. A big goal: each path of its map. The state change and the
    ledger's effect are ONE transaction; undo is the opposite in one transaction with the row
    marked undone."""

    def __init__(self, pool, repo: Repo, clock: Callable[[], datetime], new_id: Ids):
        super().__init__(repo, clock, new_id)
        self._pool = pool

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        paused = c.get("paused")
        if not isinstance(paused, bool):
            raise ActionRefused("要说明是暂停还是继续")
        now = self._clock()
        with self._pool.connection() as conn, conn.transaction():
            ids = self._goal_ids(self._repo.load(user_id).state, c.get("goal_id"))
            parked = bot_goals.parked(conn, user_id)
            todo = [g for g in ids if (g in parked) != paused]
            if not todo:
                raise ActionRefused("这个目标已经是暂停的" if paused else "这个目标没有暂停")
            for g in todo:
                (bot_goals.park if paused else bot_goals.resume)(conn, user_id, g, now)
            store.set_action_undo(conn, action.action_id, {"goals": todo, "paused": paused})
            effect(conn)
        return {"goal_id": c.get("goal_id"), "paused": paused, "paths": todo}

    def undo(self, user_id: str, action: ActionRow) -> dict:
        if not action.undo:
            raise ActionRefused("这条修改没有保存撤销数据，不能撤销")
        u, now = action.undo, self._clock()
        with self._pool.connection() as conn, conn.transaction():
            parked = bot_goals.parked(conn, user_id)
            if any((g in parked) != u["paused"] for g in u["goals"]):
                raise ActionRefused("这个目标之后又改过了，不能撤销")
            for g in u["goals"]:
                (bot_goals.resume if u["paused"] else bot_goals.park)(conn, user_id, g, now)
            self._mark(action)(conn)
        return {"restored": u["goals"]}


# a goal_new row with neither blocks nor a reading plan (the chat always proposes one of them)
NO_BLOCKS = "这条目标还没分好块，请在聊天里重新说一遍想学什么"


class GoalNewExecutor(_GoalBase):
    """`goal_new`: opens the goal (its map + the first diagnosis round, the user answers it in the
    chat); the goal's commit carries the ledger's effect. A row with `blocks` (chat-first blocks:
    the assistant's `new_goal` / `edit_goal_blocks` draft, maybe edited on the card through
    `revise`) is made from those blocks with no intake call (`create_goal_from_blocks`, with its
    materials when `source_ids`). A row with `reading` (读懂这篇, reading-mode spec §3.3–§3.4:
    its `blocks` are the prerequisites, maybe none) is made by
    `wikiapp.reading.create_reading_goal_with_source` from the plan the chat made — no S1 call at
    confirm. A row with neither is refused (NO_BLOCKS): the chat always proposes one of the two.
    With `source_ids` the goal is made from those materials through `wikiapp.goal_sources`: a
    material that cannot be used fails the row with its text, nothing written; the imports the
    uses need start after the commit on `runs()` (one that cannot start: the row is executed and
    its result's `note` says so). Undo archives the goal and closes its draft."""

    def __init__(self, repo: Repo, gateway: Callable[[str], CountingGateway | None],
                 clock: Callable[[], datetime], new_id: Ids, *, cancel_writes: CancelWrites,
                 pool: Any = None, runs: Callable[[], Any] | None = None):
        super().__init__(repo, clock, new_id)
        self._gateway, self._pool = gateway, pool
        self._cancel_writes = cancel_writes
        self._runs = runs or _no_source_runs     # read at use: tests set their runner later

    def revise(self, action: ActionRow, draft: Any) -> dict[str, Any]:
        """The learner's edit of a blocks draft on the card (chat-first blocks): the card's
        `pages` reordered, a block taken out (×), a block added with only a name (加一块) — the
        same `revise_pages` rules as the page drafts — and the `blocks` rebuilt from that list:
        a kept key is its block, level included (a changed `covers` line is split at 、 into its
        covers; `{key, title}` renames it, keeping its key, level, covers and said), a new title
        is a new block with no covers and no level. A draft row may carry
        a `level` (one of LEVELS or ""): it replaces that block's (a 读懂这篇 row's blocks are its
        prerequisites; its `reading` is kept as it is). A row without blocks has no list to edit:
        NotDecidable, as a kind without `revise`. Two blocks of one name
        (加一块 with a name the draft has) → ActionRefused 「已经有这一块了」, at the edit (the
        card's edits are stored as they happen: `ActionLedger.edit_draft`), not at confirm."""
        blocks = _blocks_of(action.content)
        if blocks is None:
            raise NotDecidable("这类动作不能带修改后的草稿批准")
        levels, draft = _draft_levels(draft)
        content = revise_pages(action.content, draft, allow_new=True, rename=True,
                               allow_empty="reading" in action.content)
        by_key = {b["key"]: b for b in blocks}
        out = []
        for p, level in zip(content["pages"], levels, strict=True):
            # a block added on the card: a stated beginner's goal never diagnoses it (D07)
            beginner = str(action.content.get("beginner") or "")
            b = dict(by_key.get(p["key"]) or {"key": p["key"], "name": p["title"], "covers": [],
                                              "summary": "", "target": False, "existing_id": "",
                                              "level": "", "diagnose": not beginner,
                                              "said": [beginner] if beginner else [],
                                              "said_level": "unlearned" if beginner else "",
                                              "atomic": False})
            if p["key"] in by_key and p["title"] != b["name"]:     # renamed on the card
                b["name"] = p["title"]
            line = p.get("covers") or ""
            if line != "、".join(b["covers"]):
                b["covers"] = [x.strip() for x in line.split("、") if x.strip()]
            if level is not None:
                b["level"] = level
            out.append(b)
        names = [norm_name(b["name"]) for b in out]
        if len(set(names)) != len(names):      # 加一块 with a name the draft already has
            raise ActionRefused("已经有这一块了")
        return {**content, "blocks": out,
                "pages": [{"key": b["key"], "title": b["name"], "covers": "、".join(b["covers"]),
                           "level": b["level"]} for b in out]}

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        c = action.content
        goal = c.get("goal") or ""
        source_ids = c.get("source_ids") or []
        if not isinstance(goal, str) or not isinstance(source_ids, list) or not all(
                isinstance(x, str) for x in source_ids):
            raise ActionRefused("这条目标的内容不对")
        if not goal.strip() and not source_ids:
            raise ActionRefused("要说明想学什么")
        # a draft left waiting (an older card, another conversation): confirming it must not make the goal twice
        title = c.get("title")
        if not source_ids and not c.get("again") and same_goal(
                self._repo.load(user_id).state, goal, title if isinstance(title, str) else "") is not None:
            raise ActionRefused("已经有这个目标了，这张卡片不用再确认")
        reading = c.get("reading")
        plan = None
        if reading is not None:              # 读懂这篇: the plan the chat made (§3.3)
            try:
                plan = ReadingPlan.model_validate(reading)
            except ValidationError as exc:
                raise ActionRefused("这条带读的内容不对") from exc
            if source_ids != [plan.source_id]:
                raise ActionRefused("这条带读的内容不对")
        blocks = _blocks_of(c)
        if "blocks" in c and not blocks and not (plan is not None and c["blocks"] == []):
            raise ActionRefused("这条目标的分块不对")
        if plan is None and not blocks:      # a row from before chat-first blocks: none is made
            raise ActionRefused(NO_BLOCKS)
        if source_ids and self._pool is None:
            raise ActionRefused("资料现在读不到，没法按它开目标")
        try:                                # an unusable material says so, before the model check
            load_materials(self._pool, user_id, source_ids)
        except GoalSourceError as exc:
            raise ActionRefused(exc.detail) from exc
        gw = self._gateway(user_id)
        if gw is None:
            raise ActionRefused("请切换模型重试")
        draft_ids: list[str] = []
        reading_ids: list[str] = []

        def spy(prefix: str) -> str:
            v = self._new_id(prefix)
            if prefix == "dr":
                draft_ids.append(v)
            elif prefix == "rdg":
                reading_ids.append(v)
            return v

        def write_undo(conn) -> None:       # runs inside the goal's commit, the ids exist by then
            # a reading goal's map id is known here (its text + its material): its undo archives
            # it even when the opening asked nothing and no draft was stored
            undo: dict[str, Any] = {"draft_id": draft_ids[0]}
            if plan is not None:
                undo["map_id"] = reading_map_id(goal.strip(), plan.source_id)
                undo["reading_id"] = reading_ids[0] if reading_ids else None
            store.set_action_undo(conn, action.action_id, undo)
        words = [str(x) for x in c.get("words") or () if isinstance(x, str)]
        try:
            if plan is not None:            # 读懂这篇: no S1 call, the prerequisites and units
                res = create_reading_goal_with_source(
                    self._repo, self._pool, gw, user_id, goal.strip(),
                    [block_of(b) for b in blocks or ()], plan, runs=self._runs(),
                    words=words, limits=[str(x) for x in c.get("limits") or ()
                                         if isinstance(x, str)],
                    beginner=str(c.get("beginner") or ""), title=str(c.get("title") or ""),
                    now=self._clock(), new_id=spy, extra=[effect, write_undo])
            else:                           # the blocks the learner confirmed: no further call
                res = create_goal_from_blocks_with_sources(
                    self._repo, self._pool, gw, user_id, goal.strip(),
                    [block_of(b) for b in blocks], source_ids, runs=self._runs(),
                    words=words, limits=[str(x) for x in c.get("limits") or ()
                                         if isinstance(x, str)],
                    beginner=str(c.get("beginner") or ""),
                    title=str(c.get("title") or ""),
                    use="reference" if c.get("use") == "reference" else "follow",
                    now=self._clock(), new_id=spy, extra=[effect, write_undo])
        except GoalSourceError as exc:
            raise ActionRefused(exc.detail) from exc
        except (WikiInvalid, WikiConflict) as exc:   # two blocks that are one node; the same
            raise ActionRefused(str(exc)) from exc       # goal already being learned (H4)
        except LLMUnavailable as exc:
            raise ActionRefused("请切换模型重试") from exc
        # only a draft that was stored (it is open): a goal that finished at once (no question)
        # has no draft to read back — the map / path / page id says where it went
        out: dict[str, Any] = {"draft_id": res.draft.draft_id if res.draft and res.open else None,
                               "open": res.open, "calls": res.calls}
        for field in ("path_id", "map_id", "page_id"):
            out[field] = getattr(res, field)
        if res.source_note:             # the goal is made; a material's full import did not start
            out["note"] = "目标建好了，但资料没能开始整理"
        return out

    def undo(self, user_id: str, action: ActionRow) -> dict:
        if not action.undo:
            raise ActionRefused("这条修改没有保存撤销数据，不能撤销")
        draft_id = action.undo["draft_id"]
        snap = self._repo.load(user_id)
        draft = snap.state.drafts.get(draft_id)
        if draft is None:
            # an opening that asked nothing stores no draft: the goal the action made is the
            # map in its undo data (a reading goal) or the map / path its result names
            made = [action.undo.get("map_id"),
                    *((action.result or {}).get(k) for k in ("map_id", "path_id"))]
            goal_ids = {x for x in made if isinstance(x, str) and x
                        and (x in snap.state.maps or x in snap.state.paths)}
            if not goal_ids:
                raise ActionRefused("这个目标是之前就有的，没有新建的东西可以撤销")
        else:
            goal_ids = {draft.map_id} if draft.map_id else set()
            for ev in self._repo.wiki_events_of(user_id, {"draft_closed"}):
                if isinstance(ev.body, DraftClosed) and ev.body.draft_id == draft_id:
                    goal_ids |= {x for x in (ev.body.map_id, ev.body.path_id) if x}
            goal_ids = goal_ids or {draft_id}
        for gid in tuple(goal_ids):
            if gid in snap.state.maps:
                goal_ids.update(map_paths(snap.state, gid))
        page_ids = sorted({pid for gid in goal_ids if gid in snap.state.paths
                           for pid in snap.state.paths[gid].page_ids if pid is not None})
        wiki: list[WikiEvent] = []
        if draft is not None and draft_id not in snap.state.closed_drafts:
            wiki.append(_wev(self._new_id, self._clock(),
                             DraftClosed(draft_id=draft_id, outcome="stopped")))
        for gid in sorted(goal_ids):
            cur = snap.state.goal_settings.get(gid) or GoalSettings(goal_id=gid)
            wiki.append(_wev(self._new_id, self._clock(), GoalSettingsSet(
                settings=cur.model_copy(update={"archived": True}))))
        action_id = action.action_id

        def mark(conn) -> None:
            store.set_action_status(conn, action_id, status="undone", at=self._clock(),
                                    from_statuses=("executed",))
        self._repo.commit(snap, Commit(wiki=wiki, extra=[mark]))
        if page_ids:
            self._cancel_writes(user_id, page_ids)
        return {"archived": sorted(goal_ids)}


# --- registration ---------------------------------------------------------------------------

BOT_PATH_OPS = ("move", "add")       # the Bot's weekly-review path edit (`bot.path_actions`, B5b)
ASSISTANT_PATH_OPS = ("reorder", "postpone")


class PathEditRouter:
    """Ledger kind `path_edit` has two executors: the Bot's weekly-review edit (`op` move / add,
    `bot.path_actions.PathEditExecutor`, scope `path`) and the assistant's reorder / postpone
    (`PathEditExecutor` here); both are scope `path` and store through
    `wikiapp.path_edits.save_paths`, so the `on_path_changed` hooks run for both. A row goes to the
    one its `op` belongs to; an op neither knows is refused (ActionRefused), never guessed. A Bot
    row takes no draft (NotDecidable, as without `revise`)."""

    def __init__(self, assistant: "PathEditExecutor", bot: ActionExecutor):
        self.assistant, self.bot = assistant, bot

    def _for(self, action: ActionRow):
        op = action.content.get("op")
        if op in BOT_PATH_OPS:
            return self.bot
        if op in ASSISTANT_PATH_OPS:
            return self.assistant
        raise ActionRefused(f"不认识的路径修改：{op}")

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        return self._for(action).execute(user_id, action, effect)

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return self._for(action).rejected(user_id, action)

    def undo(self, user_id: str, action: ActionRow) -> dict:
        return self._for(action).undo(user_id, action)

    def revise(self, action: ActionRow, draft: Any) -> dict[str, Any]:
        revise = getattr(self._for(action), "revise", None)
        if revise is None:
            raise NotDecidable("这类动作不能带修改后的草稿批准")
        return revise(action, draft)

    def decided(self, user_id: str, action: ActionRow) -> None:
        hook = getattr(self._for(action), "decided", None)
        if hook is not None:
            hook(user_id, action)


def _no_source_runs() -> Any:
    from evowiki.sources.runs import NoSourceRuns
    return NoSourceRuns()


class SourceUseExecutor:
    """`source_use`: the assistant's `mark_source_use` (materials phase 2, T15) - a material's use
    (exam / learn / understand) through `sources.uses.apply_use`, which also starts the full import
    of a chat attachment; the use and the ledger's effect are ONE transaction. A run that could not
    start leaves the use recorded and says so in the result's `note`. Undo deletes that use row
    (undo data `use_id`); the import it started stays (the material keeps its full import)."""

    def __init__(self, pool, runs: Callable[[], Any], clock: Callable[[], datetime]):
        self._pool, self._runs, self._clock = pool, runs, clock

    def execute(self, user_id: str, action: ActionRow, effect: Callable[[Any], None]) -> dict:
        from evowiki.sources import uses  # heavy import side: only this kind needs it
        c = action.content
        source_id, use = c.get("source_id"), c.get("use")
        if not isinstance(source_id, str) or use not in ("exam", "learn", "understand"):
            raise ActionRefused("要说明是哪份资料、记成什么用处")
        def write(conn, use_id: str) -> None:      # in the use's transaction
            store.set_action_undo(conn, action.action_id, {"use_id": use_id})
            effect(conn)
        try:
            added = uses.apply_use(self._pool, self._runs(), user_id, source_id, use,
                                   self._clock(), session_id=c.get("session_id"), effect=write)
        except LookupError as e:
            raise ActionRefused("没有这份资料，或者已经删掉了") from e
        except (uses.StillReading, uses.ReadFailed) as e:
            raise ActionRefused(e.detail) from e
        except uses.RunNotStarted:             # the use is recorded (and the row executed with it)
            # even the durable retries could not be scheduled: a plain failure, no button named
            return {"source_id": source_id, "use": use,
                    "note": "资料没能开始整理"}
        return {"source_id": source_id, "use": use, "use_id": added.use_id}

    def rejected(self, user_id: str, action: ActionRow) -> None:
        return None

    def undo(self, user_id: str, action: ActionRow) -> dict:
        from evowiki.sources import store as sources_store
        from evowiki.store.locks import user_writer
        if not action.undo:
            raise ActionRefused("这条修改没有保存撤销数据，不能撤销")
        use_id = action.undo["use_id"]
        with user_writer(self._pool, user_id) as tx:
            if not sources_store.delete_use(tx.conn, user_id, use_id):
                raise ActionRefused("这份资料的这个用处已经不在了")
            store.set_action_status(tx.conn, action.action_id, status="undone", at=self._clock(),
                                    from_statuses=("executed",))
        return {"use_id": use_id}


def register_assistant_executors(
        ledger: ActionLedger, repo: Repo, clock: Callable[[], datetime], new_id: Ids, *,
        request_write: RequestWrite, cancel_writes: CancelWrites,
        gateway: Callable[[str], CountingGateway | None],
        pool, practice: PracticeWriter | None = None,
        bot_path: ActionExecutor | None = None,
        source_runs: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Registers the executors of every kind in `KINDS` (the graph / card / rule / settings ones are
    `bot.executors_graph`) on `ledger`; returns them by kind.
    `pool`: the ledger's pool (goal pause shares its transaction with the effect).
    `bot_path`: the Bot's own `path_edit` executor (B5b); given, the ledger gets a
    `PathEditRouter` of both for that kind (the returned dict keeps the assistant's).
    `source_runs`: the source import runner `source_use` starts runs on, read at use (None: no
    runner - a use that needs an import is recorded and says the run did not start)."""
    base = (repo, clock, new_id)
    writes = {"request_write": request_write, "cancel_writes": cancel_writes}
    executors: dict[str, Any] = {
        "page_split": PageSplitExecutor(*base, **writes),
        "page_new": PageNewExecutor(*base, **writes),
        "page_merge": PageMergeExecutor(*base, **writes),
        "page_practice": PagePracticeExecutor(repo, practice, clock, new_id),
        "path_edit": PathEditExecutor(*base, **writes),
        "goal_deadline": GoalDeadlineExecutor(pool, *base),
        "goal_pause": GoalPauseExecutor(pool, *base),
        "goal_limits": GoalLimitsExecutor(*base),
        "path_blocks": PathBlocksExecutor(*base),
        "goal_new": GoalNewExecutor(repo, gateway, clock, new_id, pool=pool, runs=source_runs,
                                    cancel_writes=cancel_writes),
        "source_use": SourceUseExecutor(pool, source_runs or _no_source_runs, clock),
    }
    from evowiki.bot.executors_graph import build_graph_executors  # it imports this module
    executors.update(build_graph_executors(repo, clock, new_id, gateway=gateway, pool=pool))
    for kind, ex in executors.items():
        ledger.register(kind, PathEditRouter(ex, bot_path)
                        if kind == "path_edit" and bot_path is not None else ex)
    return executors
