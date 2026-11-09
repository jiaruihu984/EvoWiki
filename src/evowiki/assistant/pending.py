"""Pending ops: what a short 「好」 may confirm (design B3.3, Q5/Q12; stage 3 T5).

The assistant proposes an op in its answer; the learner's next message may confirm it. An op exists
only when the model binds it explicitly in that turn (a draft card on its own is never an op: a
blocks card whose tail asks a question is not confirmed by 「可以」):

- a draft tool's proposed draft row becomes a `ledger` op in the same call, the model is not asked
  (assistant.turn_skills; live runs 2026-10-28 and 2026-11-04: neither DeepSeek's offer nor Haiku's
  switch came, so 「好」 had nothing to confirm). Not the goal-blocks tools
  (actions.NO_CHAT_CONFIRM, H4). A page-edit proposal bound this way gets its ledger row
  (the one the session store mirrors, or a new `ask` row) and that row is the op;
- `offer(tool, args, title)`: one direct-tier call of a tool the turn may run now (offered in this
  request, or of an active Skill), its args checked against its schema and dry-run build without
  running it, becomes a `call` op; its title carries the key args;
- `offer(action_id)`: one of his preferences / facts waiting for his yes from before this turn
  (wikiapp.preferences, listed in the ops line) becomes a `ledger` op. A draft this turn writes is
  never bound here: its own call does that.

Both are logged with the turn's answer (`assistant` entry `pending_ops`). An op is valid only in the
learner's very next message (`valid_ops`): proposed in the answer right before it, not closed in this
turn (closed = a recorded `confirm` result names it: `ToolResult.closes`), and a ledger op's row is
still `proposed`. When an answer could not tell which of several ops he meant (it asked him), the
still-valid ops are carried again on that answer (`reasked`), so 「第一个」 resolves next turn.

`confirm(pending_id, asked)`:
- `asked` is his words of THIS message, the grounding rule (`grounding.quoted_now`: his whole
  message, or a piece of at least four characters), and not opening with a negation (「不好」
  「不好意思」);
- exactly one valid op → it runs, provided `pending_id` names it (an irreversible one too: the id
  binds it, Q12);
- no valid op → refused, the model asks again;
- two or more → nothing is guessed: the call pauses with one option per op (the existing ask card,
  `{question, options}`; the turn ends on it, his next message resumes it); that message binds the
  choice only when it is an option's exact text (an explicit pick, irreversible ops included).
  「都不是」 / anything else → nothing runs. If the ops were carried again after such a question,
  `pending_id` alone binds (his answer to it).

A ledger op runs through `ledger.approve` (the card click's path) and the row's result records
`confirmed_by = [proposed_in, {message_id: this, role: "user", quote: asked}]`; a call op runs the
tool's own `run` with the stored arguments (their hash, the tool's availability and the dry run
re-checked). These rules are code: no Skill, harness key or tool text changes them."""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.agent import durable
from evowiki.agent.tool_helpers import fail, run_guarded, spec
from evowiki.agent.tools import ActionCard, Pause, Reply, Tool, ToolContext, ToolResult
from evowiki.assistant.tools.grounding import negated, quoted_now
from evowiki.memory.contracts import EvidenceRef, MemoryScope

OFFER = "offer"
CONFIRM = "confirm"
PICK_KIND = "confirm_pick"
NONE_OF_THEM = "都不是"
NO_PENDING = "现在没有等他确认的操作，先问清楚他要做什么"
NOT_QUOTED = "asked 要照抄他这一条消息里表示同意的话（整条消息，或至少 4 个字）"
NOT_A_YES = "他这句话不是同意（以否定开头），什么都没做"
ARGS_MAX = 120                         # chars of the key args a call op's title carries
PAGE_EDIT = "assistant_edit"           # edit_executor.KIND: a page-edit proposal's ledger row


