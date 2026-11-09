"""The assistant's action tools (assistant-actions spec §1.2-§1.7, §2, §7, §11): every change goes
through the Bot action ledger, so the chat card and 参数's row are ONE record.

- Drafts. A structure / page / path / graph / rule tool proposes a DRAFT: `ledger.propose(...,
  level="ask")` with `content["confirm"] = True`. Nothing changes until the learner confirms (the
  plugin approves it, with an edited page list as `draft` when he changed it); cancel = reject.
- Suggestions. `make_cards` / `fix_card` / `drop_card` / `postpone_reviews` / `mark_source_use` are scope `suggestion`
  (level `auto`): the row is recorded and executed at once (still undoable).
- Settings. `bot_settings` runs at once (propose approved_now + approve; the `preference` level is
  fixed `auto_undo`) and is undoable. `set_exemplar` (a 样板页, scope `teaching_params`) runs the
  same way: the learner's own request in the chat is the click the settings button was.
- Freshness tokens. path_edit / page_split / page_new-with-path carry `path_seen`
  (`path_fingerprint`), graph actions `graph_seen` (`graph_fingerprint`); the executors refuse a
  row without them.
- Idempotency. The key is `assistant:<workflow>:<step>` (`agent.durable.step_key`) inside a durable
  turn, `assistant:<session>:<uuid>` outside one. A replayed step finds its row: a draft the learner
  already edited (a new content hash, `EffectConflict`) is returned as it is now, never an error.
- Goal blocks (chat-first blocks, spec 2026-10-23). `new_goal` for a goal to learn runs the blocks
  calls itself (`wikiapp.goal.plan_goal_blocks`, the learner's metered gateway) and proposes the
  blocks draft (`content["blocks"]` + the card's `pages`, `session_id`); `edit_goal_blocks`
  applies the learner's chat edits to the session's latest blocks draft as a NEW draft, the old
  one rejected with detail `replaced` in the same transaction (`Plan.replaces`). 读懂这篇
  (reading-mode spec §3.1, §3.3) runs S1's understand mode the same way (`_reading_plan`): the
  draft has the prerequisite `blocks` (edited by `edit_goal_blocks`) and the `reading` plan, whose
  units `edit_reading` merges / skips / restores the same replacing way (no model call; only
  before confirm — a confirmed reading's units change through /wiki/readings).
- Bad arguments (unknown page / cell / node / path) are `fail(...)`: nothing is written.
- The result's `action` card is what the loop turns into the `action` event; the model's text
  claiming a change without a tool result shows no card."""

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from itertools import count
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from evowiki.agent import durable
from evowiki.agent.effects import EffectConflict
from evowiki.agent.source_tools import SOURCE_CONTENT_NOTE
from evowiki.agent.tool_helpers import (
    TITLE_CHARS,
    clip,
    fail,
    run_guarded,
    spec,
)
from evowiki.agent.tools import ActionCard, ActionItem, Source, ToolContext, ToolResult
from evowiki.assistant.tools.grounding import ASKED, current_words, grounded_spec, quoted_now
from evowiki.assistant.tools.items import ItemArg, ItemRefused, checked_item
from evowiki.bot.actions import (
    SPLIT_FRESH,
    TURN_COST,
    ActionLedger,
    ActionRefused,
    ActionRow,
    NotDecidable,
    can_undo,
    made_of,
)
from evowiki.bot.bot_schemas import GOAL_RESULT, section_of
from evowiki.bot.executors_assistant import GOAL_NOT_STARTED, SCOPES, path_fingerprint
from evowiki.bot.executors_graph import graph_fingerprint
from evowiki.goals.itemflaws import near_duplicate
from evowiki.i18n import tr
from evowiki.nodes.model import Relation
from evowiki.profile.note import FACT_SECTIONS, scoped_line
from evowiki.sources.reader import SourceReader
from evowiki.store.usage import usage_tz
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.map_edits import PathBlockOp, fold_blocks
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.path_edits import map_paths
from evowiki.wikiapp.path_order import shown, stored_index
from evowiki.wikiapp.repo import Repo, Snapshot
from evowiki.wikiapp.review_cards import ANSWER_MAX, CUE_MAX, find_card, summary_cue
from evowiki.wikiapp.state import WikiState

log = logging.getLogger(__name__)

SOURCE = "assistant"            # ActionRow.source of every row the assistant's tools write
# draft tools his next 「好」 does not confirm (H4): the goal-blocks card's tail asks a question, and a
# 「可以」 answers it, it never confirms the blocks; the reading-goal card (edit_reading) is clicked too
NO_CHAT_CONFIRM = frozenset({"new_goal", "edit_goal_blocks", "edit_reading"})
TITLE_MAX = 120
REPLACED = "replaced"           # detail of a blocks draft a chat edit replaced (「已换成新的分法」)


class Refused(Exception):
    """A tool argument that does not fit the wiki: becomes `fail(message)`, nothing written."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImportRequest:
    """One material `add_source` imports: `url` or `text` (exactly one), recorded as a use of the
    learner's current page / session."""

    url: str | None
    text: str | None
    title: str | None
    use: str
    page_id: str | None
    session_id: str | None


@dataclass(frozen=True)
class Imported:
    """The imported material (a repeat of the same bytes is the existing one)."""

    source_id: str
    title: str
    status: str                     # reading / ready / partial / failed
    error: str | None = None


class ImportFailed(Exception):
    """The import was refused (Chinese reason); `fetch`: the URL could not be read."""

    def __init__(self, reason: str, *, fetch: bool = False):
        super().__init__(reason)
        self.fetch = fetch


@dataclass
class ActionDeps:
    """What the action tools need. `gateway`: user id -> the metered gateway (None: no model) for
    the goal's blocks. `import_source(user_id, request)`: the materials import (the `/wiki/sources` path:
    store + `ImportJob`), raising `ImportFailed`; None = `add_source` refuses. `revise(user_id,
    page_id, source_ids, key)`: `wikiapp.source_revise.start_revise` (row + run), raising as it
    does; None = `revise_page` refuses. `practice(user_id, page_id, kind, covers, key)`: composes
    and render-checks the practice of `add_practice` now (`bot.practice.DeepPracticeWriter.compose`:
    the writer's practice run, keyed by the tool step's key; a `PracticeDraft`), raising
    ActionRefused; None = `add_practice` refuses. `sources`: the user's materials: `new_goal` checks its ids
    and takes the titles from it, `mark_source_use` checks the source against it (None: `new_goal`
    leaves the check to the executor, `mark_source_use` refuses). (`remember_about_me` writes the
    profile through its ledger row: `bot.executors_profile`.)"""

    repo: Repo
    ledger: ActionLedger
    clock: Callable[[], datetime]
    new_id: Callable[[str], str]
    gateway: Callable[[str], CountingGateway | None] = lambda uid: None
    import_source: Callable[[str, ImportRequest], Imported] | None = None
    revise: Callable[[str, str, Sequence[str], str], tuple[ActionRow, str]] | None = None
    practice: Callable[[str, str, str, str, str], Any] | None = None
    illustrate: Callable[[str, str, str, str, str], Any] | None = None
    sources: SourceReader | None = None
    profile_lines: Callable[[str], Mapping[str, Sequence[str]]] | None = None   # 我's lines he owns, by section
    timezone: Callable[[str], str] = lambda uid: "Asia/Shanghai"   # user id -> Bot settings' zone
    memory_store: Any = None                                       # forget_memory's search; None: it refuses
    # user id -> the mean cost (USD) of his recent page writes (restyle_pages' estimate; None:
    # unknown, the card shows no estimate)
    # user id -> the rule judge (`teach.rule_judge`, verdict "same") remember_preference asks when
    # his words are not a near-duplicate of a rule or card he has (None: no semantic check)
    rule_judge: Callable[[str], Any] = lambda uid: None
    # possibly conflicting memories (memory/conflicts.py ConflictService): read / resolve in the
    # chat; None: those tools refuse
    conflicts: Any = None
    # (user id, source ids, parse, say): `sources.uses.await_layout` for `new_goal`; None: no wait,
    # the reader's checks refuse a material still being read
    await_materials: Callable[[str, Sequence[str], bool, Callable[[str], None] | None], None] | None = None
    feedback_notify: Callable[[str], None] | None = None


@dataclass
class Plan:
    """What a tool wants written. `items`: the card's list. `page_id` / `page_version`: the row's
    freshness token of its one page (executors check `ActionRow.page_version`)."""

    kind: str
    title: str
    content: dict[str, Any]
    why: str = ""
    items: tuple[ActionItem, ...] = ()
    page_id: str | None = None
    page_version: str | None = None
    replaces: str | None = None     # a pending row this draft takes the place of (rejected "replaced")
    note: str = ""                  # said to the model after the card line (what was not done;
    #                                 a blocks draft: the blocks and the lines to word the reply from)


def _clip_title(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1] + "…"


# ---- cards ----

def items_of(content: Mapping[str, Any]) -> tuple[ActionItem, ...]:
    """The card's list of a row: its `pages` (the draft's page list; for merge / reorder the
    `key` is the thing's id; a blocks draft's rows carry the block's `level`)."""
    out = []
    for p in content.get("pages") or ():
        if isinstance(p, Mapping) and isinstance(p.get("key"), str):
            level = p.get("level")
            out.append(ActionItem(key=p["key"], title=str(p.get("title") or p["key"]),
                                  page_id=p.get("page_id"), covers=p.get("covers") or None,
                                  level=level if isinstance(level, str) and level else None))
    return tuple(out)


def _made_items(row: ActionRow, items: tuple[ActionItem, ...]) -> tuple[ActionItem, ...]:
    """The list of an executed row: the pages it made, found by the draft item's `key` (an item that
    made no page - a node the path already showed - is not listed); a merge lists the one page it
    made, with its folder. Unchanged for a row that made nothing."""
    made = made_of(row)
    if not made:
        return items
    if row.kind == "page_merge":
        m = made[0]
        title = row.content.get("title")
        return (ActionItem(key=m["page_id"], title=title if isinstance(title, str) and title else row.summary,
                           page_id=m["page_id"], covers=f"在「{m['folder']}」文件夹里" if m["folder"] else None),)
    by_key = {m["key"]: m for m in made if m["key"] is not None}
    if not by_key:
        return items
    return tuple(i.model_copy(update={"page_id": by_key[i.key]["page_id"], "n": by_key[i.key]["n"]})
                 for i in items if i.key in by_key)


def card_of(row: ActionRow, items: Sequence[ActionItem] | None = None, *,
            language: str | None = None) -> ActionCard:
    from evowiki.bot.presentation import action_detail, action_summary
    res = row.result or {}
    return ActionCard(
        action_id=row.action_id, kind=row.kind, title=action_summary(row, language=language),
        goal_title=(str(row.content.get("title") or "").strip() or None)
        if row.kind == "goal_new" else None,
        # a split shows the points it made (a model may have chosen them after 确认)
        items=_made_items(row, items_of({"pages": res["parts"]}) if res.get("parts") else
                          tuple(items) if items is not None else items_of(row.content)),
        status=row.status,
        confirm=bool(row.content.get("confirm")), can_undo=can_undo(row),
        detail=(action_detail(row, language=language) or str(res.get("kept") or ""))[:300], replaces=str(row.content.get("replaces") or ""),
        result={k: res[k] for k in GOAL_RESULT if k in res},
        page_id=row.page_id, section_id=section_of(row))


def approve(ledger: ActionLedger, user_id: str, action_id: str) -> ActionRow:
    """`ledger.approve` as a card click runs it: off the tool's DBOS step (`durable.outside_dbos`).
    DBOS starts no workflow inside a step, so an executor that starts work on approve (a split's
    page writes, a source import) failed there and only noted `write_error`."""
    return durable.outside_dbos(lambda: ledger.approve(user_id, action_id))


def _step_key(ctx: ToolContext) -> str:
    try:
        return f"assistant:{durable.step_key()}"
    except RuntimeError:               # not in a durable step (inline turn, tests)
        return f"assistant:{ctx.session_id}:{uuid.uuid4().hex[:12]}"


# ---- lookups in a snapshot ----

def _path_of(state: WikiState, path_id: str | None, ref: str | None = None):
    """(path id, record): by id; else the one path whose shown rows contain `ref`; else the only
    path."""
    if path_id is not None:
        rec = state.paths.get(path_id)
        if rec is None:
            raise Refused(f"没有这条学习路径：{path_id}")
        return path_id, rec
    if ref is not None:
        hits = [(pid, rec) for pid, rec in state.paths.items() if _find_rows(rec, [ref])]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise Refused(f"「{ref}」在好几条路径上，请给出 path_id")
        raise Refused(f"路径上没有「{ref}」")
    if len(state.paths) == 1:
        pid = next(iter(state.paths))
        return pid, state.paths[pid]
    raise Refused("要说明是哪条学习路径（path_id）")


def _find_rows(rec, refs: Sequence[str]):
    """The shown rows named by `refs` (row key, page id or an exact title that is unique), in the
    order of `refs`; an unknown reference is `Refused` (callers) / [] when asked by `_path_of`."""
    rows = shown(rec)
    out = []
    for ref in refs:
        hit = next((r for r in rows if r.key == ref), None)
        if hit is None:
            hit = next((r for r in rows if rec.page_ids[stored_index(rec, r.key)] == ref), None)
        if hit is None:
            same = [r for r in rows if r.title == ref]
            hit = same[0] if len(same) == 1 else None
        if hit is None:
            return []
        out.append(hit)
    return out


def _rows(rec, refs: Sequence[str]):
    got = _find_rows(rec, refs)
    if not got:
        bad = next((r for r in refs if not _find_rows(rec, [r])), refs[0])
        raise Refused(f"路径上没有这一页：{bad}（用 read_path 看路径上的页）")
    if len({r.key for r in got}) != len(got):
        raise Refused("同一页写了两次")
    return got


def _row_page(rec, row) -> dict[str, Any]:
    """A row as the draft's page list stores it: key and title, and the wiki page once started (a
    reloaded card opens it; `revise_pages` keeps the entry of a key the learner keeps)."""
    page_id = rec.page_ids[stored_index(rec, row.key)]
    return {"key": row.key, "title": row.title, **({"page_id": page_id} if page_id else {})}


def _row_item(rec, row) -> ActionItem:
    return ActionItem(key=row.key, title=row.title,
                      page_id=rec.page_ids[stored_index(rec, row.key)])


def _node(snap: Snapshot, ref: str):
    """A graph node by id, else by exact name (unique)."""
    graph = snap.state.graph
    n = graph.node(ref)
    if n is None:
        same = [x for x in graph.nodes if x.name == ref]
        n = same[0] if len(same) == 1 else None
    if n is None:
        raise Refused(f"图谱里没有这个点：{ref}（用 read_graph 查）")
    return n


def _page(snap: Snapshot, page_id: str, *, written: bool = False):
    rec = snap.state.pages.get(page_id)
    if rec is None or page_id in snap.state.trashed_pages:
        raise Refused(f"没有这个页面：{page_id}")
    if written and not rec.has_body:       # none yet, or a run is writing it
        raise Refused(f"页面「{rec.title}」还没写好")
    return rec


def _goal_label(state: WikiState, goal_id: str) -> str:
    if goal_id in state.paths:
        return state.paths[goal_id].path.goal
    if goal_id in state.maps:
        return state.maps[goal_id].goal
    raise Refused(f"没有这个目标：{goal_id}（用 read_goals 查）")


# ---- the tool ----

