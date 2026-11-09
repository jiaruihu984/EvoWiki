"""Reading assistant shared contracts (plan rev 3.2 §9.1). Pure data and one Protocol — no IO, no LLM.

Lanes build against these types in parallel:
  F1 (loop)     runs turns: model → tool calls → results → model, streams `AssistantEvent`s;
  F2 (tools)    implements `Tool`s;
  F3 (UIs)      speak the HTTP shapes at the bottom and render the events.
Changing a type here is a cross-lane change: do it in its own commit and tell the other lanes."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.agent.tools import (
    ActionCard,
    ActionItem,
    ProposalDraft,
    Source,
    ToolEffect,
)
from evowiki.store.artifacts import ARTIFACT_PATTERN

# ---- streamed events (SSE `event:` = type) ----

class _Ev(BaseModel):
    model_config = ConfigDict(frozen=True)


class TextDelta(_Ev):
    """Answer text. `at` (character position in the turn's answer): the client keeps
    `answer[:at]` and appends `text`, so a resent or corrected piece replaces what was shown
    from there on (spec 2026-10-15 §3); None appends."""

    type: Literal["text_delta"] = "text_delta"
    text: str
    at: int | None = Field(default=None, ge=0)


class ToolStart(_Ev):
    type: Literal["tool_start"] = "tool_start"
    call_id: str
    name: str
    effect: ToolEffect
    summary: str = ""                   # e.g. the search query, for the UI
    # the row while the call runs, one plain-Chinese line (assistant/status_line.py)
    label: str = Field(default="", max_length=200)


class StatusLine(_Ev):
    """What the assistant is doing right now, one short plain-Chinese line (assistant/status_line.py),
    sent when a tool call starts (before its `tool_start`, if any). The UI shows it under the
    question in place of the previous one until answer text arrives or that call ends; it is
    never logged or replayed."""

    type: Literal["status"] = "status"
    text: str = Field(min_length=1, max_length=200)


class ToolEnd(_Ev):
    type: Literal["tool_end"] = "tool_end"
    call_id: str
    ok: bool
    sources: tuple[Source, ...] = ()
    # the row once done (assistant/tool_rows.py): what was done, the group it folds into with the
    # calls next to it, that group's line ({n} calls, {k} different items), the result titles
    # behind a click, and the sources the answer can cite
    label: str = Field(default="", max_length=200)
    group: str = Field(default="", max_length=40)
    group_label: str = Field(default="", max_length=60)
    items: tuple[str, ...] = ()
    refs: tuple[Source, ...] = ()


class ProposalChange(BaseModel):
    """What a page-edit proposal changes, read when it was raised (an applied edit changes exactly
    this: `WikiApplier.apply` refuses once the section is no longer the one read here). Bodies as
    shown (`bot.diffs.shown_body`). `old_heading`: None for an added section."""

    old_heading: str | None = None
    old_body: str = ""
    new_body: str


class ProposalEvent(_Ev):
    type: Literal["proposal"] = "proposal"
    proposal_id: str
    draft: ProposalDraft
    change: ProposalChange | None = None


class ActionEvent(_Ev):
    """An action tool wrote a ledger row (a draft to confirm, or an action done): the card the chat
    shows. Re-sent with a new `status` when a later tool changes that row (undo)."""

    type: Literal["action"] = "action"
    action_id: str
    kind: str
    title: str
    goal_title: str | None = None       # goal_new's summarized topic, separate from the action heading
    items: tuple[ActionItem, ...] = ()
    status: str = "proposed"
    confirm: bool = False
    can_undo: bool = False
    # the waiting draft this one replaced (a goal's blocks edited in chat; "" = none): the
    # client marks only that card 已换成新的分法
    replaces: str = ""
    # the page this row changes (None = none): a change made in the chat moves that page's note too
    page_id: str | None = None


# waiting: his next message answers it (the session's pending question); answered: a message came
AskStatus = Literal["waiting", "answered"]


class AskEvent(_Ev):
    """The assistant asks the learner (a pausing tool, e.g. ask_user): the turn ends on it (`done`
    follows) and waits for nothing. His next message, typed or an option he clicked (sent as his
    message, through the normal send), resumes that turn: the server sees the session's pending
    question. Any message clears it; one that does not answer it is answered as a new request.
    `kind` names the payload's type: ask_user's is `{question, options}` (options empty: he types
    the answer)."""

    type: Literal["ask"] = "ask"
    call_id: str
    kind: str
    payload: dict[str, Any]


class CellChange(_Ev):
    cell: str
    from_band: str
    to_band: str


class Observed(_Ev):
    """After the turn: the deterministic question-as-evidence step (§9.1 point 4)."""

    type: Literal["observed"] = "observed"
    observations: int
    changes: tuple[CellChange, ...] = ()


class AttachmentRead(BaseModel):
    """How one attachment was given to the model in a turn (what the model was told in its
    「本会话的附件」 block). For debugging and tests: the UIs do not show it (user decision: no
    process text). `full`: the whole text; `toc`: the table of contents and each section's first
    line (the model reads more with read_source); `image`: the picture itself; `text_only`: only
    the text read from the picture; `unseen`: neither. `unseen_pages`: pages the model got nothing
    of (unreadable, or figure pages over the per-turn image cap)."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["full", "toc", "image", "text_only", "unseen"]
    pages_total: int | None = None
    pages_read: int | None = None
    sections: int | None = None
    tokens: int | None = None
    unseen_pages: tuple[int, ...] = ()


class AttachmentOut(BaseModel):
    """A chat attachment (a material with use=ask, or any of the user's materials) on its user
    message. `deleted`: deleted or cleaned up (the row and title stay). `expires_at`: when the
    original is cleaned up (its newest use + 30 days, as the cleanup counts); None when it never
    is (it has a non-ask use, or a node links to it) or is already gone. `unreadable_pages`: pages nothing could be read from
    (the card says 「部分页读不出 · 第 a–b 页」). `chars`: characters of its text (「约 N 万字」;
    None while reading, for an image, or deleted). `read`: that turn's AttachmentRead (None when
    the turn never got that far). `importing`: an import run of it has not ended yet (the card
    shows 正在解析 + bar while it follows that run)."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    title: str
    kind: str
    status: str
    deleted: bool
    uses: tuple[str, ...] = ()
    bytes: int = 0
    pages: int | None = None
    expires_at: datetime | None = None
    unreadable_pages: tuple[int, ...] = ()
    chars: int | None = None
    read: AttachmentRead | None = None
    importing: bool = False


class Done(_Ev):
    type: Literal["done"] = "done"
    message_id: str
    tool_calls: int
    llm_calls: int
    cost_usd: float | None = None
    attachments: tuple[AttachmentOut, ...] = ()   # this turn's attachments, as on its user message
    # after an action in the turn: the session's earlier attachments as they are now (an action may
    # have given one a use, 「刚才那份是往年题」); the UI updates every card of each source
    session_attachments: tuple[AttachmentOut, ...] = ()
    stopped: bool = False   # he stopped this answer: what was shown is all of it (assistant.loop)
    # his 「好」 decided a card of an earlier turn (no event names it): the client reads its cards again
    confirmed: bool = False


class ErrorEv(_Ev):
    type: Literal["error"] = "error"
    code: Literal["budget_exceeded", "tool_limit", "llm_unavailable", "llm_busy", "llm_timeout",
                  "bad_request", "needs_vision", "refused", "internal"]
    retryable: bool
    # for logs only; UIs show their own copy per code, except llm_busy, llm_timeout,
    # needs_vision, budget_exceeded and refused: the text to show (「请切换模型重试」: the composer has a
    # model picker, 「请切换模型重试」, 「这张图片现在看不了」; refused: why the tool a turn runs
    # itself (learn_goal's new_goal) did nothing, e.g. 「请切换模型重试」)
    detail: str = ""
    # answer text was already shown and stays: the answer broke off here (spec 2026-10-15 §4)
    interrupted: bool = False


AssistantEvent = Annotated[
    TextDelta | StatusLine | ToolStart | ToolEnd | ProposalEvent | ActionEvent | AskEvent
    | Observed | Done | ErrorEv,
    Field(discriminator="type")]


# ---- HTTP shapes ----

class SessionCreateIn(BaseModel):
    page_id: str | None = None
    note_concept_id: str | None = None


class SessionCreateOut(BaseModel):
    session_id: str


class Selection(BaseModel):
    kind: Literal["text", "wrong_answer"] = "text"
    text: str = Field(min_length=1, max_length=4000)
    heading: str | None = Field(default=None, max_length=300)
    # the section version the text was selected from, as the client showed it (0091)
    artifact_id: str | None = Field(default=None, pattern=ARTIFACT_PATTERN)


MAX_ATTACHMENTS = 50
"""Attachments on one message: a runaway guard only, not a product limit."""


RETEACH_INTENT = "reteach_section"              # 「重讲这一节」 as 标记为不会 leaves it in the composer
MessageIntent = Literal["chat", "learn_goal", "diagnose", "resolve_memory_conflicts", "reteach_section"]
CONFLICTS_INTENT = "resolve_memory_conflicts"   # 「处理」 on 个性化's 待确认的记忆 card


RoughMark = Literal["know", "partly", "dont"]


class ReplyData(BaseModel):
    """The structured answer a card sends with the learner's message that answers it: the ask_rough
    摸底 grid's marks, block id -> 掌握 (know) / 熟悉 (partly) / 不会 (dont); a block left unmarked
    is skipped. Read only when the message resumes a waiting ask_rough (else ignored)."""

    model_config = ConfigDict(extra="forbid")

    marks: dict[Annotated[str, Field(min_length=1, max_length=200)], RoughMark] = Field(
        default_factory=dict, max_length=60)


class MessageIn(BaseModel):
    """POST /assistant/sessions/{id}/messages → SSE stream of AssistantEvent. `attachments`: the
    source ids sent with this message (ready or partial; still reading → 409). Text may be empty
    when there are attachments; neither → 422.

    `intent`: "chat" (the model infers what the message wants) or "learn_goal" (学点新的 → 开始:
    the turn runs new_goal with `goal` itself and `text` as his words — the form's goal and 背景
    line, as the chat shows them —, then the model writes the tail) or "diagnose" (a goal's
    diagnosis starts in the chat: the turn loads the diagnosis tools and calls ask_rough itself on
    `draft_id`, the diagnosis of the card he clicked, never his newest open one; stage 3 T11) or "resolve_memory_conflicts" (「处理」 on 个性化's 待确认的记忆 card:
    the turn loads the 冲突 Skill and reads his open groups first) or "reteach_section" (the turn loads
    edit-wiki's page reference first, so 重讲 comes out as a draft of the quoted section). `goal` belongs to learn_goal only, which
    needs a goal and takes no selection or attachments (422 otherwise)."""

    text: str = Field(default="", max_length=4000)
    selection: Selection | None = None
    # the EvoWiki page whose note is open in the editor as he sends (any session: a free chat read back from 历史对话
    # too); None = no EvoWiki note open. The turn's 当前页 (else the session's own page).
    page_id: str | None = Field(default=None, min_length=1, max_length=200)
    attachments: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = Field(
        default=(), max_length=MAX_ATTACHMENTS)
    intent: MessageIntent = "chat"
    # no caps of their own: they are the parts of `text` (what the chat shows), whose limit is the message's
    goal: str = ""
    # intent diagnose only (needed there): the diagnosis this message starts
    draft_id: str | None = Field(default=None, min_length=1, max_length=200)
    # the answer of the card this message answers (the ask_rough grid; `text` is only his bubble)
    reply_data: ReplyData | None = None

    @model_validator(mode="after")
    def _something(self) -> "MessageIn":
        if not self.text.strip() and not self.attachments:
            raise ValueError("a message needs text or an attachment")
        if self.intent == "learn_goal":
            if not self.goal.strip():
                raise ValueError("learn_goal needs a goal")
            if self.selection is not None or self.attachments:
                raise ValueError("learn_goal takes no selection or attachments")
        elif self.goal:
            raise ValueError("goal belongs to intent learn_goal")
        if (self.intent == "diagnose") != (self.draft_id is not None):
            raise ValueError("draft_id belongs to intent diagnose, which needs one")
        return self


class ToolCallOut(BaseModel):
    """One stored tool call of a turn, replayed with GET session (read from the log, no LLM call):
    the restored turn shows its tool line as a live one did. `summary` is the live ToolStart summary
    (the query / url), else the title of the one thing the call read."""

    call_id: str
    name: str
    effect: ToolEffect
    summary: str = ""
    ok: bool
    sources: tuple[Source, ...] = ()
    # the row as the live tool_end drew it (assistant/tool_rows.py)
    label: str = ""
    group: str = ""
    group_label: str = ""
    items: tuple[str, ...] = ()
    refs: tuple[Source, ...] = ()


AnswerChip = Literal["不对", "没帮上忙", "太啰嗦", "其他"]
"""A 👎's reasons for an answer."""
QuestionChip = Literal["问过了", "我说过不会", "问得看不懂", "没必要问"]
"""A 👎's reasons for a question the turn ended on (ask_user)."""
RatingChip = Literal[AnswerChip, QuestionChip]
RATING_NOTE_MAX = 200
"""The longest note kept with a rating (characters); a longer one is clipped."""


class RatingIn(BaseModel):
    """POST /assistant/sessions/{sid}/messages/{mid}/rating, `mid` = the assistant answer's
    message id (`Done.message_id` / an assistant `MessageOut.message_id`). `value` None clears the
    rating. Chips only with "down" (422 otherwise); the note is clipped to RATING_NOTE_MAX and
    dropped with a clear."""

    model_config = ConfigDict(extra="forbid")

    value: Literal["up", "down"] | None
    chips: tuple[RatingChip, ...] = Field(default=(), max_length=4)
    note: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def _shape(self) -> "RatingIn":
        if self.chips and self.value != "down":
            raise ValueError("chips go only with a down rating")
        return self


class AskCardOut(BaseModel):
    """A question a turn ended on, as a restored turn shows it (read from the session log):
    `waiting` while no message came after it (a reload shows it answerable), else `answered`."""

    call_id: str
    kind: str
    payload: dict[str, Any]
    status: AskStatus


class RatingOut(BaseModel):
    """An answer's current rating (the latest one stored; latest wins). `value` None = cleared."""

    message_id: str                     # the assistant answer it rates
    value: Literal["up", "down"] | None
    chips: tuple[RatingChip, ...] = ()
    note: str = ""


class MessageOut(BaseModel):
    message_id: str
    role: Literal["user", "assistant"]
    text: str
    sources: tuple[Source, ...] = ()
    proposals: tuple[str, ...] = ()     # proposal ids raised in this message
    actions: tuple[str, ...] = ()       # ledger action ids the turn's tools wrote (SessionOut.actions).
    #   An undo turn's `undo` tool re-lists the id of the card it undid: that card is updated in
    #   place (same action_id, new status), not shown twice.
    selection: Selection | None = None  # user messages: the quote sent with it (replayable)
    tools: tuple[ToolCallOut, ...] = ()  # assistant messages: the turn's stored tool calls, in order
    attachments: tuple[AttachmentOut, ...] = ()  # user messages: what was attached, in order
    rating: RatingOut | None = None     # assistant messages: the current 👍 / 👎 (None: none or cleared)
    asks: tuple[AskCardOut, ...] = ()        # assistant messages: what the turn asked the learner
    draft_id: str | None = None             # intent diagnose: the diagnosis it started
    intent: MessageIntent | None = None     # user messages: how it was sent (learn_goal: 学点新的's 开始,
    #   diagnose: its 确认), as the session log stored it; None: a plain chat message
    card_at: int | None = None          # assistant messages: the offset in `text` where the turn's
    #   first action card goes (the length of the text streamed before the first action event);
    #   None: no card, or no text came before it (a replay renders text[:card_at] → cards → the rest)
    stopped: bool = False               # assistant messages: he stopped this answer (`text` may be "")


DETAIL_MAX = 500
"""The longest failure detail kept on a decision / proposal state (characters)."""

DECISION_INTERNAL = "internal"
"""`ProposalStateOut.detail` code for a failure that is not an ApplyRefused (no Chinese; UIs show
their generic copy)."""


class ProposalStateOut(BaseModel):
    """Latest decision on a proposal (failed included), returned with GET session; after a reload
    the UI restores each proposal card from it. `undone`: accepted, then the page change was
    undone in the ledger (the card says 已撤销). `action_id` / `can_undo`: the ledger row of an
    accepted edit, which the card's 撤销 undoes."""

    proposal_id: str
    status: Literal["accepted", "rejected", "failed", "undone"]
    applied: tuple[str, ...] = ()
    detail: str = ""                    # failed: the ApplyRefused reason or DECISION_INTERNAL
    action_id: str = ""
    can_undo: bool = False


class ProposalOut(BaseModel):
    """A proposal raised in a stored session: what the live `proposal` event carried (id +
    draft), so a resumed session can show the same card."""

    proposal_id: str
    draft: ProposalDraft
    change: ProposalChange | None = None


class SessionRecapOut(BaseModel):
    """The latest compaction as the learner sees it (compact@v7, assistant/summary.py): points he
    said he got (or answered right), points he said he didn't (or answered wrong) — both only
    with a checked message behind them —, what is left to do, and the accepted page edits. Plain
    items; the client words them. Never enters K."""

    understood: tuple[str, ...] = ()
    unclear: tuple[str, ...] = ()
    edits: tuple[str, ...] = ()


class SessionOut(BaseModel):
    """GET /assistant/sessions/{id}."""

    session_id: str
    page_id: str | None
    recap: SessionRecapOut | None = None   # compacted earlier turns; None = nothing to show
    messages: tuple[MessageOut, ...]
    decisions: tuple[ProposalStateOut, ...] = ()   # in the order proposals were first decided
    proposals: tuple[ProposalOut, ...] = ()        # every proposal raised, in raise order
    actions: tuple[ActionCard, ...] = ()           # every action card, in order, with the ledger's status
    gone_sources: tuple[str, ...] = ()     # materials its answers cite that are deleted or cleaned up


class SessionSummaryOut(BaseModel):
    """One row of GET /assistant/sessions (the history list)."""

    session_id: str
    title: str                          # first user message, trimmed (see `conversation_title`)
    page_id: str | None
    page_title: str                     # "" when the session has no page or the page is gone
    note_concept_id: str | None
    created_at: datetime
    last_activity_at: datetime          # newest entry of the conversation, else created_at
    message_count: int                  # user + assistant messages, as SessionOut.messages


class SessionListOut(BaseModel):
    """GET /assistant/sessions: newest activity first; empty conversations are not listed."""

    sessions: tuple[SessionSummaryOut, ...]


class ForgetRuleOut(BaseModel):
    """A teaching rule whose words came from the session (its evidence is never shown)."""

    item_id: str
    text: str
    authority: str                      # "user" | "pending" | ...


class ForgetFactOut(BaseModel):
    """A profile line the session gave."""

    key: str
    section: str
    text: str


class ForgetSessionOut(BaseModel):
    """Another session with turns that used this one's content (their answers will read as
    removed; his words stay)."""

    session_id: str
    title: str
    turns: int


class ForgetMaterialOut(BaseModel):
    """A material the session used: tick it to delete it from the library too; unticked it
    stays (only its tie to this session goes). `used_by`: where else it is used
    (session:<id> / page:<id> / goal:<id> / node:<id>)."""

    source_id: str
    title: str
    in_use_elsewhere: bool
    used_by: tuple[str, ...]


class ForgetPreviewOut(BaseModel):
    """GET /assistant/sessions/{id}/forget-preview: what the delete dialog lists. `rules` /
    `facts`: tick the ones to revoke with it; the others stay in force (`note`)."""

    rules: tuple[ForgetRuleOut, ...]
    facts: tuple[ForgetFactOut, ...]
    tainted_sessions: tuple[ForgetSessionOut, ...]
    memory_counts: dict[str, int]
    materials: tuple[ForgetMaterialOut, ...]
    note: str


class ForgetIn(BaseModel):
    """DELETE /assistant/sessions/{id}: the rules / profile lines he ticked to revoke too."""

    model_config = ConfigDict(extra="forbid")

    revoke_rule_ids: list[str] = Field(default_factory=list, max_length=200)
    revoke_fact_keys: list[str] = Field(default_factory=list, max_length=200)
    delete_source_ids: list[str] = Field(default_factory=list, max_length=200)


class ForgetPendingOut(BaseModel):
    """A session whose delete was asked and has not finished (retried automatically); it is no
    longer listed in GET /assistant/sessions and its content is not shown."""

    session_id: str
    state: Literal["requested", "deleting", "failed"]
    requested_at: datetime


class ForgetPendingListOut(BaseModel):
    """GET /assistant/forget-requests: the 「删除中」 sessions, oldest request first."""

    sessions: tuple[ForgetPendingOut, ...]


class ForgetOut(BaseModel):
    """DELETE result: 200 `committed`; 202 `failed` = recorded, finished by a retry (`detail`)."""

    state: Literal["committed", "failed"]
    detail: str = ""


# ---- possibly conflicting memories (memory/conflicts.py; 个性化 「可能矛盾的记忆」) ----

MemoryConflictCategory = Literal["rule", "profile", "decision"]
# 聊天里说的 / 从对话里整理的 / 规则页手写的 / 档案里写的
MemoryConflictSource = Literal["chat", "consolidation", "rule_page", "profile"]


class MemoryConflictItemOut(BaseModel):
    """One entry of his memory library (what is in force for him): a rule of his (`rule`), a
    line he wrote in his profile (`profile`), a decision from a chat (`decision`).
    `id`: the spec item id / profile line key / memory item id.
    `where` / `where_kind`: what it is limited to — a rule's domain (`domain`, the bare name: 数学),
    a decision's page (`page`, its title), goal or concept (`goal` / `concept`, the id); both None
    for everything (a profile line too; its column is `section`). `origin_ref`:
    `<session>/<message>` of his words when known (`deleted:…` once that session was deleted:
    `origin_deleted`); `origin_title`: that conversation's title (None when deleted or unknown);
    `quote`: his words (or 「你说过「…」」)."""

    model_config = ConfigDict(frozen=True)

    id: str
    category: MemoryConflictCategory
    text: str
    date: datetime | None = None
    source: MemoryConflictSource
    where: str | None = None
    where_kind: Literal["domain", "page", "goal", "concept"] | None = None
    section: str | None = None
    origin_ref: str | None = None
    origin_title: str | None = None
    origin_deleted: bool = False
    quote: str | None = None


class MemoryConflictOut(BaseModel):
    conflict_id: str
    why: str
    detected_at: datetime
    items: list[MemoryConflictItemOut]


class MemoryConflictListOut(BaseModel):
    """GET /assistant/memory/conflicts: the open groups (all of whose entries still exist)."""

    conflicts: list[MemoryConflictOut]


class MemoryConflictResolveIn(BaseModel):
    """POST /assistant/memory/conflicts/{id}: `keep` one entry (「保留这条」: the others go out of
    force, each its own normal way; undoable from the activity log), or `keep_both` (「两条都保留」:
    nothing changes; the group is not raised again)."""

    model_config = ConfigDict(extra="forbid")

    keep: str | None = Field(default=None, min_length=1, max_length=200)
    keep_both: bool = False

    @model_validator(mode="after")
    def _one(self) -> "MemoryConflictResolveIn":
        if (self.keep is None) == (not self.keep_both):
            raise ValueError("give exactly one of keep / keep_both")
        return self


class MemoryConflictResolveOut(BaseModel):
    """`action_id`: for `keep`, the activity-log row (kind memory_conflict_resolve) whose undo
    puts the others back and reopens the group (the usual action undo); None for keep_both."""

    conflict_id: str
    status: Literal["kept_both", "resolved"]
    action_id: str | None = None


class ProposalDecisionOut(BaseModel):
    """POST /assistant/proposals/{id}/accept|reject. On accept the page change goes through the
    Phase 1 minimal patch; `applied` lists the section ids written; `action_id` / `can_undo`: its
    ledger row, which the card's 撤销 undoes."""

    proposal_id: str
    status: Literal["accepted", "rejected", "already_decided", "failed"]
    applied: tuple[str, ...] = ()
    detail: str = ""
    action_id: str = ""
    can_undo: bool = False


class KnownItemOut(BaseModel):
    """GET /assistant/actions/{id}/confirm: the one confirm question after 「这个我早会了」 (the key
    is not sent)."""

    stem: str
    options: tuple[str, ...]


class KnownAnswerOut(BaseModel):
    correct: bool
    correct_index: int
    undone: bool
    message: str


MessageOut.model_rebuild()
SessionOut.model_rebuild()


class LevelOut(BaseModel):
    """A concrete reasoning option supported by this model."""

    reasoning: str
    name: str


class ModelOut(BaseModel):
    """One model of the composer's picker (runtime.models): `name` is shown in the picker only;
    `reason` (when not `available`) behind its ?."""

    id: str
    name: str
    provider: str
    provider_name: str
    available: bool
    reason: str = ""
    levels: tuple[LevelOut, ...] = ()   # the thinking levels it has (the picker's second section)


class ModelsOut(BaseModel):
    """GET /assistant/models: every model the server knows, the one this user's turns use and their
    thinking level (`tier`: the user's assistant tier, the same setting as the usage tiers)."""

    models: tuple[ModelOut, ...]
    current: str | None          # None: no model can be routed at all
    reasoning: str | None


class ModelIn(BaseModel):
    """A pick: the model, the thinking level, or both (at least one)."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, min_length=1, max_length=256)
    reasoning: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def _one(self) -> "ModelIn":
        if self.model is None and self.reasoning is None:
            raise ValueError("pick a model or a reasoning level")
        return self
