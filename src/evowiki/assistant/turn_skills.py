"""One assistant turn's Skills, tools per request and pending ops (stage 3 T3/T5/T8), kept out of
`assistant.loop` so the loop only asks: which tools now (`request_names`), may this one run
(`allowed`), what does the system prompt add (`system_rules`), what does the user message list
(`ops_line`), what does the answer log (`logged`).

Everything here is a function of the folded session, the turn's code signals and the turn's
recorded tool results (fed in order through `after_tool`, also on a durable replay where the tools
do not run again), so a recovered turn recomputes the same tool sets and the same closed ops: the
ledger lookups (row statuses for the pending ops, the undoable rows) are one recorded step,
`assistant.ops`; the routed model (for the exposure) another, `assistant.exposure`."""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from evowiki.agent import durable
from evowiki.agent.catalog import (
    LOAD_SKILL,
    READ_REF,
    Activation,
    LoadSkillTool,
    ReadRefTool,
    SkillLibrary,
    ToolSet,
    activate,
    library_version,
)
from evowiki.agent.spec import Restart
from evowiki.agent.tools import ActionCard, Tool, ToolResult
from evowiki.assistant.contracts import CONFLICTS_INTENT, RETEACH_INTENT
from evowiki.assistant.pending import (
    ConfirmTool,
    OfferTool,
    PendingOp,
    last_ops,
    ledger_op,
    proposal_op,
    proposed,
    valid_ops,
)
from evowiki.assistant.skills import (
    CORE,
    UNDO,
    carry,
    library,
    preload,
    registry,
    skill_of_tools,
    turn_rules,
)
from evowiki.llm.chat import ToolCall
from evowiki.llm.exposure import ToolExposure, rejects_tool_change

log = logging.getLogger(__name__)

LOADERS = frozenset({LOAD_SKILL, READ_REF})
LATER = "\n（这些工具这一轮还不能用，下一轮可以）"
LOADED_HEAD = "已载入："
OPS_HEAD = ("可以撤销或等他确认的操作（id、标题、状态），这些卡片现在的状态以这里为准，摘要和前面的消息可能是旧的；"
            "除了标明来自别的对话的，都是这段对话里的，来历和时间没写就别说；和他这条消息无关的不提：")
# a card of this session as its row stands now: the summary (compact) was written earlier and only
# says what was so at the time; a status not here is said elsewhere (已执行，可以撤销) or by the tool
CARD_STATES = {"proposed": "还等他在卡片上确认", "approved": "他确认了，正在执行", "executed": "已执行",
               "undone": "已撤销", "rejected": "他取消了，没有执行"}
_ABOUT_WAITING = re.compile(r"待确认|等我确认|卡片|档案|偏好|讲解规则|记忆|\b(?:confirm|confirmation|card|background|profile|preference|teaching rule|memory|memories)\b", re.IGNORECASE)
OPS_LIMIT = 20                    # the session's latest cards whose rows the ops line reads
UNDO_LIMIT = 50                   # the user's latest executed rows looked at for undo
UNDO_SHOWN = 5                    # undoable rows the ops line lists (newest first)
BOUND_LINE = "他下一条回「好」就确认这张草稿（{pid}）。"
DIAGNOSIS_TOOLS = frozenset({"ask_rough", "ask_check"})
NO_DIAGNOSIS_CORE = frozenset({"offer", "confirm"})
# ending a diagnosis needs its card on screen, or a goal he confirmed here; 「开始吧」 anywhere else ends nothing (ruling 2026-11-05)
NEEDS_DIAGNOSIS_HERE = frozenset({"skip_diagnosis"})
# a goal he confirmed here whose diagnosis has not ended (the diagnose turn never ran, or he left it)
def _blocks_of(rows: Any, action_id: str | None) -> str:
    """A waiting blocks draft's block names: the card shows them, the log keeps only the answer,
    and edit_goal_blocks names blocks by them (「把最后两块合并」 in the next turn)."""
    names = rows.get(action_id or "", {}).get("blocks")
    return f"（分块：{'、'.join(names)}）" if names else ""