class _ActionTool:
    """One action tool: `build(args, snap, ctx) -> Plan` validates against the snapshot (raises
    `Refused`), the base proposes / records the row and returns the card. `mode`: "draft" (ask +
    confirm), "auto" (suggestion: recorded + executed), "settings" (executed at once)."""

    def __init__(self, deps: ActionDeps, name: str, description: str, args: type[BaseModel],
                 build: Callable[[Any, Snapshot, ToolContext, ActionDeps], Plan], *,
                 mode: Literal["draft", "auto", "settings"] = "draft"):
        self._deps, self._args, self._build, self._mode = deps, args, build, mode
        self.spec = spec(name, description, args, "propose", "action", acts=True,
                         tier="draft" if mode == "draft" else "direct")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(self._args, args, lambda a: self._run(a, ctx))

    @property
    def args_model(self) -> type[BaseModel]:
        return self._args

    def dry_run(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        """Why `args` would be refused now (None: they would build), writing nothing: the schema
        and the build against the current snapshot (assistant.pending's `offer` / `confirm`)."""
        try:
            a = self._args.model_validate(args)
        except ValidationError as e:
            return f"参数不合法：{e.errors(include_url=False)[0]['msg']}"
        try:
            self._build(a, self._deps.repo.load(ctx.user_id), ctx, self._deps)
        except Refused as e:
            return str(e)
        return None

    def _run(self, a: BaseModel, ctx: ToolContext) -> ToolResult:
        d = self._deps
        key = _step_key(ctx)
        found = d.ledger.get_by_key(key)
        if found is not None and found.user_id == ctx.user_id:
            # a replay of a step that already wrote its row (the learner may have edited, confirmed
            # or even undone it since): that row as it is now, never a second one or an error
            return self._result(self._settled(ctx, found), None)
        try:
            plan = self._build(a, d.repo.load(ctx.user_id), ctx, d)
            spent = None if ctx.spent_usd is None else ctx.spent_usd()
            result = None if spent is None else {TURN_COST: spent}
            if self._mode == "auto":
                row = self._record(ctx, plan, key, result)
            else:
                row = self._propose(ctx, plan, key, result)
                if self._mode == "settings":
                    row = self._execute_settings(ctx, row)
        except Refused as e:
            result = fail(str(e))
            return result.model_copy(update={"data": {"error_code": e.code}}) if e.code else result
        return self._result(row, plan)

    def _settled(self, ctx: ToolContext, row: ActionRow) -> ActionRow:
        """A found row driven to its end the way a first run would (auto / settings kinds)."""
        if self._mode == "settings":
            return self._execute_settings(ctx, row)
        if self._mode == "auto" and row.status == "approved":
            try:
                return approve(self._deps.ledger, ctx.user_id, row.action_id)
            except NotDecidable as e:
                return replace(row, status="failed", detail=str(e))
        return row

    @staticmethod
    def _result(row: ActionRow, plan: Plan | None) -> ToolResult:
        if row.status == "failed":
            return fail(row.detail or "没改成")
        # a fresh draft shows the plan's items; a row the learner edited (its content differs from
        # the plan's, `confirm` aside) shows his page list
        edited = plan is None or {k: v for k, v in row.content.items()
                                  if k != "confirm"} != plan.content
        items = plan.items if plan is not None and (not edited or not row.content.get("pages")) \
            else None
        card = card_of(row, items)
        # a replayed step (no plan) rebuilds a blocks draft's note from the stored row, so the
        # model gets the same brief (and the turn the same lead) as the first run did
        told = plan.note if plan is not None else _replay_note(row)
        note = "\n" + told if told else ""
        return ToolResult(content=_ActionTool._say(row, card) + note, action=card,
                          lead=_lead(row))

    def _propose(self, ctx: ToolContext, plan: Plan, key: str,
                 result: dict | None = None) -> ActionRow:
        d = self._deps
        scope = SCOPES[plan.kind][0]
        content = dict(plan.content)
        kw: dict[str, Any] = {}
        if self._mode == "draft":
            content["confirm"] = True
            kw["level"] = "ask"
        else:             # settings: the `preference` level is fixed auto_undo (BotSettings)
            kw.update(level="auto_undo", approved_now=True)
        kw.update(kind=plan.kind, scope=scope, source=SOURCE, summary=plan.title, why=plan.why,
                  content=content, key=key, page_id=plan.page_id, page_version=plan.page_version,
                  result=result)
        try:
            if plan.kind == "page_revise":
                from evowiki.store import bot as bot_store
                row, created = d.ledger.propose_unless(
                    ctx.user_id,
                    lambda conn: bot_store.open_page_revise(conn, ctx.user_id, plan.page_id),
                    **kw)
                if not created:
                    raise Refused("这一页已经有一份草稿，请先确认或取消")
                return row
            if plan.replaces is not None:     # one transaction: the new draft in, the old one out
                return d.ledger.propose_replacing(ctx.user_id, plan.replaces, detail=REPLACED,
                                                  **kw)
            return d.ledger.propose(ctx.user_id, **kw)
        except (NotDecidable, LookupError) as e:
            raise Refused(str(e)) from e
        except EffectConflict:
            # a replay after the learner edited the draft (new content hash): the row as it is now
            row = d.ledger.get_by_key(key)
            if row is None or row.user_id != ctx.user_id:
                raise
            return row

    def _execute_settings(self, ctx: ToolContext, row: ActionRow) -> ActionRow:
        if row.status == "approved":
            try:
                return approve(self._deps.ledger, ctx.user_id, row.action_id)
            except NotDecidable as e:
                return replace(row, status="failed", detail=str(e))
        return row

    def _record(self, ctx: ToolContext, plan: Plan, key: str,
                result: dict | None = None) -> ActionRow:
        """A suggestion-scope row: recorded approved and executed at once. A replay finds the row
        (never re-records: that would reset its outcome) and only approves it again (idempotent)."""
        d = self._deps
        row = d.ledger.get_by_key(key)
        if row is None:
            d.ledger.record(ctx.user_id, kind=plan.kind, scope=SCOPES[plan.kind][0], source=SOURCE,
                            summary=plan.title, why=plan.why, content=plan.content, key=key,
                            status="approved", page_id=plan.page_id, result=result)
            row = d.ledger.get_by_key(key)
        assert row is not None and row.user_id == ctx.user_id
        try:
            return approve(d.ledger, ctx.user_id, row.action_id)
        except NotDecidable as e:
            return replace(row, status="failed", detail=str(e))

    @staticmethod
    def _say(row: ActionRow, card: ActionCard) -> str:
        if row.status == "proposed" and row.kind == "page_practice":
            what = _PRACTICE.get(str(row.content.get("kind")), "练习")
            return (f"已写好{what}草稿（{row.action_id}）：{card.title}。它已经显示在这一页里，标成"
                    "草稿，用户可以直接在页面里试；点「确认替换」才加进页面，点「取消」就不加，"
                    "确认前笔记不会变。不要说已经加好了。")
        if row.status == "proposed":
            what = "已生成草稿" if card.confirm else "已提出，等用户批准"
            told = SPLIT_FRESH + "。" if row.kind == "page_split" else ""
            return (f"{what}（{row.action_id}）：{card.title}。等用户在卡片上确认，确认前什么都没改。"
                    f"不要说已经改好了。{told}")
        if row.status == "executed":
            note = (row.result or {}).get("note")
            if note:                     # done, with a part that did not happen (its reason)
                return f"已执行（{row.action_id}）：{card.title}。但{note}，要照实告诉用户。"
            if not card.can_undo:
                return f"已执行（{row.action_id}）：{card.title}。"
            return f"已执行（{row.action_id}）：{card.title}。用户可以在卡片上撤销。"
        return f"{card.title}：状态 {row.status}。"


# ============================== argument models ==============================

class _A(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Part(_A):
    title: str = Field(min_length=1, max_length=120, description="页的名字：短，说清这一页讲什么，不带冒号和说明")
    covers: str = Field(default="", max_length=400,
                        description="这一页回答的那个问题（路径里标题下面的一行小字）：用学习者自己的话，短，一句，不写方法和计划，不要「先用……再给……」「从……推到……再算……」；例：特征向量 →「哪些方向转完还朝原来那边」")
    node_id: str | None = Field(default=None, description="这一页讲的图谱点（id），没有就省略")


class SplitPageArgs(_A):
    page: str = Field(description="要拆的路径行：行 key、页面 id 或唯一的标题（read_path 里有）")
    parts: list[Part] = Field(min_length=2, description="拆成的几页，按学习顺序")
    path_id: str | None = Field(default=None, description="省略=自动从行里找")
    why: str = Field(default="", max_length=300)


def _split_page(a: SplitPageArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    pid, rec = _path_of(snap.state, a.path_id, a.page)
    row = _rows(rec, [a.page])[0]
    page_id = rec.page_ids[stored_index(rec, row.key)]
    version = None
    if page_id is not None and page_id in snap.state.pages:
        version = page_fingerprint(snap.state.pages[page_id])
    pages = []
    for i, p in enumerate(a.parts):
        if p.node_id is not None:
            _node(snap, p.node_id)
        pages.append({"key": f"p{i + 1}", "title": p.title.strip(), "covers": p.covers.strip(),
                      **({"node_id": p.node_id} if p.node_id else {})})
    return Plan(
        kind="page_split", title=_clip_title(f"把「{row.title}」拆成 {len(pages)} 页"),
        why=a.why,
        content={"path_id": pid, "page_key": row.key, "pages": pages,
                 "path_seen": path_fingerprint(rec)},
        items=tuple(ActionItem(key=p["key"], title=p["title"], covers=p["covers"] or None)
                    for p in pages),
        page_id=page_id if version else None, page_version=version)


class MergePagesArgs(_A):
    pages: list[str] = Field(min_length=2, description="要合并的页面 id")
    title: str = Field(min_length=1, max_length=120, description="合并后那一页的标题")
    why: str = Field(default="", max_length=300)


def _merge_pages(a: MergePagesArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    ids = list(dict.fromkeys(a.pages))
    if len(ids) < 2:
        raise Refused("合并至少要两页")
    recs = [_page(snap, p, written=True) for p in ids]
    return Plan(
        kind="page_merge",
        title=_clip_title(f"把 {'、'.join(r.title for r in recs)} 合成一页「{a.title.strip()}」"),
        why=a.why,
        content={"title": a.title.strip(),
                 "pages": [{"key": r.page_id, "title": r.title, "page_id": r.page_id}
                           for r in recs],
                 "versions": {r.page_id: page_fingerprint(r) for r in recs}},
        items=tuple(ActionItem(key=r.page_id, title=r.title, page_id=r.page_id) for r in recs))


class AddPracticeArgs(_A):
    page: str | None = Field(default=None, description="页面 id 或标题，省略=当前页")
    kind: Literal["question", "card", "interactive"] = Field(
        description="question=练习题，card=记忆卡，interactive=交互")
    covers: str = Field(default="", max_length=400, description="练习要覆盖页面里的什么，可省略")


_PRACTICE = {"question": "练习题", "card": "记忆卡", "interactive": "交互"}


def _add_practice(a: AddPracticeArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    """The practice is composed and render-checked now (the writer's practice run), so the draft
    shows in the page, playable, before the learner confirms; a run without a usable interactive
    writes no row."""
    # the model has only the current page's title in front of it: live runs passed titles and
    # invented ids, and once put the draft on another page it had read about (2026-11-04)
    ref = a.page or ctx.page_id
    if ref is None:
        raise Refused("没有当前页，要说明加到哪一页")
    titled = [pid for pid, r in snap.state.pages.items() if r.title == ref]
    rec = _page(snap, titled[0] if len(titled) == 1 else ref, written=True)
    if d.practice is None:
        raise Refused("现在不能加练习")
    try:
        draft = d.practice(ctx.user_id, rec.page_id, a.kind, a.covers.strip(), _step_key(ctx))
    except ActionRefused as e:
        raise Refused(str(e) or "这一页刚改过，再试一次") from e
    return Plan(kind="page_practice", title=_clip_title(f"给「{rec.title}」加{_PRACTICE[a.kind]}"),
                why=draft.why,
                content={"page_id": rec.page_id, "kind": a.kind, "covers": a.covers.strip(),
                         "draft": draft.model_dump(mode="json")},
                page_id=rec.page_id, page_version=draft.base_version)


class IllustrateSectionArgs(_A):
    page: str | None = Field(default=None, description="页面 id 或标题，省略=当前页")
    section_id: str = Field(min_length=1, description="read_page 返回的这一节 id，不猜 id")
    request: str = Field(min_length=1, max_length=2000, description="这一节要配什么图、图要讲清什么")


def _illustrate_section(a: IllustrateSectionArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    from evowiki.bot.section_visual import validate_target
    ref = a.page or ctx.page_id
    if ref is None:
        raise Refused("没有当前页，请说明给哪一页配图")
    matches = [pid for pid, rec in snap.state.pages.items() if rec.title == ref]
    pid = matches[0] if len(matches) == 1 else ref
    try:
        rec, sec = validate_target(snap, pid, a.section_id)
        if d.ledger.open_page_revise(ctx.user_id, pid) is not None:
            raise Refused("这一页已经有一份草稿，请先确认或取消")
        if d.illustrate is None:
            raise Refused("配图的页面写作还没有接上")
        draft = d.illustrate(ctx.user_id, pid, a.section_id, a.request.strip(), _step_key(ctx))
    except ActionRefused as exc:
        raise Refused(str(exc)) from exc
    return Plan(kind="page_revise", title=_clip_title(f"给「{sec.heading}」配图"),
                why=a.request.strip(), page_id=pid, page_version=draft.base_version,
                content={"page_id": rec.page_id, "state": "ready", "source_ids": [],
                         "illustration": {"section_id": a.section_id, "request": a.request.strip()},
                         "draft": draft.model_dump(mode="json")})


class WritePageArgs(_A):
    title: str | None = Field(default=None, max_length=120, description="页面标题：短，说清这一页讲什么；省略=用 node 的名字")
    node: str | None = Field(default=None, description="这页讲的图谱点（id 或名字），可省略")
    covers: str = Field(default="", max_length=400, description="这一页回答的那个问题（路径里标题下面的一行小字）：用学习者自己的话，短，一句，不写方法和计划，不要「先用……再给……」「从……推到……再算……」；例：特征向量 →「哪些方向转完还朝原来那边」")
    path_id: str | None = Field(default=None, description="给了就把这页放进这条路径")
    after: str | None = Field(default=None, description="放在路径的这一行之后（行 key / 标题），省略=末尾")


def _write_page(a: WritePageArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    if not a.title and not a.node:
        raise Refused("要给标题或者图谱里的点")
    if a.after is not None and a.path_id is None:
        raise Refused("给了 after 就要说明是哪条路径（path_id）")
    node = _node(snap, a.node) if a.node else None
    title = (a.title or node.name).strip()                       # type: ignore[union-attr]
    page: dict[str, Any] = {"key": "n1", "title": title, "covers": a.covers.strip()}
    if node is not None:
        page["node_id"] = node.id
    content: dict[str, Any] = {"mode": "page", "pages": [page]}
    if a.path_id is not None:
        pid, rec = _path_of(snap.state, a.path_id)
        if node is not None and any(node.id in it.node_ids for it in shown(rec)):
            raise Refused(f"「{node.name}」在这条路径上已经有一页了；要写新内容就只给 title，不给 node")
        content.update(path_id=pid, path_seen=path_fingerprint(rec))
        if a.after is not None:
            at = shown(rec).index(_rows(rec, [a.after])[0])
            content["index"] = at + 1
    return Plan(kind="page_new", title=_clip_title(f"写一页「{title}」"), content=content,
                items=(ActionItem(key="n1", title=title, covers=a.covers.strip() or None),))


class CheatSheetArgs(_A):
    title: str | None = Field(default=None, max_length=120, description="速查页标题")
    pages: list[str] = Field(default_factory=list, description="要总结的页面 id")
    path_id: str | None = Field(default=None, description="总结这条路径上已写好的页")


def _cheat_sheet(a: CheatSheetArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    ids = list(dict.fromkeys(a.pages))
    if a.path_id is not None:
        _, rec = _path_of(snap.state, a.path_id)
        ids += [p for p in (rec.page_ids[stored_index(rec, r.key)] for r in shown(rec))
                if p and p not in ids]
    recs = [_page(snap, p) for p in ids]
    recs = [r for r in recs if r.has_body]
    if not recs:
        raise Refused("没有可以总结的已写好的页面")
    title = (a.title or f"速查：{recs[0].title}" + ("等" if len(recs) > 1 else "")).strip()
    concepts = list(dict.fromkeys(c for r in recs for c in r.concepts))
    covers = "涵盖：" + "、".join(r.title for r in recs)
    return Plan(kind="page_new", title=_clip_title(f"做一页速查「{title}」"),
                content={"mode": "summary", "pages": [
                    {"key": "s1", "title": title, "covers": covers, "concepts": concepts}]},
                items=(ActionItem(key="s1", title=title, covers=covers),))


class ReorderArgs(_A):
    order: list[str] = Field(min_length=1, description="要放到最前面的页，按新顺序；其余页保持原顺序。行 key / 页面 id / 唯一标题")
    why: str = Field(min_length=1, max_length=300, description="哪里不合理（哪一页用到了排在后面的东西）")
    path_id: str | None = None


def _reorder(a: ReorderArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    pid, rec = _path_of(snap.state, a.path_id, a.order[0])
    first = _rows(rec, a.order)
    # the card lists the WHOLE new order (the draft is edited as a list); the executor reads it as
    # "these rows first, in this order", the rest following in their order
    rows = [*first, *(r for r in shown(rec) if r.key not in {f.key for f in first})]
    return Plan(kind="path_edit", title=_clip_title(f"调整「{rec.path.goal}」的学习顺序"),
                why=a.why,
                content={"path_id": pid, "op": "reorder", "path_seen": path_fingerprint(rec),
                         "pages": [_row_page(rec, r) for r in rows]},
                items=tuple(_row_item(rec, r) for r in rows))


class PostponeArgs(_A):
    pages: list[str] = Field(min_length=1, description="要往后放的页（行 key / 页面 id / 唯一标题）")
    after: str | None = Field(default=None, description="放在这一行之后，省略=放到最后")
    why: str = Field(default="", max_length=300)
    path_id: str | None = None


def _postpone(a: PostponeArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    pid, rec = _path_of(snap.state, a.path_id, a.pages[0])
    rows = _rows(rec, a.pages)
    content: dict[str, Any] = {"path_id": pid, "op": "postpone",
                               "path_seen": path_fingerprint(rec),
                               "pages": [_row_page(rec, r) for r in rows]}
    if a.after is not None:
        content["after"] = _rows(rec, [a.after])[0].key
    return Plan(kind="path_edit", title=_clip_title(
        f"把 {'、'.join(r.title for r in rows)} 往后放"), why=a.why, content=content,
        items=tuple(_row_item(rec, r) for r in rows))


class DeadlineArgs(_A):
    goal_id: str = Field(description="目标：路径 id 或大目标 id（read_goals 里有）")
    deadline: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")


def _goal_has_path(state: WikiState, goal_id: str) -> None:
    """A goal setting needs a path to sit on: refused at the draft, not after he confirms it."""
    if goal_id in state.maps and not map_paths(state, goal_id):
        raise Refused(GOAL_NOT_STARTED)


def _deadline(a: DeadlineArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    label = _goal_label(snap.state, a.goal_id)
    _goal_has_path(snap.state, a.goal_id)
    try:
        day = date.fromisoformat(a.deadline)
    except ValueError:
        raise Refused("日期不对，要写成 年-月-日") from None
    if day < d.clock().astimezone(ZoneInfo(d.timezone(ctx.user_id))).date():
        raise Refused("这个日期已经过了")
    return Plan(kind="goal_deadline", title=_clip_title(f"给「{label}」设截止日期 {a.deadline}"),
                content={"goal_id": a.goal_id, "deadline": a.deadline})


class NewGoalArgs(_A):
    goal: str = Field(default="", description="想学什么，写用户的原话；附了资料时可以空")
    words: list[str] = Field(default_factory=list, max_length=8, description=(
        "他为这个目标说过的其他原话，逐句一字不改地照抄（条件、会什么、不会什么）；他已经放下的"
        "旧请求不抄；没有就省略"))
    source_ids: list[str] = Field(default_factory=list, max_length=5,
                                  description="这次会话里用户上传的资料 id（要按它学或读懂它时）")
    use: Literal["reference", "follow", "understand"] | None = Field(default=None, description=(
        "附了资料时，他说了怎么用它才写：reference=只当参考（按目标本身分块，资料只用来对照），"
        "follow=按这份资料的顺序学，understand=带他读懂这一篇；他没说就省略"))
    notes: str = Field(default="", max_length=1500, description=(
        "你刚用 web_search 查到的、关于这个目标所在领域的要点，几句话（例如一个俗称一般指什么、"
        "要注意的安全问题、要看地区或版本的地方）；没查就省略"))
    again: bool = Field(default=False, description="已有这个目标还没开始学、他明确说要重新分块时才写 true")

    @model_validator(mode="after")
    def _goal_or_source(self) -> "NewGoalArgs":
        if not self.goal.strip() and not self.source_ids:
            raise ValueError("要说明想学什么，或者附上资料")
        return self


def _new_goal(a: NewGoalArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    """The goal row: the user's words and the attached materials. A goal to learn (no material,
    or materials to learn by) is first split into a few big blocks (chat-first blocks, plan B2):
    `plan_goal_blocks` runs here, on the learner's metered gateway, and the row is the blocks
    draft the learner talks over (`edit_goal_blocks`) and confirms; nothing is written to the
    wiki before that. How the materials are used (`goal_sources.material_use`, spec 2026-10-28
    §2.5: his stated intent — `use`, the assistant's reading of his words — else the defaults; a
    default is said in the reply, `use_default`) is the row's `use`; one paper and no words: a
    Refused that has the model ask him first (ASK_USE). 读懂这篇 (one material; several:
    MSG_UNDERSTAND_ONE) is S1's understand mode here too (`_reading_plan`): the draft has the
    prerequisites and the reading units, and confirming makes no S1 call (reading-mode spec §3.1,
    §3.3). `words`: his own words for
    this goal, each one quoted from what he wrote in this conversation (`quoted_in`; one that is
    not: nothing written) — S1 reads them and checks its quotes against them. With materials the
    reader checks them first (unknown / still reading / unusable: the route's text, nothing
    written); without a reader nothing is proposed (MATERIALS_UNREADABLE). A material still being
    read is waited for, and a text-first PDF is parsed first (`_await_materials`), because the
    blocks and units attach to its sections: the turn's status line shows that wait."""
    from evowiki.wikiapp.goal import same_goal
    from evowiki.wikiapp.goal_sources import (
        MSG_UNDERSTAND_ONE,
        GoalSourceError,
        material_use,
        read_materials,
    )
    from evowiki.wikiapp.guidelines import quoted_in
    ids = list(dict.fromkeys(s.strip() for s in a.source_ids if s.strip()))
    goal = a.goal.strip()
    words = list(dict.fromkeys(w.strip() for w in a.words if w.strip()))
    if any(not quoted_in(w, ctx.user_words) for w in words):
        raise Refused(WORDS_NOT_QUOTED)
    content: dict[str, Any] = {"goal": goal, "words": words}
    if ids:
        content["source_ids"] = ids
    if a.again:
        content["again"] = True
    elif not ids and (twin := same_goal(snap.state, goal)) is not None:
        # 学点新的's 开始 shows this line as it is (no model call), so it is written to him
        raise Refused(f"你已经有「{twin.title or twin.goal}」这个目标了，可以在「今天」接着学。要重新分块就说一声。")
    materials = []
    if ids and d.sources is not None:
        reader, uid = d.sources, ctx.user_id

        def read(*, before_layout: bool = False) -> list[Any]:
            try:
                return read_materials(lambda sid: reader.get(uid, sid),
                                      lambda sid: reader.sections(uid, sid), ids,
                                      before_layout=before_layout)
            except GoalSourceError as e:
                raise Refused(e.detail) from e
        _await_materials(d, uid, ids, False, ctx)
        materials = read(before_layout=True)
    decided = material_use(goal, a.use, materials)
    if decided is not None and decided.use is None:     # one paper, no words: ask first (§2.5)
        raise Refused(ASK_USE)
    if any(m.row.text_first for m in materials):
        _await_materials(d, ctx.user_id, ids, True, ctx)
        materials = read()
        decided = material_use(goal, a.use, materials)
    if decided is not None and decided.use == "understand":
        if len(materials) > 1:
            raise Refused(MSG_UNDERSTAND_ONE)
        content["use"] = "understand"
        return _reading_plan(content, materials, snap, ctx, d, a.notes.strip())
    if decided is not None:
        content["use"] = decided.use
        if decided.default:
            content["use_default"] = True
    if ids and decided is None:     # no reader: the blocks cannot be planned from the materials
        raise Refused(MATERIALS_UNREADABLE)
    return _blocks_plan(content, materials, snap, ctx, d, a.notes.strip())


def _await_materials(d: ActionDeps, uid: str, ids: Sequence[str], parse: bool,
                     ctx: ToolContext) -> None:
    """`ActionDeps.await_materials`: too long a wait is the route's own refusal."""
    from evowiki.sources.uses import MaterialsNotReady
    from evowiki.wikiapp.goal_sources import MSG_SOURCE_READING
    if d.await_materials is None:
        return
    try:
        d.await_materials(uid, ids, parse, ctx.progress)
    except MaterialsNotReady as e:
        raise Refused(MSG_SOURCE_READING) from e


MODEL_UNAVAILABLE = "请切换模型重试"
# model-facing: a turn that repeats the same words only spends again (live 2026-11-04: 8 分 50 秒)
READING_FAILED = ("没排好怎么读：段落已经自动修正过，还是对不上原文。同样的话再调 new_goal 也一样，不要再调；"
                  "告诉他这次没排好，请切换模型重试")
# one paper and no words for it (spec 2026-10-28 §2.5): the model asks in chat, then calls again
ASK_USE = ("这份资料像一篇论文，他没说要怎么用：先在聊天里问他一句——是带他读懂这一篇，还是围绕它的主题"
           "系统地学；他回答后再调用 new_goal，带上 use（understand 或 follow / reference）")
# a default use, told in the blocks reply so the learner can change it (§2.5)
USE_DEFAULT_LINE = {
    "reference": "这本书我先当参考，你想按它的顺序学可以说",
    "follow": "我先按这本书的顺序排了，你只想把它当参考可以说"}
# the blocks (and 读懂这篇's units) are planned from the materials' sections in this turn: with
# no reader nothing is proposed (spec 2026-10-28 §2.8: no fallback to the old route)
MATERIALS_UNREADABLE = "没能读到，重试"
WORDS_NOT_QUOTED = "words 里每一句都要一字不改地抄他在这次对话里自己写的话"


def _blocks_progress(ctx: ToolContext, titles: Sequence[str], understand: bool = False):
    """plan_blocks' steps as the turn's status line (`ToolContext.progress`; None: not shown);
    `understand`: the 读懂这篇 line (reading_step_line)."""
    if ctx.progress is None:
        return None
    from evowiki.assistant.status_line import blocks_step_line, reading_step_line
    say = ctx.progress

    def tell(step: str) -> None:
        try:
            say(reading_step_line(step, titles) if understand else blocks_step_line(step, titles))
        except Exception:             # a status line never fails the tool
            log.warning("blocks progress line failed", exc_info=True)
    return tell


def _plan_blocks(snap: Snapshot, ctx: ToolContext, d: ActionDeps, goal: str,
                 words: Sequence[str], sources: Sequence[Any], notes: str = "",
                 mode: str = "follow", understand: bool = False):
    """`wikiapp.goal.plan_goal_blocks` on the user's metered gateway (`ActionDeps.gateway`: the
    calls are the turn's, metered to the learner); what they cost is charged to
    the turn (`ToolContext.charge`: the row's turn cost and the turn's cost include it, failed
    calls too); every model failure is a `Refused` with the learner-language reason (no row).
    `understand`: S1's understand mode over the one material (its units' ids from
    `ActionDeps.new_id`), the reply lines on as for a learning goal (reading-mode spec §3.2)."""
    from evowiki.goals.intake import IntakeError
    from evowiki.wikiapp.goal import plan_goal_blocks
    from evowiki.wikiapp.llm import LLM_BUDGET, LLM_FAILURES, LLM_UNAVAILABLE, LLMUnavailable
    try:
        gw = d.gateway(ctx.user_id)
    except LLMUnavailable:
        gw = None
    if gw is None:
        raise Refused(MODEL_UNAVAILABLE)
    try:
        # A crash during S1 re-runs it when the durable turn replays this step (the row is
        # written only after it returned): the tool's step makes exactly this one call.
        return plan_goal_blocks(snap, gw, ctx.user_id, goal, words, now=d.clock(),
                                sources=sources, notes=notes,
                                use="understand" if understand else "learn",
                                mode="reference" if mode == "reference" else "follow",
                                new_id=d.new_id if understand else None,
                                progress=_blocks_progress(ctx, [getattr(s, "title", "")
                                                                for s in sources], understand))
    except (LLMUnavailable, *LLM_UNAVAILABLE) as e:
        raise Refused(MODEL_UNAVAILABLE) from e
    except LLM_BUDGET as e:
        raise Refused(e.line("没法拆这个目标")) from e
    except (IntakeError, *LLM_FAILURES) as e:
        log.warning("blocks for %s failed: %s", ctx.user_id, e)
        raise Refused(tr(READING_FAILED if understand else
                         "The model could not organize this goal. Switch models and try again.",
                         lang=snap.state.language), code="goal_plan_failed") from e
    finally:            # the tool gateway's calls are the turn's spend (the card's cost too)
        if ctx.charge is not None:
            ctx.charge(getattr(gw, "cost", None))


# The learner-facing lines of a blocks draft (intake@v27): framing / caveat as the blocks call
# wrote them; mainline / start / question only on the first draft (a chat edit clears them: they
# named the blocks before the edit).
LINES = ("framing", "caveat", "mainline", "start", "question")


def _blocks_content(base: Mapping[str, Any], title: str, blocks: Sequence[Any],
                    lines: Mapping[str, Any]) -> dict[str, Any]:
    """A blocks draft's content (the plugin reads it as it is): the goal fields of `base`, the
    topic `title`, the learner-facing `lines` (framing, caveat, mainline, question: str; start:
    [str]; "" / [] = none), the blocks keyed b1…bN in order (with their `level`, "" = none), and
    `pages` = the card's editable list (one row per block: its name, its covers joined by 、, its
    level). A block row also carries the goal record's part (`diagnose`, `said`, `said_level`;
    never shown on the card)."""
    rows = [{"key": f"b{i}", "name": b.name, "covers": list(b.covers), "summary": b.summary, "definition": b.definition,
             "target": b.target, "existing_id": b.existing_id, "level": b.level,
             "diagnose": b.diagnose, "said": list(b.said), "said_level": b.said_level,
             "atomic": b.atomic, "sections": list(b.sections), "exercises": list(b.exercises)}
            for i, b in enumerate(blocks, 1)]
    told = {k: (list(lines.get(k) or ()) if k == "start" else str(lines.get(k) or ""))
            for k in LINES}
    return {**base, "title": title, **told, "blocks": rows,
            "pages": [{"key": r["key"], "title": r["name"], "covers": "、".join(r["covers"]),
                       "level": r["level"]} for r in rows]}


def _blocks_title(title: str, n: int) -> str:
    return _clip_title(f"「{title}」分成 {n} 块")


def _blocks_items(content: Mapping[str, Any]) -> tuple[ActionItem, ...]:
    return tuple(ActionItem(key=p["key"], title=p["title"], covers=p["covers"] or None,
                            level=p.get("level") or None)
                 for p in content["pages"])


def _block_lines(blocks: Sequence[Any]) -> str:
    """The blocks as the model reads them in a tool result: 「1. 名字〔基础〕：一句话（包含：…）」."""
    return "\n".join(
        f"{n}. {b.name}" + (f"〔{b.level}〕" if b.level else "")
        + (f"：{b.summary}" if b.summary else "")
        + (f"（包含：{'、'.join(b.covers)}）" if b.covers else "")
        for n, b in enumerate(blocks, 1))


BLOCKS_LISTED = "分块（卡片上已经列出来了，回复里不要再列一遍）："
READING_LISTED = "怎么带他读（卡片上已经列出来了，回复里不要再列一遍）："


def _content_blocks(content: Mapping[str, Any]) -> list[Any]:
    """A blocks row's `blocks` as goals.blocks.Block (a level outside LEVELS is "")."""
    from evowiki.goals.blocks import block_of
    return [block_of(b) for b in content.get("blocks") or ()]


def _is_first_blocks(row: ActionRow) -> bool:
    return (row.kind == "goal_new" and isinstance(row.content.get("blocks"), list)
            and not row.content.get("replaces"))


def _lead_of(content: Mapping[str, Any]) -> str:
    """A first blocks draft's words before its card: the blocks call's framing, then its caveat
    (a paragraph each; written in one call with the blocks and the main line, so they agree)."""
    return "\n\n".join(x for x in (str(content.get(k) or "").strip()
                                   for k in ("framing", "caveat")) if x)


def _lead(row: ActionRow) -> str:
    """What the turn says before a row's card: a first blocks draft's `_lead_of`, else "". From
    the stored row, so a replayed step leads with the same words."""
    return _lead_of(row.content) if _is_first_blocks(row) else ""


def _first_brief(content: Mapping[str, Any]) -> str:
    """What the model words the first reply's tail from (tool result, model-facing only), built
    from the stored row (a replayed step rebuilds the same text): the blocks, the lead the turn
    already said before the card (`_lead_of`), and the lines to word after it (loop: GOAL_RULES)."""
    pages = content.get("pages") or ()
    first = pages[0] if pages and isinstance(pages[0], Mapping) else {}
    lead = _lead_of(content)
    lines = [*_listed(content),
             f"卡片前已经替你对他说了：「{lead}」" if lead else "卡片前没有说话。",
             "卡片后面你接着写，照这几句的意思用你自己的话说：",
             f"- 主线：{content.get('mainline') or '（没写）'}"]
    if first.get("title"):      # the block the card starts with: the same order as the card and the 主线
        lines.append(f"- 从哪里开始：第一块「{first['title']}」" + (f"（{first['covers']}）" if first.get("covers") else ""))
    question = content.get("question") or ""
    lines.append(f"- 问题：{question}" if question else "- 问题：没有要问的")
    lines.append("主线一句、从哪里开始一句、最多一个问题（上面没有就不问）；卡片前说过的不再说；不说卡片在上面还是下面。主线的先后要和从哪里开始一致，都照卡片的顺序。")
    if content.get("use_default") and content.get("use") in USE_DEFAULT_LINE:
        lines.append("他没说资料怎么用，再加一句（意思照这句，用你自己的话）："
                     f"「{USE_DEFAULT_LINE[content['use']]}」；他要换用法时，带上 use 重新调用 new_goal")
    lines += _limits_lines(content)
    return "\n".join(lines)


def _limits_lines(content: Mapping[str, Any]) -> list[str]:
    """His conditions for this goal as the model reads them (model-facing only; never on the
    card): the goal record's `limits`, so the reply can say how they shape the blocks."""
    limits = [str(x) for x in content.get("limits") or () if str(x).strip()]
    return ["他这次的条件（分块时已经考虑了）：" + "；".join(limits)] if limits else []


def _edit_brief(content: Mapping[str, Any]) -> str:
    """What a chat edit's result tells the model (model-facing only), from the stored row: what
    each op changed (`changes`), the ops that could not be done (`refused`), the new blocks."""
    refused = list(content.get("refused") or ())
    return "\n".join([
        "改了什么：", *(f"- {x}" for x in content.get("changes") or ()),
        *(["其中这些没做成，要照实告诉用户：" + "；".join(refused)] if refused else []),
        *_listed(content, "现在的"), *_limits_lines(content)])


def _listed(content: Mapping[str, Any], now: str = "") -> list[str]:
    """The draft as the model reads it in a tool result: the blocks — for 读懂这篇 the
    prerequisites, then the reading units numbered as on the card (`_unit_lines`)."""
    if "reading" not in content:
        return [now + BLOCKS_LISTED, _block_lines(_content_blocks(content))]
    pre = _block_lines(_content_blocks(content)) or "（没有要先补的）"
    return [now + READING_LISTED, "先补：", pre, "按原文读：",
            _unit_lines(_content_reading(content))]


def _replay_note(row: ActionRow) -> str:
    """The note of a blocks draft rebuilt from its row ("" for any other row)."""
    if row.kind != "goal_new" or not isinstance(row.content.get("blocks"), list):
        return ""
    return _first_brief(row.content) if _is_first_blocks(row) else _edit_brief(row.content)


def _blocks_why(content: Mapping[str, Any], n: int) -> str:
    """The why of a blocks row (the card's ? and 参数's row): where the blocks come from, one plain
    sentence built from the row's own data — the learner's material or words."""
    titles = [str(t) for t in content.get("source_titles") or () if str(t).strip()]
    if titles:
        more = f"等 {len(titles)} 份资料" if len(titles) > 1 else ""
        return f"按你发的《{titles[0]}》{more}分成了 {n} 块"
    said = str(content.get("said") or content.get("goal") or "").strip()
    said = said if len(said) <= 40 else said[:39] + "…"
    return f"按你说的「{said}」分成了 {n} 块" if said else f"分成了 {n} 块"


def _blocks_plan(content: dict[str, Any], materials: Sequence[Any], snap: Snapshot,
                 ctx: ToolContext, d: ActionDeps, notes: str = "") -> Plan:
    said = content["goal"]
    res = _plan_blocks(snap, ctx, d, content["goal"], content["words"],
                       [m.toc for m in materials], notes, str(content.get("use") or "follow"))
    if res.dropped:   # what S1 wrote that could not be used: the log only
        log.info("blocks dropped for %s: %s", ctx.user_id, list(res.dropped))
    goal = res.goal
    # the goal record (S1): his words as S1 read them, his conditions for this goal
    base = {**content, "goal": goal, "words": list(res.words), "limits": list(res.limits),
            "session_id": ctx.session_id, "said": said}
    if materials:
        base["source_titles"] = [m.row.title for m in materials]
    title = res.title or goal
    out = _blocks_content(base, title, res.blocks, {
        "framing": res.framing, "caveat": res.caveat, "mainline": res.mainline,
        "start": list(res.start), "question": res.question})
    if res.stated_beginner:   # D07: kept for the blocks a chat edit adds (never diagnosed)
        out["beginner"] = res.beginner_quote
    return Plan(kind="goal_new", title=_blocks_title(title, len(res.blocks)), content=out,
                why=_blocks_why(out, len(res.blocks)), items=_blocks_items(out),
                note=_first_brief(out))


# ---- 读懂这篇: the reading draft (reading-mode spec §3.1, §3.3) ----

def _content_reading(content: Mapping[str, Any]) -> Any:
    """A reading draft's `reading` as goals.reading.ReadingPlan (Refused when it is malformed:
    never a guessed plan)."""
    from evowiki.goals.reading import ReadingPlan
    try:
        return ReadingPlan.model_validate(content.get("reading"))
    except ValidationError as e:
        raise Refused("这份带读的草稿坏了，请重新排一次") from e


def _unit_lines(plan: Any) -> str:
    """The units as the model reads them: 「3. 标题：一句话」, a skipped one marked 「（不读）」. The
    number is the unit's place (`order`), the one edit_reading's `units` take."""
    return "\n".join(f"{u.order}. {u.title}" + ("（不读）" if u.skipped else "")
                     + (f"：{u.about}" if u.about else "")
                     for u in sorted(plan.units, key=lambda u: u.order))


def _reading_title(content: Mapping[str, Any]) -> str:
    """The card's line (§3.3): 「先补这几个：A、B；然后按原文分 M 段读：1 问题和动机 · 2 方法」
    (no prerequisites: 「不用先补什么」; a skipped unit is not counted or listed). Reasons stay in
    the row's `why` (the ? hover)."""
    plan = _content_reading(content)
    names = [b.name for b in _content_blocks(content)]
    pre = f"先补这几个：{'、'.join(names)}" if names else "不用先补什么"
    units = [u for u in sorted(plan.units, key=lambda u: u.order) if not u.skipped]
    reads = " · ".join(f"{u.order} {u.title}" for u in units)
    return _clip_title(f"{pre}；然后按原文分 {len(units)} 段读：{reads}")


def _reading_why(content: Mapping[str, Any]) -> str:
    """The reading draft's ? (the card's hover and 参数's row): where the plan comes from."""
    plan = _content_reading(content)
    left = "，参考文献这类不用读的节没排进去" if plan.left_out else ""
    return (f"按《{plan.title}》的内容排的：先补读它要用到的东西，再按原文顺序一段一段读，"
            f"每段按内容切，不按页{left}")


def _reading_plan(content: dict[str, Any], materials: Sequence[Any], snap: Snapshot,
                  ctx: ToolContext, d: ActionDeps, notes: str = "") -> Plan:
    """读懂这篇's draft (§3.1–§3.3): S1's understand mode in this turn (one call on the learner's
    metered gateway, the status line as for blocks), its prerequisites as the draft's `blocks`
    (the same rows and card list as a blocks draft, so `edit_goal_blocks` and the card edit them)
    and its units as `reading` (a ReadingPlan dump, unit ids already fixed); `limits` and the reply
    lines as a blocks draft has them. The same reading already begun (its map id: the goal text
    S1 will work on + the material, `reading_map_id`) is refused BEFORE S1 runs (GOAL_IN_PROGRESS):
    a re-sent 读懂 costs no call."""
    from evowiki.belief.aspects import AspectKView
    from evowiki.wikiapp.goal import GOAL_IN_PROGRESS, _map_progress, _source_goal
    from evowiki.wikiapp.reading import reading_map_id
    said = content["goal"]
    tocs = [m.toc for m in materials]
    text, _ = _source_goal(said, tocs, "understand")
    old = snap.state.maps.get(reading_map_id(text, tocs[0].source_id)) if tocs else None
    if old is not None and _map_progress(snap.state, old, AspectKView(snap.k, d.clock())):
        raise Refused(GOAL_IN_PROGRESS)
    res = _plan_blocks(snap, ctx, d, said, content["words"], [m.toc for m in materials], notes,
                       understand=True)
    if res.reading is None:        # plan_blocks raises first; never a reading draft without units
        raise Refused(READING_FAILED)
    if res.dropped:   # what S1 wrote that could not be used: the log only
        log.info("reading plan dropped for %s: %s", ctx.user_id, list(res.dropped))
    base = {**content, "goal": res.goal, "words": list(res.words), "limits": list(res.limits),
            "session_id": ctx.session_id, "said": said,
            "source_titles": [m.row.title for m in materials]}
    out = _blocks_content(base, res.title or res.goal, res.blocks, {
        "framing": res.framing, "caveat": res.caveat, "mainline": res.mainline,
        "start": list(res.start), "question": res.question})
    out["reading"] = res.reading.model_dump(mode="json")
    if res.stated_beginner:
        out["beginner"] = res.beginner_quote
    return Plan(kind="goal_new", title=_reading_title(out), content=out, why=_reading_why(out),
                items=_blocks_items(out), note=_first_brief(out))


class ReadingEditArg(_A):
    op: Literal["merge", "skip", "unskip"] = Field(description=(
        "merge=相邻的几段合成一段；skip=这一段不读；unskip=恢复一段不读的"))
    units: list[int] = Field(min_length=1, max_length=8, description=(
        "段号，照最新草稿里的编号：merge 给相邻的几段（合成后在第一段的位置），skip / unskip 给一段"))
    title: str = Field(default="", max_length=120, description="merge：合并后的标题，可省略")


class EditReadingArgs(_A):
    ops: list[ReadingEditArg] = Field(min_length=1, description="按顺序执行的改动")


NO_READING_DRAFT = "现在没有待确认的带读草稿"
READING_CONFIRMED = "这份带读已经确认了，分段不能再改"
_READING_OP = {"merge": "合并第 {} 段", "skip": "第 {} 段不读", "unskip": "恢复第 {} 段"}


def _reading_op(plan: Any, e: ReadingEditArg, ids: Mapping[int, str]) -> tuple[Any, str]:
    """One edit on the draft's plan (goals/reading's stability ops: ReadingRefused; Refused for a
    number the draft does not have) and what it changed. `ids`: number → unit id as the draft was
    before this call's edits, so every op's numbers are the ones the learner saw."""
    from evowiki.goals.reading import merge_units, skip_unit, unskip_unit
    if any(n not in ids for n in e.units):
        raise Refused("草稿里没有这一段")
    if e.op == "merge":
        if len(e.units) < 2:
            raise Refused("合并要至少两段")
        first = ids[e.units[0]]
        for n in e.units[1:]:
            plan = merge_units(plan, first, ids[n], title=e.title)
        merged = next(u for u in plan.units if u.unit_id == first)
        return plan, f"第 {'、'.join(map(str, e.units))} 段合成了一段「{merged.title}」"
    if len(e.units) != 1:
        raise Refused("一次只能给一段")
    if e.op == "skip":
        return skip_unit(plan, ids[e.units[0]]), f"第 {e.units[0]} 段设成了不读"
    return unskip_unit(plan, ids[e.units[0]]), f"第 {e.units[0]} 段恢复成要读"


def _edit_reading(a: EditReadingArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    """The session's latest 读懂这篇 draft with the learner's unit edits (§3.3, Q8: merge
    adjacent units, skip one, restore one; no model call — 「第 2 段太长」 is new_goal again with
    his words): a NEW `goal_new` row, the old one rejected "replaced" in the same transaction, as
    `edit_goal_blocks`. A merge keeps the first unit's id. Only what cannot be done is refused (no
    draft, a confirmed one, every op inexecutable); the note says what changed and what not."""
    from evowiki.goals.reading import ReadingRefused, refused_text
    old = d.ledger.latest(ctx.user_id, "goal_new", match={"session_id": ctx.session_id},
                          has="reading")
    if old is None or old.status in ("rejected", "undone", "failed"):
        raise Refused(NO_READING_DRAFT)
    if old.status != "proposed":
        raise Refused(READING_CONFIRMED)
    plan = _content_reading(old.content)
    ids = {u.order: u.unit_id for u in plan.units}
    done: list[str] = []
    failed: list[str] = []
    for e in a.ops:
        what = _READING_OP[e.op].format("、".join(map(str, e.units)))
        try:
            plan, changed = _reading_op(plan, e, ids)
        except ReadingRefused as x:
            failed.append(f"{what}（{refused_text(x)}）")
            continue
        except Refused as x:
            failed.append(f"{what}（{x}）")
            continue
        done.append(changed)
    if len(failed) == len(a.ops):
        raise Refused("这些改动都做不了：" + "；".join(failed))
    content = {k: v for k, v in old.content.items()
               if k not in ("confirm", "replaces", "changes", "refused")}
    content["reading"] = plan.model_dump(mode="json")
    content.update(replaces=old.action_id, changes=done, refused=failed)
    return Plan(kind="goal_new", title=_reading_title(content), content=content,
                why=_reading_why(content), items=_blocks_items(content),
                replaces=old.action_id, note=_edit_brief(content))


# ---- chat edits of a blocks draft ----

class BlockEditArg(_A):
    op: Literal["merge", "move", "drop", "rename", "add", "place", "level"] = Field(description=(
        "merge=几块合成一块；move=把一个小主题从一块挪到另一块；drop=这一块不学了；"
        "rename=改名；add=加一块；place=把一块挪到第几个；level=改一块的分级"))
    names: list[str] = Field(default_factory=list, description=(
        "merge：要合并的几块的名字（第一块留下，合并后的块在它的位置）"))
    name: str = Field(default="", max_length=120, description=(
        "drop / rename / place / level：那一块的名字；add：新块的名字；merge：合并后的名字（可省略，"
        "不写就用第一块的名字）"))
    new_name: str = Field(default="", max_length=120, description="rename：新名字")
    cover: str = Field(default="", max_length=120, description="move：要挪的小主题（照分块里写的）")
    from_block: str = Field(default="", max_length=120, description="move：小主题现在在哪一块")
    to_block: str = Field(default="", max_length=120, description="move：挪到哪一块")
    covers: list[str] = Field(default_factory=list, description="add：新块包含的小主题，短名词，可省略")
    summary: str = Field(default="", max_length=300, description="add：一句话说新块讲什么，可省略")
    position: int | None = Field(default=None, ge=1, description=(
        "place：挪到第几块（从 1 数）；add：放在第几块（省略=最后）"))
    level: Literal["", "基础", "核心", "进阶", "拓展"] = Field(default="", description=(
        "level：新的分级；add：新块的分级，可省略。基础=后面几块都要用到，核心=这个目标的主体，"
        "进阶=核心学完以后，拓展=可学可不学"))


class EditGoalBlocksArgs(_A):
    ops: list[BlockEditArg] = Field(default_factory=list, description=(
        "按顺序执行的改动；块用名字指，照最新分块草稿里的名字写"))
    limits: list[str] | None = Field(default=None, max_length=8, description=(
        "他改了这个目标的条件（时间、形式、只用什么）时：改完以后的全部条件，每条一个短句；"
        "条件没变就省略，他说不再有条件就给 []"))

    @model_validator(mode="after")
    def _something(self) -> "EditGoalBlocksArgs":
        if not self.ops and self.limits is None:
            raise ValueError("要给出改动（ops）或新的条件（limits）")
        return self


_BLOCK_DROPPED = {"unknown_block": "没有叫这个名字的块", "self_merge": "合并要两块不同的",
                  "self_move": "挪出和挪入是同一块", "unknown_cover": "那一块里没有这个小主题",
                  "name_taken": "已经有一块叫这个名字", "unusable_node": "名字是空的",
                  "bad_index": "位置不对", "bad_level": "分级只能是基础、核心、进阶、拓展",
                  "empty_result": "至少要留一块"}
NO_BLOCKS_DRAFT = "现在没有待确认的分法。已经确认的目标，它的块用 edit_path_blocks 改"
BLOCKS_CONFIRMED = "这个分法已经确认了，这几块现在在学习路径上：用 edit_path_blocks 改"


def _as_beginner(b: Any, beginner: str) -> Any:
    """A block of a stated beginner's goal (D07): never diagnosed, unlearned on his words."""
    if not beginner:
        return b
    return b.model_copy(update={"diagnose": False, **(
        {} if b.said else {"said": (beginner,), "said_level": "unlearned"})})


def _block_op(e: BlockEditArg):
    from evowiki.goals.blocks import BlockOp
    to = e.new_name if e.op == "rename" else e.to_block
    return BlockOp(op=e.op, names=tuple(e.names), into=e.names[0] if e.names else "",
                   name=e.name.strip(), cover=e.cover, src=e.from_block, to=to,
                   covers=tuple(e.covers), summary=e.summary, level=e.level,
                   to_index=None if e.position is None else e.position - 1)


def _op_text(e: BlockEditArg) -> str:
    return {"merge": f"合并{'、'.join(e.names)}", "move": f"把「{e.cover}」挪到「{e.to_block}」",
            "drop": f"去掉「{e.name}」", "rename": f"把「{e.name}」改名",
            "add": f"加「{e.name}」", "place": f"挪动「{e.name}」",
            "level": f"改「{e.name}」的分级"}[e.op]


def _by_name(blocks: Sequence[Any], name: str):
    from evowiki.plain_text import norm_name
    key = norm_name(name)
    return next((b for b in blocks if norm_name(b.name) == key), None)


def _changed(e: BlockEditArg, before: Sequence[Any], after: Sequence[Any]) -> str:
    """What one executed op changed, for the model to word the reply from (where a merged or
    dropped block's contents are now; model-facing only)."""
    if e.op == "merge":
        names = list(dict.fromkeys([*e.names]))
        old = [b for b in (_by_name(before, n) for n in names) if b is not None]
        new = _by_name(after, e.name.strip() or (old[0].name if old else ""))
        into = new.name if new is not None else (e.name or names[0])
        return (f"「{'」「'.join(b.name for b in old)}」合并成了「{into}」，原来的"
                f"{'、'.join(c for b in old for c in b.covers) or '内容'}现在都在「{into}」里")
    if e.op == "move":
        return f"「{e.cover}」从「{e.from_block}」挪到了「{e.to_block}」"
    if e.op == "drop":
        b = _by_name(before, e.name)
        had = "、".join(b.covers) if b is not None and b.covers else "没写"
        return f"去掉了「{e.name}」（它原来包含：{had}）"
    if e.op == "rename":
        return f"「{e.name}」改名为「{e.new_name.strip()}」"
    if e.op == "add":
        b = _by_name(after, e.name)
        at = next((i for i, x in enumerate(after, 1) if x is b), len(after))
        return f"加了「{e.name.strip()}」" + (f"〔{e.level}〕" if e.level else "") + f"，在第 {at} 块"
    if e.op == "place":
        return f"「{e.name}」挪到了第 {e.position} 块"
    return f"「{e.name}」的分级改成了「{e.level}」"


def _edit_goal_blocks(a: EditGoalBlocksArgs, snap: Snapshot, ctx: ToolContext,
                      d: ActionDeps) -> Plan:
    """The session's latest blocks draft with the learner's edits (`goals.blocks.apply_block_ops`,
    the same code as the review's): a NEW `goal_new` row with the edited blocks (same goal,
    goal record — words, limits, beginner —, materials, title; levels and framing / caveat kept;
    `limits` replaces his conditions), the old row rejected
    "replaced" in the same transaction. Only what cannot be executed is refused: no draft, a
    confirmed one, every op inexecutable; the result (`note`) says what each op changed (where a
    merged or dropped block's contents are now), the ops that could not be done next to ones that
    could, and the new blocks — the model words the reply."""
    from evowiki.goals.blocks import apply_block_ops
    old = d.ledger.latest(ctx.user_id, "goal_new", match={"session_id": ctx.session_id},
                          has="blocks")
    if old is None or old.status in ("rejected", "undone", "failed"):
        raise Refused(NO_BLOCKS_DRAFT)
    if old.status != "proposed":
        raise Refused(BLOCKS_CONFIRMED)
    beginner = str(old.content.get("beginner") or "")
    out, failed, done = tuple(_content_blocks(old.content)), [], []
    reading = "reading" in old.content     # 读懂这篇: no prerequisite left is a plan too (§3.3)
    for e in a.ops:              # one at a time, so each refusal is said next to its own op
        before = out
        out, dropped = apply_block_ops(out, [_block_op(e)], allow_empty=reading)
        failed += [f"{_op_text(e)}（{_BLOCK_DROPPED.get(x.reason, x.reason)}）" for x in dropped]
        if not dropped:
            done.append(_changed(e, before, out))
    if a.ops and len(failed) == len(a.ops) and a.limits is None:
        raise Refused("这些改动都做不了：" + "；".join(failed))
    # a stated beginner (D07): a block his edit adds is not diagnosed either, unlearned on his words
    out = tuple(_as_beginner(b, beginner) for b in out)
    # the goal record is carried over whole: his words, his conditions, the beginner quote
    base = {k: v for k, v in old.content.items()
            if k in ("goal", "words", "limits", "beginner", "source_ids", "session_id", "said",
                     "source_titles", "use", "use_default", "reading")}
    if a.limits is not None:
        base["limits"] = list(dict.fromkeys(x.strip() for x in a.limits if x.strip()))
        told = "；".join(base["limits"]) or "没有了"
        done.append(f"这个目标的条件改成了：{told}")
    title = str(old.content.get("title") or base.get("goal") or "")
    content = _blocks_content(base, title, out, {k: old.content.get(k)
                                                 for k in ("framing", "caveat")})
    # stored on the row: the replaced row (the client marks only that card replaced) and what
    # the edit changed (a replayed step rebuilds the same note from them)
    content.update(replaces=old.action_id, changes=done, refused=failed)
    return Plan(kind="goal_new",
                title=_reading_title(content) if reading else _blocks_title(title, len(out)),
                content=content,
                why=_reading_why(content) if reading else _blocks_why(content, len(out)),
                items=_blocks_items(content), replaces=old.action_id,
                note=_edit_brief(content))


class PauseGoalArgs(_A):
    goal_id: str
    paused: bool = Field(default=True, description="true=暂停，false=继续")


def _pause_goal(a: PauseGoalArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    label = _goal_label(snap.state, a.goal_id)
    _goal_has_path(snap.state, a.goal_id)
    return Plan(kind="goal_pause",
                title=_clip_title(f"{'暂停' if a.paused else '继续'}目标「{label}」"),
                content={"goal_id": a.goal_id, "paused": a.paused})


# what set_goal_limits did, in the words the answer should use: it stores text, nothing in the schedule reads it
LIMITS_ONLY_WORDS = ("只存了这段文字，以后拆块时参考。它不是上限：每天学几页、写几页、什么时候提醒都没有变。"
                     "回答照这个说，不要说设好了或已生效；他要控制节奏，能改的是每天的建议条数、暂停目标、关掉空闲写页。")


class GoalLimitsArgs(_A):
    goal_id: str = Field(description="大目标 id（read_goals 里有）")
    limits: list[str] = Field(max_length=8, description=(
        "改完以后这个目标的全部条件（时间、形式、只用什么），每条一个短句；他说不再有条件就给 []"))


def _goal_limits(a: GoalLimitsArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    """His conditions for a goal already made (the goal record's `limits`, S1): the whole new
    list; the next block he enters is split with them."""
    m = snap.state.maps.get(a.goal_id)
    if m is None:
        raise Refused(f"没有这个大目标：{a.goal_id}（用 read_goals 查）")
    limits = list(dict.fromkeys(x.strip() for x in a.limits if x.strip()))
    if tuple(limits) == m.limits:
        raise Refused("这个目标的条件已经是这样了")
    told = "；".join(limits) or "没有条件"
    return Plan(kind="goal_limits", title=_clip_title(f"给「{m.title or m.goal}」记下条件（只在拆块时参考）：{told}"),
                content={"goal_id": a.goal_id, "limits": limits},
                note=LIMITS_ONLY_WORDS)


class EditPathBlocksArgs(_A):
    goal_id: str = Field(max_length=200, description="大目标 id（read_goals 里有）")
    ops: list[PathBlockOp] = Field(min_length=1, max_length=20, description="按顺序执行的改动")


def _edit_path_blocks(a: EditPathBlocksArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    """His edits of a confirmed goal's 学习路径 from the chat (`wikiapp.map_edits.edit_blocks`, the card's code):
    what can be done is done at once as one ledger row, undone as one; what cannot is said next to it."""
    made = count()
    try:
        got = fold_blocks(snap.state, a.goal_id, a.ops, lambda prefix: f"{prefix}-{next(made)}")
    except LookupError as e:
        raise Refused(str(e)) from e
    if not got.done:
        raise Refused("；".join(got.refused))
    order = "、".join(n.name for n in got.after.children(None) if n.kind == "region")
    tail = f"没改成的：{'；'.join(got.refused)}。如实告诉他。" if got.refused else ""
    m = snap.state.maps[a.goal_id]
    return Plan(kind="path_blocks", title=_clip_title(f"改「{m.title or m.goal}」的学习路径：{'；'.join(got.done)}"),
                content={"goal_id": a.goal_id, "ops": [op.model_dump(mode="json") for op in a.ops]},
                note=f"学习路径现在是：{order}。{tail}")


# ---- graph ----

class NodePart(_A):
    name: str = Field(min_length=1, max_length=120, description="这个部分的名字，照他说的写")
    definition: str = Field(max_length=200, description="一句话说它是什么")


class SplitNodeArgs(_A):
    node: str = Field(description="要拆细的点（id 或名字）")
    parts: list[NodePart] = Field(default_factory=list, description=(
        "他说了拆成哪几部分时写，只写他说的，不增不减；确认后图谱里就是这些。他没说拆成什么就不写"))
    why: str = Field(default="", max_length=300, description="为什么要拆")
    words: str = Field(default="", max_length=600, description=(
        "他要求拆细时的原话，从他这次对话里写的话里一字不改地照抄一段；没有就空"))


def _split_node(a: SplitNodeArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    """A request in chat to split a node, reason requested, source chat. With `parts` (he named
    them) the card lists them, editable, and the graph gets exactly those; without, the shared
    one-level split decides after his confirmation (it may leave the node whole)."""
    from evowiki.goals.intake import IntakeError
    from evowiki.wikiapp.guidelines import quoted_in
    from evowiki.wikiapp.node_split import named_split
    n = _node(snap, a.node)
    if snap.state.graph.has_children(n.id):
        raise Refused(f"「{n.name}」已经有子点了，不能再拆")
    if a.words.strip() and not quoted_in(a.words, ctx.user_words):
        raise Refused(WORDS_NOT_QUOTED)
    content: dict[str, Any] = {
        "node_id": n.id, "reason": "requested", "words": a.words,
        "graph_seen": graph_fingerprint(snap.state.graph, [n.id], parts_only=True)}
    if not a.parts:
        return Plan(kind="node_split", title=_clip_title(f"把「{n.name}」拆细"), why=a.why,
                    content=content, note="拆成哪些点要等他确认后才定，卡片现在不列；不要说会拆成什么，也不要说卡片上有。")
    parts = [(p.name.strip(), p.definition.strip()) for p in a.parts]
    try:
        named_split(snap, n.id, parts)
    except IntakeError as e:
        raise Refused(str(e)) from e
    content["pages"] = [{"key": f"p{i + 1}", "title": name, "covers": line}
                        for i, (name, line) in enumerate(parts)]
    return Plan(kind="node_split", title=_clip_title(f"把「{n.name}」拆成 {len(parts)} 个点"),
                why=a.why, content=content, items=items_of(content),
                note=f"确认后「{n.name}」留在图谱里，这些点挂在它下面。")


class RelationEnd(_A):
    kind: str | None = None
    src: str | None = None
    dst: str | None = None
    phrase: str | None = None


class EditRelationArgs(_A):
    op: Literal["remove", "add", "change"]
    src: str = Field(description="关系的起点（id 或名字）。prerequisite：src 是 dst 的前置")
    dst: str
    kind: Literal["part_of", "prerequisite", "recommended", "kind_of", "related"] = "prerequisite"
    phrase: str = Field(default="", max_length=200)
    to: RelationEnd | None = Field(default=None, description="op=change：改成什么")
    why: str = Field(default="", max_length=300)


def _edit_relation(a: EditRelationArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    src, dst = _node(snap, a.src), _node(snap, a.dst)
    graph = snap.state.graph
    ids = [src.id, dst.id]
    content: dict[str, Any] = {"op": a.op, "kind": a.kind, "src": src.id, "dst": dst.id,
                               "phrase": a.phrase}
    if a.op in ("remove", "change") and not any(r.key == (a.kind, src.id, dst.id)
                                                 for r in graph.relations):
        raise Refused(f"图谱里没有这条关系：{src.name} → {dst.name}（{a.kind}）")
    if a.op == "change":
        if a.to is None:
            raise Refused("op=change 要说明改成什么（to）")
        to = {k: v for k, v in a.to.model_dump().items() if v is not None}
        for end in ("src", "dst"):
            if end in to:
                to[end] = _node(snap, to[end]).id
        content["to"] = to
    try:
        Relation(kind=a.kind, src=src.id, dst=dst.id, source="user")
    except ValidationError as e:
        raise Refused(f"这条关系不合法：{e.errors()[0]['msg']}") from None
    content["graph_seen"] = graph_fingerprint(graph, ids)
    verb = {"remove": "去掉", "add": "添加", "change": "改掉"}[a.op]
    word = {"part_of": "从属", "prerequisite": "前置", "recommended": "建议先学", "kind_of": "类属",
            "related": "相关"}[a.kind]
    return Plan(kind="node_relation", why=a.why, content=content, title=_clip_title(
        f"{verb}「{src.name}」→「{dst.name}」的{word}关系"))


class MarkKnownArgs(_A):
    node: str = Field(description="用户说早就会了的点（id 或名字）")


def _mark_known(a: MarkKnownArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    n = _node(snap, a.node)
    return Plan(kind="node_known", title=_clip_title(f"记下「{n.name}」已经会了"),
                content={"node_id": n.id})


class MoveNodeArgs(_A):
    node: str
    parent: str = Field(description="新的上级点")
    keep_old: bool = Field(default=False, description="true=保留原来的上级，同时属于两个")
    why: str = Field(default="", max_length=300)


def _move_node(a: MoveNodeArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    n, p = _node(snap, a.node), _node(snap, a.parent)
    if n.id == p.id:
        raise Refused("不能把一个点挪到它自己下面")
    graph = snap.state.graph
    seen = graph_fingerprint(graph, [n.id, p.id, *graph.parents(n.id)])
    return Plan(kind="node_move", why=a.why, title=_clip_title(
        f"把「{n.name}」挪到「{p.name}」下面"),
        content={"node_id": n.id, "parent": p.id, "keep_old": a.keep_old, "graph_seen": seen})


# ---- cards / reviews ----

CUE_DESC = "复习题：只有一个问号，只问一件事，不自带答案；几天后单独看到也能回答，他看不到页面，不提这一页的例子和说法"


class CardCheck(ItemArg):
    cell: str = Field(min_length=3, max_length=120,
                      description="这道确认题测的格子 id，必须是这一段的格子")


class CardSpec(_A):
    section_id: str
    cue: str | None = Field(default=None, max_length=200, description=CUE_DESC)
    answer: str | None = Field(default=None, max_length=ANSWER_MAX, description="一两句话回答 cue，不提这一页的例子；给 cue 时必须给")
    check: CardCheck | None = Field(default=None, description="另外加的一道确认题，你写好")


class MakeCardsArgs(_A):
    page: str = Field(description="页面 id")
    cards: list[CardSpec] = Field(min_length=1, description="给哪几段做什么卡")


def _make_cards(a: MakeCardsArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    rec = _page(snap, a.page, written=True)
    ids = {s.id for s in rec.page.outline.sections}                  # type: ignore[union-attr]
    cards = []
    for c in a.cards:
        if c.section_id not in ids:
            raise Refused(f"页面「{rec.title}」没有这一段：{c.section_id}")
        if not c.cue and c.check is None:
            raise Refused(f"段 {c.section_id} 要给回想提示（cue）或者要确认题（check）")
        sec = next(s for s in rec.page.outline.sections                      # type: ignore[union-attr]
                   if s.id == c.section_id)
        if c.cue and not sec.cells:                # no cell: nothing would ever bring the card up
            raise Refused(f"段 {c.section_id} 没有对应的格子，回想卡不会到期")
        if c.cue and not (c.answer or "").strip():
            raise Refused(f"段 {c.section_id} 给了回想提示，还要给答案（answer）")
        check = None
        if c.check is not None:
            if c.check.cell not in sec.cells:
                raise Refused(f"确认题的格子「{c.check.cell}」不是段 {c.section_id} 的格子")
            try:
                check = checked_item(c.check.cell, c.check)
            except ItemRefused as exc:
                raise Refused(f"段 {c.section_id} 的确认题：{exc}") from exc
        cards.append({"section_id": c.section_id,
                      **({"cue": c.cue.strip(), "answer": (c.answer or "").strip()} if c.cue else {}),
                      **({"check": check.model_dump(mode="json")} if check is not None else {})})
    return Plan(kind="card_new", title=_clip_title(f"给「{rec.title}」做 {len(cards)} 张复习卡"),
                content={"page_id": rec.page_id, "cards": cards}, page_id=rec.page_id,
                page_version=page_fingerprint(rec))


class FixCardArgs(_A):
    card_id: str
    opinion: str = Field(min_length=1, max_length=500, description="这张卡哪里有问题")
    cue: str | None = Field(default=None, max_length=200, description=CUE_DESC + "；改回想卡时给")
    answer: str | None = Field(default=None, max_length=ANSWER_MAX, description="一两句话回答 cue，不提这一页的例子；改回想卡时给")
    check: ItemArg | None = Field(default=None, description="确认题卡的新题；改确认题卡时给，测原来的格子")


def _card(snap: Snapshot, card_id: str):
    card = find_card(snap.state, card_id)
    if card is None:
        raise Refused(f"没有这张复习卡：{card_id}（用 read_reviews 看到期的卡）")
    return card


def _new_face(card, a: FixCardArgs) -> dict[str, Any]:
    """The card's new text from the args, checked as the old model rewrite was (stage 3 T12)."""
    if card.mode == "cue":
        cue = " ".join((a.cue or "").split())
        answer = (a.answer or "").strip()
        if a.check is not None or not cue or not answer:
            raise Refused("这是一张回想卡：给回想提示（cue）和答案（answer）")
        old = card.page.texts[card.section.id]
        if len(cue) > CUE_MAX or summary_cue(cue) or (cue, answer) == (old.cue, old.answer):
            raise Refused(f"回想提示要换成问正文里一个具体的点，不超过 {CUE_MAX} 字：{cue}")
        return {"cue": cue, "answer": answer}
    old = card.section.check
    if a.check is None or a.cue is not None or old is None:
        raise Refused("这是一张确认题卡：给新的题（check）")
    try:
        item = checked_item(old.cell, a.check)
    except ItemRefused as exc:
        raise Refused(str(exc)) from exc
    if flaws := near_duplicate(item, old, rekey=True):
        raise Refused("新题和原来的太像：" + "；".join(flaws))
    return {"check": item.model_dump(mode="json")}


def _fix_card(a: FixCardArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    card = _card(snap, a.card_id)
    return Plan(kind="card_fix", title=_clip_title(f"改「{card.rec.title}」的一张复习卡"),
                why=a.opinion,
                content={"op": "fix", "card_id": a.card_id, "opinion": a.opinion.strip(),
                         **_new_face(card, a)},
                page_id=card.rec.page_id, page_version=page_fingerprint(card.rec))


class DropCardArgs(_A):
    card_id: str


def _drop_card(a: DropCardArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    card = _card(snap, a.card_id)
    return Plan(kind="card_fix", title=_clip_title(f"不再出「{card.rec.title}」的一张复习卡"),
                content={"op": "drop", "card_id": a.card_id}, page_id=card.rec.page_id,
                page_version=page_fingerprint(card.rec))


class PostponeReviewsArgs(_A):
    days: int = Field(ge=1, le=365)
    scope: Literal["all", "goal", "cells"] = "all"
    goal_id: str | None = Field(default=None, description="scope=goal")
    cells: list[str] = Field(default_factory=list, description="scope=cells：格子 id，如 zscore.procedure")


def _postpone_reviews(a: PostponeReviewsArgs, snap: Snapshot, ctx: ToolContext,
                      d: ActionDeps) -> Plan:
    content: dict[str, Any] = {"days": a.days, "scope": a.scope}
    what = "到期的复习"
    if a.scope == "goal":
        if a.goal_id is None or a.goal_id not in snap.state.paths:
            raise Refused("scope=goal 要给路径 id（goal_id）")
        content["goal_id"] = a.goal_id
        what = f"「{snap.state.paths[a.goal_id].path.goal}」的复习"
    elif a.scope == "cells":
        if not a.cells:
            raise Refused("scope=cells 要给格子 id")
        known = {f"{c}.{x}" for c in snap.k.concept_ids() for x in snap.k.aspects(c)}
        if bad := [c for c in a.cells if c not in known]:
            raise Refused(f"没有这个格子：{bad[0]}")
        content["cells"] = list(dict.fromkeys(a.cells))
        what = f"{len(content['cells'])} 个格子的复习"
    return Plan(kind="review_postpone", title=_clip_title(f"把{what}往后推 {a.days} 天"),
                content=content)


# ---- rules / settings ----

class RestyleArgs(_A):
    item_id: str = Field(min_length=1, max_length=80, description=(
        "按哪条讲解规则改写（read_rules 里的 id，通常是刚记下的那条）"))
    page_ids: list[str] = Field(default_factory=list, max_length=60, description=(
        "只改这几页；省略=这条规则适用、写在这条规则之前的所有页面"))


def restyle_candidates(state: WikiState, item_id: str) -> list[str]:
    """The written pages the rule applies to that were written under an older spec version (new
    pages already follow it)."""
    from evowiki.wikiapp import teachspec as tspec
    from evowiki.wikiapp.stale import page_version
    spec = tspec.effective_spec(state)
    out = []
    for pid, rec in state.pages.items():
        if not rec.has_body or pid in state.trashed_pages \
                or page_version(state, pid) >= spec.version:
            continue
        if any(i.item_id == item_id
               for i in tspec.applicable(spec, tspec.page_kind(state, pid), rec.domain)):
            out.append(pid)
    return out


def _restyle(a: RestyleArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    from evowiki.wikiapp import teachspec as tspec
    state = snap.state
    rule = next((i for i in tspec.effective_spec(state).items if i.item_id == a.item_id), None)
    if rule is None:
        raise Refused(f"没有这条讲解规则：{a.item_id}（用 read_rules 查）")
    if rule.authority == "pending":
        raise Refused("这条规则还在等他确认，确认以后才能按它改旧页面")
    cands = restyle_candidates(state, a.item_id)
    if a.page_ids:
        if bad := [p for p in a.page_ids if p not in cands]:
            raise Refused(f"「{bad[0]}」不是按旧规则写的、这条规则适用的页面")
        cands = list(dict.fromkeys(a.page_ids))
    if not cands:
        raise Refused("没有要改的旧页面：这条规则适用的页面都是按它写的")
    pages = [{"key": p, "title": state.pages[p].title, "page_id": p} for p in cands]
    return Plan(kind="restyle_pages",
                title=_clip_title(f"按「{rule.text}」重写 {len(cands)} 页旧页面"),
                content={"item_id": a.item_id, "pages": pages},
                items=tuple(ActionItem(key=p["key"], title=p["title"], page_id=p["page_id"])
                            for p in pages),
                note="确认后这些页下次打开时按新规则重写。")


SETTING_NAMES = {"today_cap": "每天的建议条数", "paused": "暂停",
                 "paused_until": "暂停到什么时候", "kinds": "提醒的种类", "levels": "哪些动作要先问",
                 "daily_hour": "每天提醒的时间", "daily_minute": "每天提醒的时间", "timezone": "时区"}


class BotSettingsArgs(_A):
    changes: dict[str, Any] = Field(min_length=1, description=(
        "要改的设置字段和新值：today_cap 每天往「今天」放几条建议（0-50，不是页数）；"
        "paused 暂停（no / 其他值见错误提示）；daily_hour 每天几点提醒（0-23）；kinds 每种提醒的开关；"
        "levels 哪些动作要先问，只能改得更严，如 {\"levels\": {\"new_page\": \"ask\"}}。"
        "没有每天学几页、写几页的上限。他要的东西这里没有就照实说，不改别的字段代替。"))


def _bot_settings(a: BotSettingsArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    from evowiki.bot.actions import ActionRefused
    from evowiki.bot.executors_graph import SettingsExecutor
    if "page_cap" in a.changes:     # its rule is off: a change would show as done and do nothing
        raise Refused("没有这个设置")
    try:       # validated BEFORE anything is proposed: a bad value is a tool error, no row
        changed = SettingsExecutor._changes(d.ledger.settings(ctx.user_id),
                                            {"changes": a.changes})
    except ActionRefused as e:
        raise Refused(e.detail) from e
    if not changed:
        raise Refused("这些设置已经是这样了")
    names = "、".join(dict.fromkeys(SETTING_NAMES.get(k, k) for k in sorted(a.changes)))
    return Plan(kind="settings", title=_clip_title(f"改 Bot 设置：{names}"),
                content={"changes": a.changes})


class MarkSourceUseArgs(_A):
    source_id: str = Field(min_length=1, description="附件或资料的 source_id（见「本会话的附件」或 list_sources）")
    use: Literal["exam", "learn", "understand"] = Field(description=(
        "exam=往年题 / 真题；learn=收进资料以后学；understand=要读懂的文章"))


_USE_ZH = {"exam": "往年题", "learn": "以后要学的资料", "understand": "要读懂的文章"}


def _mark_source_use(a: MarkSourceUseArgs, snap: Snapshot, ctx: ToolContext,
                     d: ActionDeps) -> Plan:
    if d.sources is None:
        raise Refused("现在看不到资料")
    src = d.sources.get(ctx.user_id, a.source_id)
    if src is None:           # another user's or a deleted source reads exactly like an unknown one
        raise Refused(f"没有这份资料：{a.source_id}")
    return Plan(kind="source_use", title=_clip_title(f"把《{src.title}》记为{_USE_ZH[a.use]}"),
                content={"source_id": a.source_id, "use": a.use, "session_id": ctx.session_id})


class SetExemplarArgs(_A):
    page_id: str | None = Field(default=None, description=(
        "要设成样板页的 Wiki 页面 id；省略=他现在打开的这一页"))
    source_id: str | None = Field(default=None, description=(
        "他发到聊天里的笔记（附件的 source_id，见「本会话的附件」）；设他自己的笔记时用，和 page_id 只给一个"))


def _set_exemplar(a: SetExemplarArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    from evowiki.bot.actions import ActionRefused
    from evowiki.bot.executors_graph import exemplar_in
    if a.page_id is not None and a.source_id is not None:
        raise Refused("页面和笔记只能给一个")
    if a.source_id is not None:
        content: dict[str, Any] = {"kind": "vault_note", "source_id": a.source_id}
    elif (pid := a.page_id or ctx.page_id) is not None:
        content = {"kind": "wiki_page", "page_id": pid}
    else:
        raise Refused("要说明是哪一页：在那一页上说，或者把笔记发到聊天里")
    try:       # checked BEFORE anything is written: a bad page / note is a tool error, no row
        _, title = exemplar_in(snap.state, content, d.sources, ctx.user_id)
    except ActionRefused as e:
        raise Refused(e.detail) from e
    return Plan(kind="exemplar", title=_clip_title(f"把「{title}」设为样板页"), content=content)


APPLIES_TEXT = ("lasting=他说以后都这样（「以后都…」「我喜欢…」）；this_task=只对这一题、这一次回答或这次任务"
                "（「这题讲简单点」「今天先快一点」），照做就行，不会记下")
THIS_TASK = "只对这一次有效：照做就行，不用记"
NOT_QUOTED_ANY = "quote 要一字不改地抄他在这次对话里自己写的话；他没说过、是你推断的就留空"
NO_MESSAGE = "现在记不了：这一轮没有他的消息"
PASTED_NO_QUOTE = "贴来的说明里拆出的规则要带 quote：照抄原文里出这条的那一句"


def remember_route(quote: str, ctx: ToolContext) -> tuple[Any, Any, Any] | str:
    """(provenance, evidence, current message) of a remember_* call, decided by code: his words
    in THIS message → explicit_current; in an earlier message of the session → explicit_history
    (waits for his confirmation); no quote → inferred (waits too). A quote found nowhere → the
    tool error."""
    from evowiki.memory.contracts import EvidenceRef
    from evowiki.wikiapp.preferences import CurrentMessage
    if ctx.message_id is None:
        return NO_MESSAGE
    cur = CurrentMessage(ctx.message_id, current_words(ctx))
    q = " ".join(quote.split())
    if not q:
        return ("inferred", EvidenceRef(message_id=ctx.message_id, session_id=ctx.session_id,
                                        role="user"), cur)
    if quoted_now(q, ctx):
        return ("explicit_current", EvidenceRef(message_id=ctx.message_id,
                                                session_id=ctx.session_id, role="user",
                                                quote=q), cur)
    from evowiki.teach.textsim import squash
    hit = next((mid for mid, text in ctx.past_messages if squash(q) in squash(text)), None)
    if hit is None or len(squash(q)) < 4:
        return NOT_QUOTED_ANY
    return ("explicit_history", EvidenceRef(message_id=hit, session_id=ctx.session_id,
                                            role="user", quote=q), cur)


class RememberPreferenceArgs(_A):
    applies: Literal["lasting", "this_task"] = Field(description=APPLIES_TEXT)
    quote: str = Field(default="", max_length=400, description=(
        "他自己说这条偏好的原话，一字不改地照抄一段（这一条消息里的直接记下；更早消息里的要等他确认）；"
        "他没这么说过、是你推断的就留空（也要等他确认）"))
    text: str = Field(min_length=1, max_length=300, description=(
        "以后写页面、讲解时照着做的那句要求，就用他原话里的说法（例如「我喜欢组件多一点的页面」）；"
        "改写过的会先等他确认"))
    replaces: str = Field(default="", max_length=80, description=(
        "这条取代哪条已有规则（read_rules 里的 id）：他改了主意时给；新的一条就省略"))
    domain: str = Field(default="", max_length=60, description="只用在这个领域的页面（如「数学」）；省略=所有页面")
    pasted: bool = Field(default=False, description=(
        "这条出自他贴来的一段「怎么给我讲」的说明（文档、笔记、别处的规则），不是他直接对你说的一句话："
        "一条规则调一次，quote 照抄原文里出这条的那一句，等他确认"))


class RememberPreferenceTool:
    """`remember_preference(applies, quote, text, replaces?, domain?, pasted?)`: a lasting teaching
    preference goes to `wikiapp.preferences.land_preference` (the one landing point; provenance by
    `remember_route`). Active → a 「记下了」 row, undoable; pending → a card he confirms;
    `applies=this_task` → nothing is written. `pasted`: one rule split out of a note he pasted
    (how he likes to be taught; it replaced the retired 「从文字导入」): its `quote` is the source
    sentence, copied verbatim and checked like any quote (not in his words → refused), stored as the
    rule's evidence; such a rule always waits for his confirmation, even when it repeats the
    sentence word for word (a pasted document is not his yes to each line). A new rule never restyles old pages: that is
    `restyle_pages`, its own draft."""

    def __init__(self, deps: ActionDeps):
        self._d = deps
        self.spec = spec(
            "remember_preference", "他明确说出一条以后都适用的偏好（页面怎么排、怎么讲、要什么形式，例如"
            "「我喜欢组件多一点的页面」「以后都先给例子」）时用：记成一条讲解规则，以后写页面都照着做，"
            "已经写好的页面不会跟着改（要改旧页面用 restyle_pages）。他改了主意就用 replaces 指出旧的那条。"
            "明确说「以后」「每章」等长期要求时，quote 和 text 都照抄原话里的那句要求，不增加未说出的规则。"
            "同一条消息还以「比如这一章」要求修改当前页时，既记长期要求，又用页面编辑工具为当前页出草稿，不自动批量改旧页面。"
            "只针对这一次的要求给 applies=this_task。text 只写怎么讲；他同一句里还说了自己的事（「我是吉他手」「我的狗叫豆豆」）的，那部分另调 remember_about_me，两条都记。", RememberPreferenceArgs, "propose",
            "action", acts=True, tier="direct")

    @property
    def args_model(self) -> type[BaseModel]:
        return RememberPreferenceArgs

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(RememberPreferenceArgs, args, lambda a: self._run(a, ctx))

    @staticmethod
    def _drafted(ctx: ToolContext, quote: str, text: str) -> str:
        from evowiki.rules.drafted import drafted_authority
        return drafted_authority(quote=quote, text=text, words=current_words(ctx))

    def _run(self, a: RememberPreferenceArgs, ctx: ToolContext) -> ToolResult:
        from evowiki.memory.contracts import MemoryScope
        from evowiki.rules.authority import RuleAuthorityError
        from evowiki.wikiapp.preferences import land_preference
        from evowiki.wikiapp.repo import WikiConflict
        from evowiki.wikiapp.teachspec import SpecInvalid
        if a.applies == "this_task":
            return ToolResult(content=THIS_TASK)
        if a.pasted and not a.quote.strip():
            return fail(PASTED_NO_QUOTE)
        route = remember_route(a.quote, ctx)
        if isinstance(route, str):
            return fail(route)
        prov, ev, cur = route
        d = self._d
        # his words in this message make the rule his only when the rule says what he said
        # (contract §6 decision 1, decided in code); a reworded one waits for his yes
        pending = a.pasted or (prov == "explicit_current"
                               and self._drafted(ctx, ev.quote or "", a.text) != "user")
        try:
            got = land_preference(d.repo, d.repo.load(ctx.user_id), kind="teaching", text=a.text,
                                  provenance=prov, evidence=(ev,),
                                  scope=MemoryScope(scope_type="global"),
                                  replaces=a.replaces or None, pending=pending,
                                  idempotency_key=_step_key(ctx),
                                  session_id=ctx.session_id, now=d.clock(), new_id=d.new_id,
                                  current=cur, domain=a.domain.strip() or None,
                                  judge=d.rule_judge(ctx.user_id))
        except (RuleAuthorityError, SpecInvalid, WikiConflict, RuntimeError) as e:
            return fail(f"没记成：{e}")
        if got.status == "refused":
            return fail(got.reason or "没记成")
        text = " ".join(a.text.split())
        if got.status == "active" and got.action_id is None:      # a rule already in force
            return ToolResult(content=f"{ALREADY_IN_FORCE}（{got.item_id}）：{text}")
        if got.existing and got.status == "pending" and prov == "explicit_current" and not pending:
            # his own words now restate a card waiting for him: that is his yes to it
            done = approve(d.ledger, ctx.user_id, got.pending_action_id or "")
            if done.status == "executed":
                return ToolResult(content=f"已记下（用的是已有的那条 {got.item_id}，{done.action_id}，"
                                          f"可以用 undo 撤销）：{text}", action=card_of(done))
        row = d.ledger.get(ctx.user_id, got.action_id or "")
        card = card_of(row) if row is not None else None
        if got.status == "active":
            sup = f"；取代了原来的 {'、'.join(got.superseded)}" if got.superseded else ""
            return ToolResult(content=f"已记下（{got.action_id}，可以用 undo 撤销）：{text}{sup}",
                              action=card)
        return ToolResult(content=f"已生成等他确认的卡片（{got.action_id}）：{text}。他确认前这条不会"
                                  "用在页面里。", action=card)


# remember_*: his quote is a piece of THIS message (the grounding rule of `asked`)
FORGET_SHOWN = 8                   # hits a forget card's reply lists
NOT_QUOTED_NOW = "quote 要一字不改地抄他这一条消息里的话（至少 4 个字，或者就是他整条消息）"
STATED_TITLE = "记下了："        # the quiet chat line: 「记下了：<the principle>」 · 撤销
ALREADY_IN_FORCE = "这条已经在用了，不用再记"


class ReportProblemArgs(_A):
    quote: str = Field(min_length=1, max_length=400, description=(
        "他抱怨的原话，从他这次对话里写的话里一字不改地照抄一段"))
    problem: str = Field(min_length=1, max_length=300, description=(
        "一句话写清系统哪里做得不对，例如「他说过不会以后，摸底又问了同一个点」"))


REPORTED = "已记下这个问题"


class ForgetMemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(min_length=1, max_length=5)
    quote: str = Field(min_length=1, max_length=200, description="抄他这次对话里要求忘掉的原话")


class ReportProblemTool:
    """`report_problem(quote, problem)`: the learner's complaint about what the system does goes to
    the harness RSI's feedback (a `user_complaint` signal, `wikiapp.signals.record_complaint`), so
    the next harness round's improver sees it. The chat shows one quiet row 「已记下这个问题」."""

    def __init__(self, deps: ActionDeps):
        self._d = deps
        self.spec = spec(
            "report_problem", "他在抱怨系统本身的做法时一定要调用（「都说了不会，为什么还老问」「怎么又问这个」「说过的又忘了」"
            "「老是这样」）：不调用就没有记下，回答不能说记下了。先调用，再回应他。只是对这一题、这一段内容有疑问不算。", ReportProblemArgs, "propose", "action", acts=True, tier="direct")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(ReportProblemArgs, args, lambda a: self._run(a, ctx))

    def _run(self, a: ReportProblemArgs, ctx: ToolContext) -> ToolResult:
        from evowiki.wikiapp.guidelines import NOT_QUOTED, quoted_in
        from evowiki.wikiapp.signals import record_complaint
        if not quoted_in(a.quote, ctx.user_words):
            return fail(NOT_QUOTED)
        mid = (ctx.message_id if quoted_in(a.quote, current_words(ctx)) else
               next((mid for mid, text in ctx.past_messages if quoted_in(a.quote, (text,))), None))
        if ctx.session_id is None or mid is None:
            return fail('找不到这条反馈的来源消息，没有记录')
        d = self._d
        sid = record_complaint(d.repo, ctx.user_id, problem=a.problem, quote=a.quote,
                               page_id=ctx.page_id, session_id=ctx.session_id, message_id=mid,
                               now=d.clock())
        if d.feedback_notify is not None:
            d.feedback_notify(ctx.user_id)
        return ToolResult(content=f"{REPORTED}（{sid}），改进系统的那一步会看到。")


class RememberAboutMeArgs(_A):
    model_config = ConfigDict(extra="forbid",
                              json_schema_extra=lambda s: s["required"].append("topic"))

    section: Literal["我是谁", "想学什么"] = Field(description=(
        "我是谁=他的身份、做过的事、会的技能、家人宠物这类生活事实（知识点会不会记进 K，不记这里）；想学什么=他想学、"
        "想做的事（想做不等于做过，只能放这里）"))
    applies: Literal["lasting", "this_task"] = Field(description=(
        "lasting=过了这次还成立的事；this_task=只对这一次（「今天只有半小时」「这周很忙」），照着用，不会记下"))
    quote: str = Field(default="", max_length=400, description=(
        "他自己说这件事的原话，一字不改地照抄一段（这一条消息里的直接记下；更早消息里的要等他确认）；"
        "是你推断的就留空（也要等他确认）"))
    text: str = Field(min_length=1, max_length=200, description=(
        "档案里的一句话，只写他话里有的意思，例如「线性代数完全零基础」「每天能学半小时」；不写"
        "「他」「用户」"))
    topic: str = Field(default="", max_length=60, description=(
        "这句话只说某个领域或技能时写它（Python、线性代数）；说他整个人时写空字符串"))
    replaces: str = Field(default="", max_length=200, description=(
        "这句更新的是档案里已有的哪一句（照抄那句），新的一件事就省略"))


PROFILE_FACT = "profile_fact"      # the ledger kind of remember_about_me (bot.executors_profile)


def _remember_about_me(a: RememberAboutMeArgs, snap: Snapshot, ctx: ToolContext,
                       d: ActionDeps) -> Plan:
    from evowiki.wikiapp.guidelines import stated_evidence
    from evowiki.wikiapp.preferences import fact_meta, message_ref
    if ctx.message_id is None or not quoted_now(a.quote, ctx):   # this message only
        raise Refused(NOT_QUOTED_NOW)
    text = scoped_line(a.topic, a.text)
    why = stated_evidence(a.quote)
    return Plan(kind=PROFILE_FACT, title=_clip_title(f"{STATED_TITLE}{text}"), why=why,
                content={"section": a.section, "quote": a.quote, "text": text,
                         "old_line": " ".join(a.replaces.split()),
                         "meta": fact_meta("explicit_current",
                                           message_ref(ctx.session_id, ctx.message_id), why)})


class ForgetAboutMeArgs(_A):
    line: str = Field(min_length=1, max_length=300, description="要删的那一句，照档案「我」里的原样写")
    quote: str = Field(min_length=1, max_length=300, description="他这条消息里要求忘掉的原话，一字不改")


def _forget_memory(a: "ForgetMemoryArgs", snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    from evowiki.memory.forget_fact import KIND, episode_key, fact_hits, waiting_cards
    from evowiki.wikiapp.guidelines import stated_evidence
    if not quoted_now(a.quote, ctx):
        raise Refused(NOT_QUOTED_NOW)
    if d.memory_store is None:
        raise Refused("现在读不到记忆")
    words = tuple(dict.fromkeys(w.strip() for w in a.keywords if w.strip()))
    # the message asking names the words too; it is no memory to report. What he said earlier in this
    # conversation is: 「我家猫叫年糕」 three turns ago is forgotten like any other
    asking = episode_key(ctx.session_id, ctx.message_id or "")
    hits = [h for h in fact_hits(d.memory_store, ctx.user_id, words) if h.key != asking]
    cards = waiting_cards(d.ledger, ctx.user_id, words)
    lines = [ln for ls in (d.profile_lines(ctx.user_id) if d.profile_lines else {}).values()
             for ln in ls if any(w in ln for w in words)]
    if not hits and not cards:
        raise Refused(f"以前的记忆和等确认的卡片里都没有「{'、'.join(words)}」"
                      + ("；档案里有这一句，用 forget_about_me" if lines else ""))
    shown = "\n".join(f"- {h.text}" for h in hits[:FORGET_SHOWN])
    shown_cards = "\n".join(f"- 等确认的卡片：{c.summary}" for c in cards)
    more = f"\n另外 {len(hits) - FORGET_SHOWN} 处" if len(hits) > FORGET_SHOWN else ""
    in_note = f"\n档案「我」里还有「{'」「'.join(lines)}」，这也是他要忘的：同时用 forget_about_me 出它的删除草稿。" if lines else ""
    return Plan(kind=KIND, title=_clip_title(f"忘掉「{'、'.join(words)}」：{len(hits)} 处记忆，{len(cards)} 张卡片"),
                why=stated_evidence(a.quote), content={"words": list(words), "quote": a.quote},
                note=(f"确认后这些不再被想起，也不再整理进记忆，对话摘要里提到它的句子也不再带给你，可以撤销；"
                      f"对话原文还留在历史对话里，页面正文和档案「我」不受影响。"
                      f"把下面命中的内容告诉他：\n{shown}\n{shown_cards}{more}{in_note}"))


def _forget_about_me(a: ForgetAboutMeArgs, snap: Snapshot, ctx: ToolContext, d: ActionDeps) -> Plan:
    from evowiki.teach.textsim import squash
    from evowiki.wikiapp.guidelines import stated_evidence
    if not quoted_now(a.quote, ctx):
        raise Refused(NOT_QUOTED_NOW)
    if d.profile_lines is None:
        raise Refused("现在读不到档案")
    want = squash(a.line)
    hits = [(sec, ln) for sec, lines in d.profile_lines(ctx.user_id).items() for ln in lines
            if sec in FACT_SECTIONS and (want in squash(ln) or squash(ln) in want)]
    if not hits:
        raise Refused("档案「我」里没有这一句；还在等确认的卡片，点卡片上的取消就行")
    if len(hits) > 1:
        raise Refused("档案里有几句都对得上：" + "、".join(f"「{ln}」" for _, ln in hits) + "，line 写全要删的那一句")
    section, text = hits[0]
    return Plan(kind="profile_forget", title=_clip_title(f"从「我」删掉：{text}"), why=stated_evidence(a.quote),
                content={"section": section, "text": text, "quote": a.quote})


class CancelCardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=80, description="上下文里那张等他确认的卡片的 id")
    quote: str = Field(min_length=1, max_length=200, description="抄他这次对话里说不要、取消的原话")


class CancelCardTool:
    """`cancel_card`: he says in the chat that he does not want a card that waits for him."""

    def __init__(self, deps: ActionDeps):
        self._d = deps
        self.spec = spec("cancel_card", "他说不要、取消一张等他确认的卡片（「那条不用记」「别记猫的事」）时用。",
                         CancelCardArgs, "propose", "action", acts=True)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(CancelCardArgs, args, lambda a: self._run(a, ctx))

    def _run(self, a: CancelCardArgs, ctx: ToolContext) -> ToolResult:
        if not quoted_now(a.quote, ctx):
            return fail(NOT_QUOTED_NOW)
        try:
            row = durable.outside_dbos(lambda: self._d.ledger.reject(ctx.user_id, a.action_id))
        except (NotDecidable, LookupError) as e:
            return fail(str(e))
        return ToolResult(content=f"已取消：{row.summary}。这张卡片不会再等他确认。")


FORGET_UNDONE = "这些内容又能被想起了。忘掉生效那段时间它们被遮住，那时说的「没找到」是因为这个，不是没查全，提到就这样说。"


class RememberAboutMeTool(_ActionTool):
    """`remember_about_me(section, quote, text, replaces?)`: one fact he said about himself goes
    into his profile note through a ledger row (kind `profile_fact`, recorded and executed at once,
    undoable with `undo`: `bot.executors_profile.ProfileFactExecutor`). The quote must be his own
    words of the session (the rule of `remember_preference`), so nothing he did not say is written
    about him. The chat shows its quiet row (assistant.tool_rows QUIET_ROWS), not a card: the loop
    logs the row as `quiet_action` and sends no action event (a new card kind would be a UI
    change); the session still knows the row (the undo line, `undo`)."""

    def __init__(self, deps: ActionDeps):
        super().__init__(
            deps, "remember_about_me", "他明确说起自己的事时用（是谁、做过什么、会什么、想学什么、每天能花多少时间，还有家人、宠物、住哪这类生活事实，"
            "例如「线性代数完全零基础」「我是护士」「每天能学一小时」「我的狗叫豆豆」）：记进他的档案「我」。"
            "怎么讲、页面怎么排用 remember_preference。只对这次任务的条件和眼下一时的情况（「今天只有半小时」「这周很忙」）这一轮照着用，"
            "不记；你从他的问题或答错推出来的（「他可能不懂微积分」）不是他说的，不记。他只是想做某件事，"
            "放「想学什么」，不能写成他做过或会。档案里已经有的不用再记。",
            RememberAboutMeArgs, _remember_about_me, mode="auto")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            a = RememberAboutMeArgs.model_validate(args)
        except ValidationError:
            return super().run(args, ctx)          # the schema error, as every tool says it
        if a.applies == "this_task":
            return ToolResult(content=THIS_TASK)
        route = remember_route(a.quote, ctx)
        if isinstance(route, str):
            return fail(route)
        if route[0] != "explicit_current":
            return self._pending(a, route, ctx)
        r = super().run(args, ctx)
        if r.error is not None or r.action is None:
            return r
        return ToolResult(content=f"已记进档案「{a.section}」：{scoped_line(a.topic, a.text)}"
                                  f"（{r.action.action_id}，可以用 undo 撤销）", action=r.action)

    def _pending(self, a: "RememberAboutMeArgs", route: tuple, ctx: ToolContext) -> ToolResult:
        """Words from an earlier message, or inferred: a card he confirms
        (`wikiapp.preferences.land_preference`, kind about_me)."""
        from evowiki.memory.contracts import MemoryScope
        from evowiki.wikiapp.preferences import land_preference
        prov, ev, cur = route
        d = self._deps
        try:
            line = scoped_line(a.topic, a.text)
            got = land_preference(d.repo, d.repo.load(ctx.user_id), kind="about_me", text=line,
                                  provenance=prov, evidence=(ev,),
                                  scope=MemoryScope(scope_type="global"), section=a.section,
                                  replaces=a.replaces or None, idempotency_key=_step_key(ctx),
                                  session_id=ctx.session_id, now=d.clock(), new_id=d.new_id,
                                  current=cur)
        except RuntimeError as e:
            return fail(f"没记成：{e}")
        if got.status == "refused":
            return fail(got.reason or "没记成")
        row = d.ledger.get(ctx.user_id, got.action_id or "")
        card = card_of(row) if row is not None else None
        return ToolResult(content=f"已生成等他确认的卡片（{got.action_id}）：档案「{a.section}」"
                                  f"{line}。他确认前不会记进档案。", action=card)


# ============================== other tools ==============================

class UndoArgs(_A):
    action_id: str = Field(default="last", description='动作 id，或 "last"=这次对话里最近一次已执行、能撤销的')
    other_sessions: bool = Field(default=False, description=(
        "true：什么都不撤销，只列出别的对话里能撤销的动作和它们所在的对话（他说的是以前那次对话里的改动时"
        "用，再按他点名的那个 action_id 撤销）"))


def session_of(row: Any) -> str | None:
    """The assistant session whose turn wrote `row`: a page-edit proposal's `session_id`, else its
    key (`assistant:<session>:…` inline, `assistant:turn:<session>:<message>:<step>` in a durable
    turn, `_step_key`); None when neither says."""
    sid = row.content.get("session_id") if isinstance(row.content, dict) else None
    if isinstance(sid, str) and sid:
        return sid
    head, _, rest = (getattr(row, "idempotency_key", "") or "").partition(":")
    if head != "assistant":
        return None
    return rest.removeprefix("turn:").partition(":")[0] or None


def undoable(ledger: Any, user_id: str, limit: int = 50) -> list[Any]:
    """The user's rows the assistant may undo, newest decision first."""
    rows = [r for r in ledger.list(user_id, status="executed", limit=limit)
            if r.source == SOURCE and can_undo(r)]
    rows.sort(key=lambda r: r.decided_at or r.created_at, reverse=True)
    return rows


UNDO_LISTED = 10                  # other sessions' rows `other_sessions` lists


def _other_line(row: Any) -> str:
    at = (row.decided_at or row.created_at).astimezone(usage_tz())
    from evowiki.assistant.forgotten import prompt_title
    return (f"- {row.action_id}「{prompt_title(row)}」（对话 {session_of(row) or '不明'}，"
            f"{at:%m-%d %H:%M} 执行）")


class UndoTool:
    """`undo`: a bare 「撤销」 ("last") undoes the newest undoable row of THIS session (live S3 run
    2026-10-28: session B's 「撤销」 undid session A's split). Another session's row is undone
    only by its id; `other_sessions` lists them with the session they came from."""

    def __init__(self, deps: ActionDeps):
        self._d = deps
        self.spec = spec("undo", "撤销账本里的一个已执行动作（用户说「撤销」「改回去」时用）。"
                         "动作 id 在之前的工具结果里；不知道就用 last。", UndoArgs, "propose",
                         "action", acts=True)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(UndoArgs, args, lambda a: self._run(a, ctx))

    def _run(self, a: UndoArgs, ctx: ToolContext) -> ToolResult:
        led = self._d.ledger
        if a.other_sessions or a.action_id == "last":
            rows = undoable(led, ctx.user_id)
            mine = [r for r in rows if session_of(r) == ctx.session_id]
            others = [r for r in rows if session_of(r) != ctx.session_id]
            if a.other_sessions:
                if not others:
                    return ToolResult(content="别的对话里没有能撤销的动作。")
                return ToolResult(content="别的对话里能撤销的（他点名要撤哪个，再用它的 action_id）：\n"
                                  + "\n".join(_other_line(r) for r in others[:UNDO_LISTED]))
            if not mine:
                more = "；别的对话里有，用 other_sessions 列出来" if others else ""
                return fail(f"这次对话里没有可以撤销的动作{more}")
            row = mine[0]
        else:
            row = led.get(ctx.user_id, a.action_id)
            if row is None:
                return fail(f"没有这个动作：{a.action_id}")
        try:
            done = durable.outside_dbos(lambda: led.undo(ctx.user_id, row.action_id))
        except NotDecidable as e:
            return fail(str(e))
        except NotImplementedError:
            return fail("这类动作不能撤销")
        card = card_of(done)
        from evowiki.store.bot import MEMORY_FORGET_KIND
        back = FORGET_UNDONE if done.kind == MEMORY_FORGET_KIND else ""
        return ToolResult(content=f"已撤销：{card.title}。{back}", action=card)


URL_FAILED = "打不开这个网址，可以把正文贴进来"


class AddSourceArgs(_A):
    url: str | None = Field(default=None, description="网址")
    text: str | None = Field(default=None, max_length=20000, description="一段文字资料（贴进来的正文）")
    title: str | None = Field(default=None, max_length=200)


class AddSourceTool:
    """`add_source(url | text, title?)`: imports the material for real (`ActionDeps.import_source`,
    use "supplement" of the current page / session); the import runs in the background and its
    end starts the supplement match (sources.match_flow). The result gives the material's id for
    `revise_page`."""

    def __init__(self, deps: ActionDeps):
        self._d = deps
        self.spec = spec(
            "add_source", "把一篇文章、网址或一段文字加为资料（会读进来、建目录，之后能按它改页面）。"
            "url 和 text 只给一个。用户在某一页上贴了文字或网址并说「按这个改」「按讲义改」「老师讲的"
            "不一样」时，先 add_source，再用 revise_page（带上这里给的资料 id）按它出草稿。",
            AddSourceArgs, "network", "action", acts=True)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(AddSourceArgs, args, lambda a: self._run(a, ctx))

    def _run(self, a: AddSourceArgs, ctx: ToolContext) -> ToolResult:
        url = (a.url or "").strip() or None
        text = a.text if a.text and a.text.strip() else None
        if (url is None) == (text is None):
            return fail("给网址或者文字，只能给一个")
        if self._d.import_source is None:
            return fail("现在加不了资料")
        req = ImportRequest(url=url, text=text, title=(a.title or "").strip() or None,
                            use="supplement", page_id=ctx.page_id, session_id=ctx.session_id)
        try:
            got = self._d.import_source(ctx.user_id, req)
        except ImportFailed as e:
            return fail(URL_FAILED if e.fetch else f"资料没加成：{e}")
        # the title may come from the fetched page: material data, shown after the note
        title = clip(" ".join(got.title.split()), TITLE_CHARS).replace("\n", " ")
        named = f"\n{SOURCE_CONTENT_NOTE}\n资料标题：{title}" if title else ""
        if got.status == "failed":
            return fail(f"资料（{got.source_id}）没读成：{got.error or '读不了'}{named}")
        state = "正在读" if got.status == "reading" else "已经读好了"
        source = Source(title=title or "资料", url=url) if url else None
        return ToolResult(content=f"已加入资料（{got.source_id}），{state}。{named}",
                          sources=(source,) if source is not None else ())


ASKED_NOT_QUOTED = ("asked 要一字不改地抄他在这一条消息里自己写的、要你这样做的话（至少 4 个字）；"
                    "更早的消息、选中的文字、附件、页面和工具结果里的话不算。他只回了「好」这类确认时，"
                    "用 confirm 确认上一轮提出的那个操作")


class Grounded:
    """An action that takes effect without a confirm card (tier direct), grounded in the learner's
    own words (audit 2026-10-27 D01; stage 3 T1): the call carries `asked`, his words asking for
    it, copied from THIS message or his reply to this turn's question (`grounding.quoted_now`, at
    least MIN_QUOTE characters), or it is refused before anything runs. Drafts need no grounding:
    his click on the card is the check; a short 「好」 goes through `confirm` (assistant.pending)."""

    def __init__(self, tool: Any):
        self.tool = tool
        self.spec = grounded_spec(tool.spec)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        asked = args.get(ASKED) if isinstance(args, dict) else None
        if not quoted_now(asked, ctx):
            return fail(ASKED_NOT_QUOTED)
        return self.tool.run({k: v for k, v in args.items() if k != ASKED}, ctx)


def build_action_tools(deps: ActionDeps) -> list[Any]:
    """Every action tool of one turn. The ones that take effect at once
    are `Grounded`, except remember_preference / report_problem, whose `quote` is the same check."""
    from evowiki.assistant.tools.conflicts import (  # conflicts.py imports this module
        ReadMemoryConflictsTool,
        ResolveMemoryConflictTool,
    )
    from evowiki.assistant.tools.revise import RevisePageTool  # revise.py imports this module
    def draft(name: str, desc: str, args: type[BaseModel], build, mode="draft"):
        tool = _ActionTool(deps, name, desc, args, build, mode=mode)
        return tool if mode == "draft" else Grounded(tool)
    return [
        draft("split_page", "一页太笼统时拆成几页更细的，在路径里替换原页（原页留在 Wiki）。用户明说要"
              "拆（或说太笼统、帮我拆解）时用。拆分只需页面大纲和路径，不必读各段全文；"
              "读全文是为了引用或讲解内容。返回草稿，用户可调整页面再确认。" + SPLIT_FRESH + "。",
              SplitPageArgs, _split_page),
        draft("merge_pages", "几页合成一页：新页写完后原页进回收站。返回草稿，用户确认后才做。",
              MergePagesArgs, _merge_pages),
        draft("add_practice", "给已写好的一页加练习题 / 记忆卡 / 交互：现在就写好并检查，草稿直接显示"
              "在那一页里（标成草稿，可以试），用户确认后才加进页面。", AddPracticeArgs, _add_practice),
        draft("illustrate_section", "给当前页面指定这一节配图、画示意图或把图画进页面。先 read_page 确认"
              "section_id；由页面写作模型用图库或 draw_visual 生成，只改这一节，返回页面里的修订草稿。"
              "用户确认后才应用，可撤销；不要在聊天或 propose_page_edit 里拼字符图。",
              IllustrateSectionArgs, _illustrate_section),
        draft("write_page", "写一页新页面（可以围绕图谱里或只在笔记里出现的点）；给 path_id 就放进那条路径。",
              WritePageArgs, _write_page),
        draft("cheat_sheet", "把几页或一条路径总结成一页速查：每个点一两行、公式、一个例子。",
              CheatSheetArgs, _cheat_sheet),
        draft("reorder_path", "用户要调整学习顺序时：把给出的页放到最前面（按给的顺序），其余保持原顺序。",
              ReorderArgs, _reorder),
        draft("postpone", "把路径上某几页往后放。", PostponeArgs, _postpone),
        draft("set_deadline", "给目标设考试 / 截止日期：复习间隔压到考前；路径顺序不变。",
              DeadlineArgs, _deadline),
        draft("new_goal", "开一个新学习目标：用户说想学一个新东西时用。要学的目标先分成几大块，"
              "返回分块草稿（卡片上每行一块，用户可以拖动、去掉、加一块再确认；确认后在聊天里一题一题"
              "做摸底）。用户附了资料并想按它学或读懂它时，把资料 id 放进 source_ids；goal 写用户的原话，"
              "附了资料时可以空；先用 web_search 查过这个领域时，把要点放进 notes。结果里有分块和"
              "写回复用的几句（开头、提醒、主线、从哪里开始、问题）。", NewGoalArgs, _new_goal),
        draft("edit_goal_blocks", "改这次会话里还没确认的分块草稿：用户说哪几块合并、哪块不学、改名、"
              "加一块、调顺序、改分级、把某个小主题挪到另一块时用，他回答了分块时问的问题、要换排法时也用；"
              "他改了这个目标的条件（时间、形式）时给 limits。按顺序给出改动，块用草稿里的名字指；"
              "返回一张新的分块草稿（旧的那张作废）和改了什么。",
              EditGoalBlocksArgs, _edit_goal_blocks),
        draft("edit_reading", "改这次会话里还没确认的带读草稿的分段：相邻几段合成一段、某一段不读、"
              "恢复一段不读的；段用草稿里的编号指。要先补的东西用 edit_goal_blocks 改。",
              EditReadingArgs, _edit_reading),
        draft("set_goal_limits", "改一个已经开始的大目标的条件：他说时间、形式变了（例如「不急了」「只用 Windows」）"
              "时用；给出改完以后的全部条件。条件只是一段文字，以后拆块时参考，不限制每天学或写几页。"
              "还没确认的分块草稿用 edit_goal_blocks 的 limits。直接生效，可撤销。",
              GoalLimitsArgs, _goal_limits, "settings"),
        draft("edit_path_blocks", "改已经确认的目标的学习路径：删一块、改名、加一块、调顺序，马上生效，可撤销。"
              "还没确认的分块草稿用 edit_goal_blocks。", EditPathBlocksArgs, _edit_path_blocks, "settings"),
        draft("pause_goal", "暂停（paused=true）或继续（false）一个目标：暂停后不再空闲写它的页、"
              "不提「目标搁置」，复习照常。", PauseGoalArgs, _pause_goal),
        draft("split_node", "他要求把图谱里的一个点拆细时用。他说了拆成什么就写 parts；没说就不写，确认后由模型决定怎么拆，可能判断它不用再拆。", SplitNodeArgs, _split_node),
        draft("edit_relation", "去掉 / 加上 / 改一条图谱关系（如用户说「A 不需要 B」）。加的关系是"
              "用户的、未核实的软顺序。", EditRelationArgs, _edit_relation),
        draft("mark_known", "用户说「这个我早会了」：出一张草稿，他确认后才记成他说会了（路径上会显示可以跳过，路径本身不改），"
              "随后出一道确认题，答错就撤回。", MarkKnownArgs, _mark_known),
        draft("move_node", "把一个点挪到另一个上级下面（可以同时属于两个）。", MoveNodeArgs, _move_node),
        draft("make_cards", "给一页的几段做复习卡（回想提示 cue 和答案 answer，和 / 或你写好的确认题 check）。",
              MakeCardsArgs, _make_cards, "auto"),
        draft("fix_card", "按用户的意见改一张有问题的复习卡：新的提示和答案，或新的题，由你写在参数里。",
              FixCardArgs, _fix_card, "auto"),
        draft("drop_card", "不再出某张复习卡。", DropCardArgs, _drop_card, "auto"),
        draft("postpone_reviews", "把到期的复习往后推几天（只改到期日，不改遗忘模型）。",
              PostponeReviewsArgs, _postpone_reviews, "auto"),
        draft("restyle_pages", "他要已经写好的页面也照一条讲解规则改（如「把以前的数学页都改成这样」）时用："
              "列出按旧规则写的、这条规则适用的页面，出一张草稿，他确认后这些页下次打开时"
              "重写。新记下的规则本身不会改旧页面。他没点名哪一条（「按我现在的规则改一遍」）就用他自己定的最新一条，这一轮就出草稿，不反问。",
              RestyleArgs, _restyle),
        draft("forget_memory", "他要忘掉一件事（「忘掉我家猫叫年糕」「别再提蓝鲸计划」）时用：按 keywords 找出记忆里提到它的"
              "内容和等确认的卡片，出一张草稿列出来，他确认后不再被想起、也不再整理进记忆，可撤销。keywords 写能认出这件事的"
              "词（「年糕」），不写「猫」这种太宽的词。这张卡确认生效之前，不要说「之后不会再记」。", ForgetMemoryArgs, _forget_memory),
        CancelCardTool(deps),
        draft("forget_about_me", "他要忘掉档案「我」里的一句（「忘掉我是大三学生」）时用：出一张删除草稿，他确认后才删，可撤销。",
              ForgetAboutMeArgs, _forget_about_me),
        draft("bot_settings", "改 Bot 设置：「今天别提醒了」「卡少一点」「新建页面先问我」。审批级别"
              "只能改得更严。直接生效，可撤销。", BotSettingsArgs, _bot_settings, "settings"),
        draft("mark_source_use", "记下一份附件 / 资料的用处：用户说这是往年题、真题 → exam；只想收进资料、"
              "先存着以后学 → learn。要按它学或读懂它是开目标，用 new_goal。直接记下，不用确认。",
              MarkSourceUseArgs, _mark_source_use, "auto"),
        draft("set_exemplar", "把一页设为样板页：以后写新页面照着它的写法。用户说「把这页设为样板页」"
              "「以后都照这页的样子写」时用；省略参数=他现在打开的这一页，他自己的笔记要先发到聊天里再给"
              " source_id。最多 3 个样板页。直接生效，可撤销。", SetExemplarArgs, _set_exemplar,
              "settings"),
        RememberPreferenceTool(deps),
        ReadMemoryConflictsTool(deps),
        ResolveMemoryConflictTool(deps),
        ReportProblemTool(deps),
        RememberAboutMeTool(deps),
        Grounded(UndoTool(deps)),
        Grounded(AddSourceTool(deps)),
        RevisePageTool(deps),
    ]