class PendingOp(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pending_id: str = Field(pattern=r"^po_")
    session_id: str
    proposed_in: EvidenceRef              # role="assistant", the proposing answer
    kind: Literal["ledger", "call", "proposal"]
    action_id: str | None = None          # kind=ledger: the draft row (its card)
    tool: str | None = None               # kind=call: a direct-tier tool ...
    args: dict[str, Any] | None = None    # ... and its checked arguments; kind=proposal: the proposal
    args_hash: str
    title: str
    scope: MemoryScope = MemoryScope(scope_type="global")
    # carried again on an answer that asked him which one he meant (pending_id alone binds then)
    reasked: bool = False
    # the learner message after which it is invalid (the next one); None until he sends it
    expires_after: str | None = None


def args_hash(tool: str, args: Mapping[str, Any]) -> str:
    blob = json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _scope(page_id: str | None) -> MemoryScope:
    return MemoryScope(scope_type="page" if page_id else "global", scope_id=page_id)


def ledger_op(session_id: str, message_id: str, card: ActionCard,
              page_id: str | None = None) -> PendingOp:
    """The op of a draft row the model bound in the turn of `message_id` (its answer id is filled
    in when the answer is logged: `proposed`)."""
    return PendingOp(pending_id=f"po_{card.action_id}", session_id=session_id,
                     proposed_in=EvidenceRef(message_id=message_id, session_id=session_id,
                                             role="assistant"),
                     kind="ledger", action_id=card.action_id,
                     args_hash=args_hash(card.kind, {"action_id": card.action_id}),
                     title=card.title, scope=_scope(page_id))


def proposal_op(session_id: str, payload: Mapping[str, Any], title: str,
                page_id: str | None = None) -> PendingOp:
    """A page-edit proposal with no ledger row (a user whose level does not mirror them): 「好」 writes
    the row then, as a click on its card does, so a draft nobody decides leaves nothing behind."""
    pid = str(payload["proposal_id"])
    return PendingOp(pending_id=f"po_{pid}", session_id=session_id,
                     proposed_in=EvidenceRef(message_id="pending", session_id=session_id, role="assistant"),
                     kind="proposal", args=dict(payload), args_hash=args_hash(PAGE_EDIT, {"proposal_id": pid}),
                     title=title, scope=_scope(page_id))


def proposed(ops: Sequence[PendingOp], answer_id: str) -> list[dict[str, Any]]:
    """The turn's ops as its assistant entry logs them: proposed in that answer."""
    return [o.model_copy(update={"proposed_in": EvidenceRef(
        message_id=answer_id, session_id=o.session_id, role="assistant")})
            .model_dump(mode="json") for o in ops]


class LedgerPort(Protocol):
    """The part of `bot.actions.ActionLedger` confirm needs."""

    def get(self, user_id: str, action_id: str) -> Any: ...

    def approve(self, user_id: str, action_id: str) -> Any: ...

    def add_result_keys(self, user_id: str, action_id: str, keys: dict) -> bool: ...


def last_ops(state: Any, current_mid: str) -> tuple[PendingOp, ...]:
    """The ops of the answer right before `current_mid` (the session's last answered turn, when
    nothing came between it and this message)."""
    turns = [t for t in state.turns if t.message_id != current_mid]
    if not turns or turns[-1].answer is None:
        return ()
    out: list[PendingOp] = []
    for raw in turns[-1].pending_ops:
        try:
            out.append(PendingOp.model_validate(raw))
        except ValueError:            # a malformed logged op is no op (never guessed back)
            continue
    return tuple(out)


def valid_ops(state: Any, current_mid: str, *, closed: Sequence[str] = (),
              status: Callable[[str], str | None] | None = None) -> tuple[PendingOp, ...]:
    """The ops this message may confirm. `status(action_id)`: the row's ledger status now (None:
    unknown row; without `status` a ledger op cannot be checked and is not valid: fail closed)."""
    out = []
    for op in last_ops(state, current_mid):
        if op.pending_id in closed or op.expires_after not in (None, current_mid):
            continue
        if op.kind == "proposal" and str((op.args or {}).get("proposal_id")) in state.latest:
            continue
        if op.kind == "ledger" and (status is None or op.action_id is None
                                    or status(op.action_id) != "proposed"):
            continue
        out.append(op)
    return tuple(out)


Lookup = Callable[[str], Tool | None]
Allowed = Callable[[str], bool]


def _inner(tool: Any) -> Any:
    """The tool under a `Grounded` wrapper (the op's authorization is the confirm, not `asked`)."""
    return getattr(tool, "tool", tool)


def check_call(tool: Tool, args: Mapping[str, Any], ctx: ToolContext) -> str | None:
    """Why `args` would not run on `tool` now (None: they would): its schema, and for a ledger
    action tool its dry-run build against the current wiki (nothing is written)."""
    inner = _inner(tool)
    dry = getattr(inner, "dry_run", None)
    if dry is not None:
        return dry(dict(args), ctx)
    model = getattr(inner, "args_model", None)
    if model is not None:
        try:
            model.model_validate(dict(args))
        except ValueError as e:
            return f"参数不合法：{e}"
        return None
    try:
        import jsonschema
        jsonschema.validate(dict(args), dict(inner.spec.parameters))
    except Exception as e:  # noqa: BLE001 - any schema failure refuses the offer
        return f"参数不合法：{str(e).splitlines()[0]}"
    return None


def _key_args(args: Mapping[str, Any]) -> str:
    text = "，".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
    return text if len(text) <= ARGS_MAX else text[:ARGS_MAX] + "…"


# ---- offer ----

class OfferArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str | None = Field(default=None, max_length=80,
                                  description="或者：待确认里一条等他点头的偏好的 id")
    tool: str | None = Field(default=None, max_length=40,
                             description="要等他同意的那个直接生效的工具")
    args: dict[str, Any] | None = Field(default=None, description="那个工具的参数（不带 asked）")
    title: str | None = Field(default=None, max_length=120, description="一句话：这个操作做什么")

    @model_validator(mode="after")
    def _one_form(self) -> "OfferArgs":
        call = self.tool is not None or self.args is not None
        if (self.action_id is None) == (not call):
            raise ValueError("给 tool + args + title，或者给 action_id，二选一")
        if call and (self.tool is None or self.args is None or not (self.title or "").strip()):
            raise ValueError("tool、args、title 都要给")
        return self


class OfferTool:
    """`offer(tool, args, title)` / `offer(action_id)`: records one pending op (nothing runs). A
    draft card this turn writes is bound by its own tool's `confirm_in_chat`, not here.
    `waiting()`: his rows waiting for a yes, by action id; `lookup(name)`: this turn's tool;
    `allowed(name)`: the tool may run now; `not_loaded(name)`: what to load first."""

    def __init__(self, waiting: Callable[[], Mapping[str, ActionCard]], lookup: Lookup,
                 allowed: Allowed, not_loaded: Callable[[str], str],
                 new_op: Callable[[str], str]):
        self._waiting, self._lookup, self._allowed = waiting, lookup, allowed
        self._not_loaded, self._new_op = not_loaded, new_op
        self.spec = spec(OFFER, "你提议一个会直接生效的操作、要等他回「好」时用：给工具和参数。现在什么都"
                         "不做，这一条里他还问了别的就照常答完。草稿卡片不用这个（出草稿时已经记下）。", OfferArgs,
                         "propose", "read", acts=True, tier="draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(OfferArgs, args, lambda a: self._run(a, ctx))

    def _run(self, a: OfferArgs, ctx: ToolContext) -> ToolResult:
        if a.action_id is not None:
            card = self._waiting().get(a.action_id)
            if card is None:
                return fail(f"{a.action_id} 不是等他点头的偏好；草稿不用 offer，他回「好」就用 confirm")
            op = ledger_op(ctx.session_id, "pending", card, ctx.page_id)
            return ToolResult(content=f"已记下等他确认的操作（{op.pending_id}）：{op.title}。他回「好」后用 "
                                      "confirm 执行；他同意前什么都没做。",
                              offer=op.model_dump(mode="json"), summary=op.title[:200])
        name = a.tool or ""
        tool = self._lookup(name)
        if tool is None or name in (OFFER, CONFIRM):
            return fail(f"这一轮没有 {name} 这个工具")
        if not self._allowed(name):
            return fail(self._not_loaded(name))
        if tool.spec.tier != "direct":
            return fail(f"{name} 不是直接生效的工具：草稿不用 offer，读工具直接用")
        why = check_call(tool, a.args or {}, ctx)
        if why is not None:
            return fail(why)
        args = dict(a.args or {})
        title = " ".join((a.title or "").split())
        op = PendingOp(pending_id=self._new_op(name), session_id=ctx.session_id,
                       proposed_in=EvidenceRef(message_id="pending",
                                               session_id=ctx.session_id, role="assistant"),
                       kind="call", tool=name, args=args, args_hash=args_hash(name, args),
                       title=f"{title}（{_key_args(args)}）" if args else title,
                       scope=_scope(ctx.page_id))
        return ToolResult(content=f"已记下等他确认的操作（{op.pending_id}）：{op.title}。他回「好」后用 "
                                  "confirm 执行；他同意前什么都没做。",
                          offer=op.model_dump(mode="json"), summary=op.title[:200])


# ---- confirm ----

class ConfirmArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending_id: str = Field(min_length=4, max_length=80, description="上一轮提出的那个操作的 id（po_…）")
    asked: str = Field(min_length=1, max_length=400, description="他这一条消息里表示同意的原话（照抄）")


class PickPayload(BaseModel):
    """`Pause.payload` of kind `confirm_pick`: the ask card's shape (`{question, options}`)."""

    model_config = ConfigDict(frozen=True)

    question: str
    options: tuple[str, ...]
    pending_ids: tuple[str, ...]          # option i -> op (the last option: none of them)


class ConfirmTool:
    """`confirm(pending_id, asked)`: see the module doc. `ops()` -> the valid ops now (this turn's
    closed ones excluded); `lookup` / `allowed` / `not_loaded`: this turn's tools; `ledger`: the
    action ledger (None: no ledger op can run)."""

    def __init__(self, ops: Callable[[], tuple[PendingOp, ...]], lookup: Lookup,
                 ledger: LedgerPort | None, *, message_id: Callable[[], str],
                 allowed: Allowed = lambda _n: True,
                 not_loaded: Callable[[str], str] = lambda n: f"{n} 还没载入"):
        self._ops, self._lookup, self._ledger, self._mid = ops, lookup, ledger, message_id
        self._allowed, self._not_loaded = allowed, not_loaded
        self.spec = spec(CONFIRM, "他只回了「好」「就这样」「可以」这类同意时用：执行上一轮提出、等他确认的"
                         "那一个操作（pending_id 在本轮开头的待确认列表里）。",
                         ConfirmArgs, "propose", "action", acts=True, pauses=True,
                         tier="direct")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(ConfirmArgs, args, lambda a: self._run(a, ctx))

    def _run(self, a: ConfirmArgs, ctx: ToolContext) -> ToolResult:
        if not quoted_now(a.asked, ctx, confirming=True):
            return fail(NOT_QUOTED)
        if negated(a.asked):
            return fail(NOT_A_YES)
        ops = self._ops()
        if not ops:
            return fail(NO_PENDING)
        named = next((o for o in ops if o.pending_id == a.pending_id), None)
        if len(ops) == 1:
            if named is None:
                return fail(f"等他确认的是 {ops[0].pending_id}（{ops[0].title}），不是 {a.pending_id}")
            return self._execute(named, a.asked, ctx)
        if named is not None and all(o.reasked for o in ops):
            return self._execute(named, a.asked, ctx)        # his answer to "which one?"
        options = (*(f"{i}. {o.title}" for i, o in enumerate(ops, 1)), NONE_OF_THEM)
        payload = PickPayload(question="你是要确认哪一个？", options=options,
                              pending_ids=tuple(o.pending_id for o in ops))
        return ToolResult(content="已经问他是哪一个，正在等他回答。",
                          pause=Pause.of(PICK_KIND, payload), summary="确认哪一个")

    def resume(self, args: dict[str, Any], ctx: ToolContext, pause: Pause,
               reply: Reply) -> ToolResult:
        payload = PickPayload.model_validate(pause.payload)
        text = reply.text.strip()
        idx = payload.options.index(text) if text in payload.options else None
        chosen = payload.pending_ids[idx] if idx is not None and idx < len(payload.pending_ids) else None
        if chosen is None:
            return ToolResult(content=f"他没有选其中一个（他说：「{text}」），什么都没做；"
                                      "回应他说的话，不要再问是哪一个。")
        op = next((o for o in self._ops() if o.pending_id == chosen), None)
        if op is None:
            return fail("他选的那个操作已经不在等待确认了，什么都没做")
        return self._execute(op, reply.text.strip() or op.title, ctx)

    def _evidence(self, op: PendingOp, quote: str) -> list[dict[str, Any]]:
        return [op.proposed_in.model_dump(mode="json", exclude_none=True),
                EvidenceRef(message_id=self._mid(), session_id=op.session_id, role="user",
                            quote=quote).model_dump(mode="json", exclude_none=True)]

    def _execute(self, op: PendingOp, quote: str, ctx: ToolContext) -> ToolResult:
        closes = (op.pending_id,)
        if op.kind == "proposal":
            if self._ledger is None:
                return fail("现在确认不了这张卡片，请他在卡片上点确认")
            from evowiki.assistant.edit_executor import proposal_action
            row = self._ledger.propose(ctx.user_id, level="ask", **proposal_action(
                op.args or {}, op.session_id, page_title=None))
            op = op.model_copy(update={"kind": "ledger", "action_id": row.action_id})
        if op.kind == "ledger":
            from evowiki.assistant.tools.actions import card_of
            if self._ledger is None or op.action_id is None:
                return fail("现在确认不了这张卡片，请他在卡片上点确认")
            from evowiki.bot.actions import NotDecidable
            try:
                # as a card click does it: off the turn's DBOS step, so an executor that starts
                # work on approve (a split's page writes, an import) can start its workflows
                led, aid = self._ledger, op.action_id
                row = durable.outside_dbos(lambda: led.approve(ctx.user_id, aid))
            except (NotDecidable, LookupError) as e:
                return fail(f"没确认成：{e}").model_copy(update={"closes": closes})
            if row.status == "failed":
                return fail(row.detail or "没改成").model_copy(update={"closes": closes})
            self._ledger.add_result_keys(ctx.user_id, op.action_id,
                                         {"confirmed_by": self._evidence(op, quote)})
            if row.kind == PAGE_EDIT:       # its proposal card is the card (no action card)
                done = "已改好页面" if row.status == "executed" else f"状态 {row.status}"
                return ToolResult(content=f"他确认了，{done}（{op.action_id}）：{row.summary}。",
                                  summary=row.summary[:200], closes=closes)
            card = card_of(self._ledger.get(ctx.user_id, op.action_id) or row)
            done = "已执行" if row.status == "executed" else f"状态 {row.status}"
            note = (row.result or {}).get("note")
            told = f"但{note}，要照实告诉用户。" if note else ""
            return ToolResult(content=f"他确认了，{done}（{op.action_id}）：{card.title}。{told}",
                              action=card, summary=card.title[:200], closes=closes)
        name = op.tool or ""
        tool = self._lookup(name)
        if tool is None or op.args is None:
            return fail(f"这一轮没有 {name} 这个工具，什么都没做")
        if not self._allowed(name):
            return fail(self._not_loaded(name))
        if args_hash(name, op.args) != op.args_hash:
            return fail("这个操作的参数和提出时不一样了，什么都没做；重新提出")
        why = check_call(tool, op.args, ctx)
        if why is not None:
            return fail(f"现在做不了：{why}")
        result = _inner(tool).run(dict(op.args), ctx)
        if result.error is not None:
            return result.model_copy(update={"closes": closes})
        return result.model_copy(update={"content": f"他确认了：{op.title}。{result.content}",
                                         "closes": closes})