WAITING_LINE = ("目标{title}已经确认（draft_id：{draft_id}）。他要开始学或接着学这个目标时，"
                "调 ask_rough（draft_id 用这个），摸底表或下一题由它给；他在说别的就照常回答。")


def chat_confirmable(tool: Tool) -> bool:
    """Its draft is the op his next 「好」 confirms (all but actions.NO_CHAT_CONFIRM, H4)."""
    from evowiki.assistant.tools.actions import NO_CHAT_CONFIRM
    return tool.spec.name not in NO_CHAT_CONFIRM


def inert_tools() -> list[Tool]:
    """load_skill / read_ref / offer / confirm with nothing behind them: their specs, as the
    harness registry and replay know them (a tool built here refuses everything it is asked)."""
    act = Activation(SkillLibrary(()))
    return [LoadSkillTool(lambda: act), ReadRefTool(lambda: act),
            OfferTool(dict, lambda _n: None, lambda _n: False, str, str),
            ConfirmTool(tuple, lambda _n: None, None, message_id=str)]


def _core_rules(lib: SkillLibrary, *, confirm: bool = True) -> str:
    from evowiki.assistant.loop import CONFIRM_RULE, CORE_RULES
    return CORE_RULES.format(catalog=lib.catalog(), confirm=CONFIRM_RULE if confirm else "")


@dataclass
class TurnSkills:
    lib: SkillLibrary
    act: Activation
    session_id: str
    message_id: str
    page_id: str | None
    rows: dict[str, dict[str, Any]]           # the session's cards' rows: id -> {status, …}
    undoable_rows: list[dict[str, Any]]       # the user's undoable rows, newest first
    state: Any
    ledger: Any
    # his preferences / facts waiting for his yes (wikiapp.preferences: Memory's backfill, an
    # earlier turn's card): listed in the ops line, offerable like this turn's drafts
    waiting_rows: list[dict[str, Any]] = field(default_factory=list)
    message_text: str = ""        # this message: which of the waiting rows it is about
    drafts: dict[str, ActionCard] = field(default_factory=dict)   # the waiting rows, offerable
    _tools: dict[str, Tool] = field(default_factory=dict)
    _available: tuple[str, ...] = ()
    _offered: frozenset[str] = frozenset()
    _frozen: ToolSet | None = None            # two_phase: the list the turn's requests carry
    exposure: ToolExposure = ToolExposure.mid_turn_swap
    reason: str = ""
    ops: list[PendingOp] = field(default_factory=list)       # bound this turn
    closed: list[str] = field(default_factory=list)          # ops a recorded confirm closed
    used: list[str] = field(default_factory=list)
    start_ops: tuple[PendingOp, ...] = ()                     # valid when the turn began
    # a turn of the diagnosis: nothing here waits for a 「好」, so confirm / offer / undo are not
    # offered and no op is listed (his 「确认」 started it; live 2026-11-03: the model read it as
    # a confirm of the executed goal and never diagnosed)
    diagnosing: bool = False
    waiting_goals: list[dict[str, str]] = field(default_factory=list)  # {draft_id, title}

    # ---- built once ----

    def tools(self) -> list[Tool]:
        """The S3 tools of the turn: load_skill, read_ref, offer, confirm."""
        def lookup(name: str) -> Tool | None:      # bound at bind(), read at call time
            return self._tools.get(name)
        return [LoadSkillTool(lambda: self.act), ReadRefTool(lambda: self.act),
                OfferTool(lambda: self.drafts, lookup, self.allowed_now, self.not_loaded,
                          lambda tool: f"po_{self.message_id}_{len(self.ops) + 1}"),
                ConfirmTool(self.valid_ops, lookup, self.ledger,
                            message_id=lambda: self.message_id,
                            allowed=self.allowed_now, not_loaded=self.not_loaded)]

    def bind(self, turn_tools: Sequence[Tool], *, exposure: tuple[ToolExposure, str]) -> None:
        self._tools = {t.spec.name: t for t in turn_tools}
        self._available = tuple(self._tools)
        self.exposure, self.reason = exposure
        start = self.toolset()
        self._offered = frozenset(start.names)
        self._frozen = start
        self.start_ops = self.valid_ops()

    # ---- per request ----

    def toolset(self) -> ToolSet:
        known = registry()
        extra = [n for n in self._available if n not in known]    # forged tools stay offered
        also = ([UNDO] if self.undoable_rows and not self.diagnosing else []) + extra
        core = tuple(n for n in CORE if n not in NO_DIAGNOSIS_CORE) if self.diagnosing else CORE
        available = (self._available if self.diagnosing or self.waiting_goals
                     else tuple(n for n in self._available if n not in NEEDS_DIAGNOSIS_HERE))
        return activate(self.lib, available=available, core=core, skills=self.act.skills(),
                        refs=self.act.refs(), also=also)

    def names(self) -> tuple[str, ...]:
        return self.toolset().names

    def request_names(self) -> tuple[str, ...]:
        ts = (self._frozen if self.exposure is ToolExposure.two_phase and self._frozen
              else self.toolset())
        self._offered = frozenset((*ts.names, *self.deferred_names()))
        return ts.names

    def deferred_names(self) -> tuple[str, ...]:
        """native_deferred: every other tool this turn could load, for the provider's own tool
        search (the gateway refuses them until a provider supports it: then two_phase)."""
        if self.exposure is not ToolExposure.native_deferred:
            return ()
        shown = set(self.toolset().names)
        loadable = set(self.lib.all_tools())
        return tuple(n for n in self._available if n not in shown and n in loadable)

    def allowed(self, name: str) -> bool:
        """The execution gate: offered by the request the model answered."""
        return name in self._offered

    def allowed_now(self, name: str) -> bool:
        """offer / confirm: offered in this request, or a tool of a Skill active now."""
        return name in self._offered or name in self.toolset().names

    def not_loaded(self, name: str) -> str:
        """What a refused call says: the reference or Skill that brings the tool."""
        if name in NEEDS_DIAGNOSIS_HERE:
            return "这段对话没有摸底卡在等他，摸底不能在这里结束"
        for s in self.lib.skills:
            for r in s.refs.values():
                if name in r.tools:
                    return f"{name} 还没载入：先 read_ref {r.path}"
        for s in self.lib.skills:
            if name in s.tools:
                return f"{name} 还没载入：先 load_skill {s.key}"
        return f"{name} 这一轮不能用"

    def trace_params(self) -> dict[str, Any]:
        ts = self._frozen if self.exposure is ToolExposure.two_phase and self._frozen \
            else self.toolset()
        return {"tool_exposure": str(self.exposure),
                **({"exposure_reason": self.reason} if self.reason else {}),
                "skills": dict(ts.versions), "skills_library": library_version(self.lib)}

    def prompt_version(self) -> str:
        from evowiki.assistant.loop import PROMPT_VERSION
        system = self.lib.versions(self._system_skills(), self.act.preloaded_refs)
        return "+".join([PROMPT_VERSION, "core", *(f"{k}@{v}" for k, v in system.items())])

    # ---- the prompt ----

    def _system_skills(self) -> tuple[str, ...]:
        return self.lib.ordered({*self.act.carried, *self.act.preloaded})

    def system_rules(self) -> str:
        rules = turn_rules(self.lib, self._system_skills(), bodies=self.act.preloaded_refs,
                           names=self.act.carried_refs)
        return (_core_rules(self.lib, confirm=not self.diagnosing)
                + (f"\n\n{rules}" if rules else ""))

    def ops_line(self) -> str:
        """This session's undoable rows and the ops waiting for his yes. Another session's rows are
        not listed (a 「撤销」 here must not reach them); undo's other_sessions lists them."""
        if self.diagnosing:
            return ""
        goals = "\n".join(WAITING_LINE.format(**g) for g in self.waiting_goals)
        return "\n\n".join(t for t in (goals, self._ops_lines()) if t)

    def _waiting_now(self) -> list[dict[str, Any]]:
        """The waiting rows of this conversation; another's only when this message is about waiting
        cards or says something of its title. A line in every prompt gets repeated in every answer,
        however it is worded (live 2026-11-04: 「养猫、护士、家里的狗…还没确认」 in unrelated answers)."""
        words = self.message_text
        asks = _ABOUT_WAITING.search(words) is not None
        return [r for r in self.waiting_rows
                if r.get("session_id") == self.session_id or asks
                or any(r["title"][i:i + 2] in words for i in range(len(r["title"]) - 1))][:UNDO_SHOWN]

    def _ops_lines(self) -> str:
        mine = [r for r in self.undoable_rows if r.get("session_id") == self.session_id]
        lines = [f"- {r['action_id']}「{r.get('title', '')}」已执行，可以撤销"
                 for r in mine[:UNDO_SHOWN]]
        lines += [f"- {o.pending_id}「{o.title}」等他确认{_blocks_of(self.rows, o.action_id)}" for o in self.start_ops]
        lines += [f"- {r['action_id']}「{r['title']}」来自别的对话，等他确认要不要记下（他点卡片，或你用 offer 提出后他说好）"
                  for r in self._waiting_now()]
        latest = self.state.latest
        lines += [f"- 修改提议「{p.draft.heading}」没有生效（{latest[p.proposal_id].detail}），那张卡片不能再点；"
                  "他还要改，就按现在的页面重新提议"
                  for p in self.state.proposals[-UNDO_SHOWN:]
                  if p.proposal_id in latest and latest[p.proposal_id].status == "failed"]
        said = {r["action_id"] for r in mine[:UNDO_SHOWN]} | {o.action_id for o in self.start_ops}
        said |= {r["action_id"] for r in self.waiting_rows}
        cards = [(c, CARD_STATES.get(self.rows.get(c.action_id, {}).get("status", ""))) for c in self.state.actions
                 if c.action_id not in said]
        lines += [f"- {c.action_id}「{c.title}」{word}{_blocks_of(self.rows, c.action_id)}"
                  for c, word in cards[-UNDO_SHOWN:] if word]
        return f"{OPS_HEAD}\n" + "\n".join(lines) if lines else ""

    def valid_ops(self) -> tuple[PendingOp, ...]:
        return valid_ops(self.state, self.message_id, closed=tuple(self.closed),
                         status=(lambda aid: self.rows.get(aid, {}).get("status"))
                         if self.ledger is not None else None)

    # ---- per tool result (recorded results, in order) ----

    def after_tool(self, tc: ToolCall, tool: Tool | None, result: ToolResult) -> ToolResult:
        if tool is not None:
            self.used.append(tc.name)
        if tc.name in LOADERS:
            self.act.note(result)
        self.closed.extend(p for p in result.closes if p not in self.closed)
        if result.error is not None:
            return result
        if result.offer is not None:
            self.ops.append(PendingOp.model_validate(result.offer))
        card = result.action
        if (card is not None and card.status == "proposed" and card.confirm and not result.closes
                and tool is not None and chat_confirmable(tool)):
            result = self._bind(ledger_op(self.session_id, "pending", card, self.page_id), result)
        return result

    def _bind(self, op: PendingOp, result: ToolResult) -> ToolResult:
        if all(o.pending_id != op.pending_id for o in self.ops):
            self.ops.append(op)
        return result.model_copy(update={
            "content": f"{result.content}\n{BOUND_LINE.format(pid=op.pending_id)}"})

    def unbind_proposal(self, proposal_id: str) -> None:
        """A replaced draft: his 「好」 no longer confirms it."""
        self.ops = [o for o in self.ops if not o.pending_id.endswith(proposal_id)]

    def bind_proposal(self, user_id: str, payload: dict[str, Any], page_title: str | None,
                      result: ToolResult) -> ToolResult:
        """A page-edit proposal becomes the op: its ledger row when the session store mirrored one
        at proposal time, else the proposal itself (`proposal_op`: no row until he says 好). Without a
        ledger, or when the row is not proposed, nothing is bound (the card still decides)."""
        if self.ledger is None:
            return result

        def row() -> dict[str, Any] | None:
            from evowiki.assistant.edit_executor import assistant_key
            r = self.ledger.get_by_key(assistant_key(payload["proposal_id"]))
            if r is None:
                return None
            return {"action_id": r.action_id, "status": r.status, "title": r.summary,
                    "kind": r.kind}
        try:
            got = durable.step("assistant.bind_proposal", row)
        except Exception:
            log.warning("could not bind proposal %s", payload.get("proposal_id"), exc_info=True)
            return result
        if got is None:
            title = str(payload["draft"].get("heading") or page_title or "这一节")
            return self._bind(proposal_op(self.session_id, payload, f"改写「{title}」", self.page_id), result)
        if got["status"] != "proposed":
            return result
        self.rows[got["action_id"]] = {**got, "can_undo": False}
        card = ActionCard(action_id=got["action_id"], kind=got["kind"], title=got["title"],
                          status="proposed", confirm=True)
        return self._bind(ledger_op(self.session_id, "pending", card, self.page_id), result)

    def logged(self, answer_id: str) -> dict[str, Any]:
        out: dict[str, Any] = {"skills": carry(self.act, self.used)}
        ops = list(self.ops)
        if len(self.start_ops) >= 2 and not self.closed:
            # he was asked which one (a pick no one answered, or a question in text): the ops stay
            # valid for his answer, and pending_id alone binds then. Once only: an op already
            # carried again expires with this message
            ops += [o.model_copy(update={"reasked": True}) for o in self.start_ops
                    if not o.reasked and all(n.pending_id != o.pending_id for n in ops)]
        if ops:
            out["pending_ops"] = proposed(ops, answer_id)
        return out

    # ---- two_phase ----

    def restart(self) -> Restart:
        def changed() -> bool:
            return self._frozen is None or self.toolset().names != self._frozen.names

        def refresh() -> None:
            self._frozen = self.toolset()

        def downgrade() -> None:
            # from now on two_phase, with every tool already given this turn kept
            self.exposure, self.reason = ToolExposure.two_phase, "tool_list_rejected"
            self._frozen = self.toolset()

        return Restart(loaders=LOADERS,
                       active=lambda: self.exposure is ToolExposure.two_phase,
                       changed=changed, refresh=refresh,
                       note=lambda texts: LOADED_HEAD + "\n\n".join(texts), later=LATER,
                       rejected=rejects_tool_change, downgrade=downgrade)

    # ---- construction ----

    @classmethod
    def of(cls, deps: Any, info: Any, msg: Any, state: Any, mid: str, has_attachments: bool,
           built: Sequence[Tool], resumed_tool: str | None = None) -> "TurnSkills":
        """`resumed_tool`: the paused call this turn resumes (a question the last turn ended on):
        the Skills that own it are loaded again, so the run goes on with the tools it had (the
        diagnosis's ask_rough / ask_check)."""
        from evowiki.assistant.loop import quotes_wrong_answer
        lib = library()
        ledger = getattr(deps, "ledger", None)
        rows, undoable, waiting, goal_drafts = _rows(ledger, info, state, mid, deps.data)
        waiting_goals = goal_drafts if msg.intent == "chat" and resumed_tool is None else []
        for r in waiting:               # a waiting row's status, for an op offered on it
            rows.setdefault(r["action_id"], {**r, "status": "proposed", "can_undo": False})
        open_blocks = any(c.kind == "goal_new" and c.confirm
                          and rows.get(c.action_id, {}).get("status", c.status) == "proposed"
                          for c in state.actions)
        pre = preload(learn_goal=msg.intent == "learn_goal", session_attachments=has_attachments,
                      message_attachments=bool(msg.attachments),
                      wrong_answer=quotes_wrong_answer(msg), open_blocks=open_blocks,
                      diagnose=msg.intent == "diagnose" or bool(waiting_goals),
                      conflicts=msg.intent == CONFLICTS_INTENT,
                      reteach=msg.intent == RETEACH_INTENT)
        keys = set(lib.keys())
        again = skill_of_tools(lib).get(resumed_tool, ()) if resumed_tool else ()
        act = Activation(lib, carried=tuple(k for k in state.skills if k in keys),
                         preloaded=tuple(dict.fromkeys((*pre.skills, *again))),
                         carried_refs=tuple(p for p in state.skill_refs
                                            if lib.ref(p) is not None),
                         preloaded_refs=tuple(p for p in pre.refs if lib.ref(p) is not None))
        sk = cls(lib=lib, act=act, session_id=info.session_id, message_id=mid,
                 page_id=info.page_id, rows=rows, undoable_rows=undoable, state=state,
                 ledger=ledger, waiting_rows=waiting, message_text=msg.text, waiting_goals=waiting_goals,
                 diagnosing=msg.intent == "diagnose" or resumed_tool in DIAGNOSIS_TOOLS)
        for r in waiting:               # offer(action_id) binds one of them like a fresh draft
            sk.drafts[r["action_id"]] = ActionCard(action_id=r["action_id"], kind=r["kind"],
                                                   title=r["title"], status="proposed",
                                                   confirm=True)
        return sk


def _rows(ledger: Any, info: Any, state: Any, mid: str, data: Any = None
          ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
                     list[dict[str, str]]]:
    """(the rows of the session's latest cards and of the last answer's ops as the ledger has them
    now, the user's undoable rows newest first, his preferences waiting for his yes, the goals
    confirmed here whose diagnosis is still open), one recorded step; without a ledger, as the
    session log last saw them (nothing waiting)."""
    cards = [*state.actions, *state.quiet_actions][-OPS_LIMIT:]
    op_rows = [o for o in last_ops(state, mid) if o.kind == "ledger" and o.action_id]
    if ledger is None:
        rows = {c.action_id: {"action_id": c.action_id, "status": c.status,
                              "can_undo": c.can_undo, "title": c.title, "kind": c.kind}
                for c in cards}
        return rows, [{**r, "session_id": info.session_id} for r in reversed(rows.values())
                      if r["status"] == "executed" and r["can_undo"]], [], []

    def read() -> dict[str, Any]:
        from evowiki.assistant.forgotten import prompt_title
        from evowiki.assistant.tools.actions import session_of, undoable
        from evowiki.bot.actions import can_undo
        out: dict[str, dict[str, Any]] = {}
        drafts: dict[str, str] = {}     # draft id -> the title of the goal confirmed here
        for c in cards:
            row = ledger.get(info.user_id, c.action_id)
            if row is not None:
                draft_id = (row.result or {}).get("draft_id")
                if c.kind == "goal_new" and row.status == "executed" and draft_id:
                    drafts[draft_id] = row.summary or c.title
                out[c.action_id] = {"action_id": c.action_id, "status": row.status,
                                    "can_undo": can_undo(row), "title": prompt_title(row) or c.title,
                                    "kind": row.kind,
                                    **({"blocks": [b.get("name", "") for b in row.content.get("blocks") or ()]}
                                       if row.kind == "goal_new" and row.status == "proposed" else {})}
        for o in op_rows:               # a bound page-edit proposal has no action card
            if o.action_id not in out and (row := ledger.get(info.user_id, o.action_id)):
                out[o.action_id] = {"action_id": o.action_id, "status": row.status,
                                    "can_undo": can_undo(row), "title": prompt_title(row),
                                    "kind": row.kind}
        undo = [{"action_id": r.action_id, "title": prompt_title(r), "kind": r.kind,
                 "session_id": session_of(r)} for r in undoable(ledger, info.user_id, UNDO_LIMIT)]
        from evowiki.wikiapp.preferences import FACT_KIND, PENDING_KIND
        waiting = [{"action_id": r.action_id, "title": r.summary, "kind": r.kind,
                    "session_id": session_of(r)}
                   for r in ledger.list(info.user_id, status="proposed", limit=UNDO_LIMIT)
                   if r.kind in (PENDING_KIND, FACT_KIND) and r.content.get("confirm")]
        opened = getattr(data, "open_diagnoses", None)
        goals = ([{"draft_id": d, "title": drafts[d]} for d in opened(info.user_id, list(drafts))]
                 if drafts and opened is not None else [])
        return {"rows": out, "undo": undo, "waiting": waiting, "goals": goals}
    got = durable.step("assistant.ops", read)
    return got["rows"], got["undo"], got.get("waiting", []), got.get("goals", [])
