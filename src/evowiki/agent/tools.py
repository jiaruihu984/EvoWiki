"""The tool contract of the shared model-and-tools loop (agent.loop): what a tool declares
(`ToolSpec`), what it gets per turn (`ToolContext`) and what it returns (`ToolResult`, with the
payloads only a tool result can create: sources, a proposal, a ledger card). Pure data
and one Protocol: no IO, no LLM. The reading assistant, the deep writer and the
harness build their tools against these types (moved from assistant.contracts, architecture
audit stage 2a)."""

from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.llm.chat import ImagePart

ToolEffect = Literal["read", "network", "propose"]
"""Permission by side effect (§9.1 point 3): read runs automatically; network runs automatically
and is shown to the user; propose never changes anything — it creates a `Proposal` the user
accepts or rejects."""


ToolCategory = Literal["read", "draft", "action"]
"""What the durable loop does with a tool (B1b). `read` reads something outside the workflow: a
step, its result is recorded. `draft` changes only the in-memory draft and is a pure function of
its arguments and the draft: it runs in the workflow body and is replayed (anything external it
calls must itself be durable, e.g. render_check's checker). `action` proposes or commits: a step,
its idempotency key is `durable.step_key()`."""

_CATEGORY_OF_EFFECT = {"read": "read", "network": "read", "propose": "action"}

ToolTier = Literal["read", "draft", "direct", "observe"]
"""What a call may do without the learner (design B3, stage 3): `read` reads (no grounds needed);
`draft` writes a draft or a suggestion card only (his click on it is the authorization); `direct`
takes effect at once and runs only on his explicit request in this message or his confirmation of
one pending op (`Grounded`, a self-checking tool); `observe` writes bounded objective evidence.
Set by code only: a Skill, a harness key or a tool text never changes it."""

_TIER_OF_EFFECT = {"read": "read", "network": "read", "propose": "draft"}


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    description: str = Field(min_length=1, max_length=1000)
    parameters: dict[str, Any]          # JSON Schema of the single argument object
    effect: ToolEffect
    category: ToolCategory
    acts: bool = False                  # an action tool: its result may carry a ledger card
    #                                     (`ToolResult.action`); the turn gets ACTION_RULES
    timeout_s: float | None = None      # declared for B1c, not enforced yet
    parallel: bool = False              # declared for B3, not enforced yet
    # may ask the learner (`ToolResult.pause`, a `PausingTool`): the run ends on the question and
    # his next message resumes it (agent.loop `Resume`)
    pauses: bool = False
    # the permission tier (`ToolTier`); None derives it from `effect` (read / network -> read,
    # propose -> draft); a tool that takes effect at once says "direct" itself
    tier: ToolTier = "read"

    @model_validator(mode="before")
    @classmethod
    def _derive_category(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("category") is None:
            effect = data.get("effect")
            if effect in _CATEGORY_OF_EFFECT:
                data = {**data, "category": _CATEGORY_OF_EFFECT[effect]}
        if isinstance(data, dict) and data.get("tier") is None:
            effect = data.get("effect")
            if effect in _TIER_OF_EFFECT:
                data = {**data, "tier": _TIER_OF_EFFECT[effect]}
        return data


class Source(BaseModel):
    """Where a piece of information came from. Exactly one of url / page_id / segment_id."""

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=300)
    url: str | None = None
    page_id: str | None = None
    segment_id: str | None = None


class ProposalDraft(BaseModel):
    """A change only the user can apply (effect="propose"). `kind` maps to an existing flow:
    add_section / rewrite_section → Phase 1 minimal patch on `page_id`."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["add_section", "rewrite_section"]
    page_id: str
    section_id: str | None = None       # rewrite_section, or the section to insert after
    heading: str = Field(min_length=1, max_length=120)
    cells: tuple[str, ...] = ()
    text: str = Field(min_length=1, max_length=4000)
    why: str = Field(min_length=1, max_length=300)
    cue: str = Field(default="", max_length=60)      # the section's review card, written with the text
    answer: str = Field(default="", max_length=200)


class ActionItem(BaseModel):
    """One line of an action card: a page (or path row) the action lists. `key` is the draft
    handle the card edits by (a path row key, a page id, or a draft key like "p1")."""

    model_config = ConfigDict(frozen=True)

    key: str
    title: str
    page_id: str | None = None
    covers: str | None = None
    n: int | None = None                # the page's position on its path (a made page of a done card)
    level: str | None = None            # a blocks draft row: the block's level word (基础 / 核心 / 进阶 / 拓展)


class ActionCard(BaseModel):
    """The ledger row an action tool wrote, as the chat card shows it (spec 2026-10-15 §2). The
    card and 参数's row are the SAME ledger record: `action_id`. `confirm`: a draft the learner
    confirms (确认 / 取消); `status` is the ledger's word at the time the card was made."""

    model_config = ConfigDict(frozen=True)

    action_id: str
    kind: str
    title: str
    goal_title: str | None = None       # goal_new's summarized topic, separate from the action heading
    items: tuple[ActionItem, ...] = ()
    status: str = "proposed"
    confirm: bool = False
    can_undo: bool = False
    detail: str = ""
    # the waiting draft this one took the place of (a goal's blocks edited in chat: that row was
    # rejected "replaced"); "" = none — the client marks only that card replaced
    replaces: str = ""
    # what an executed goal_new opened (its draft / map ids, as ActionOut.result): a reloaded chat
    # rebuilds the diagnosis under the card from them (the same ids ActionOut.result gives); {}
    # before execution
    result: dict[str, str | bool | None] = {}
    # where the row's change is (ActionOut.page_id / section_id): a reloaded chat's 查看详情 opens that page there
    page_id: str | None = None
    section_id: str | None = None


class Pause(BaseModel):
    """A tool asks the learner something (agent.loop): the run ends on the question, and his next
    message resumes it (`agent.loop.Resume`), handed to the tool's `resume`, whose result is the
    call's result. `kind` names the payload's type, which the UIs render by (ask_user:
    `{question, options}`); `payload` is the tool's own typed model as JSON (`Pause.of`)."""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    payload: dict[str, Any]

    @classmethod
    def of(cls, kind: str, payload: BaseModel) -> "Pause":
        return cls(kind=kind, payload=payload.model_dump(mode="json"))


REPLY_MAX = 4000


class Reply(BaseModel):
    """The learner's next message after a `Pause`: what he typed, or the option he clicked (sent
    as his message). It may answer the question or say something else. `data`: the structured
    answer a card sent with that message (`MessageIn.reply_data`: the ask_rough grid's marks),
    given only to the call it resumes; {} for every other reply."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(max_length=REPLY_MAX)
    data: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """What the model sees (`content`, kept short) plus what the UI shows. `action`: the ledger
    row an action tool wrote (the loop turns it into an `action` event), a fact only a tool result
    can create."""

    model_config = ConfigDict(frozen=True)

    content: str = Field(max_length=12000)
    # pictures the model sees with the result (a provider without vision gets the words alone)
    images: tuple[ImagePart, ...] = ()
    sources: tuple[Source, ...] = ()
    proposal: ProposalDraft | None = None
    action: ActionCard | None = None
    error: str | None = None            # tool failed; content explains it to the model
    # what the tool line names (a goal, a point, a page): 读了「<summary>」的学习路径; "" = the verb alone
    summary: str = Field(default="", max_length=200)
    # an action result's words to the learner that go BEFORE its card, as the turn's own text (the
    # assistant loop commits it as a text segment right before the action event; a blocks draft:
    # the blocks call's framing + caveat); "" = none
    lead: str = Field(default="", max_length=4000)
    # a pausing tool (`ToolSpec.pauses`) asks the learner: the run ends on the question, and the
    # tool's `resume` makes the call's real result from his next message (`content` here is never
    # shown to the model)
    pause: Pause | None = None
    # facts for the caller and the turn's log, never shown to the model (the memory tools: the
    # hits' ids and `sources_sessions`, the sessions the result quoted)
    data: dict[str, Any] = Field(default_factory=dict)
    # what a successful load_skill / read_ref call made available (Skill keys and reference
    # paths, agent.skills): part of the recorded result, so a replay recomputes the same tool sets
    loaded: tuple[str, ...] = ()
    # a pending op the assistant's `offer` recorded (assistant.pending.PendingOp as JSON): the turn
    # logs it with its answer, so the learner's next 「好」 can confirm exactly that op
    offer: dict[str, Any] | None = None
    # the pending ops a `confirm` call ran (or used up): part of the recorded result, so a
    # recovered turn knows them closed without re-running the call
    closes: tuple[str, ...] = ()
    # what an observe-tier tool wrote into K (`record_evidence`): {observations: n, changes:
    # [{cell, from_band, to_band}]}; the turn logs one `observed` entry and event from them
    observed: dict[str, Any] | None = None


class ToolContext(BaseModel):
    """Per-turn facts a tool may use. The selection is present only when the user clicked "ask"
    with it (7.1 D-E: vault text is never sent automatically)."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    session_id: str
    # this turn's user message id (what an observe-tier tool's evidence refs are built from)
    message_id: str | None = None
    # the call a pausing tool's `resume` answers (agent.loop sets it there; None elsewhere)
    call_id: str | None = None
    page_id: str | None = None
    note_concept_id: str | None = None
    selection_text: str | None = None
    selection_heading: str | None = None
    # the learner's own typed messages of the session, this turn's first (what a quote of his words
    # must be found in: remember_preference / report_problem)
    user_words: tuple[str, ...] = ()
    # this message only (what an `asked` quote must come from: assistant.tools.grounding); a turn
    # that resumes a question (ask_user) also has the message it asked in
    current_words: tuple[str, ...] = ()
    # his earlier messages of the session as (message id, text), newest first (with `message_id`:
    # the evidence a remembered preference cites, wikiapp.preferences)
    past_messages: tuple[tuple[str, str], ...] = ()
    # whether an op the assistant proposed waits for this message's yes (assistant.pending):
    # then a short whole-message quote grounds only `confirm`. None: nothing waits
    has_pending: Callable[[], bool] | None = Field(default=None, exclude=True)
    # What the turn's model calls cost so far (USD; None: a call has no price). Lets an action
    # tool put the cost of the calls that led to its ledger row on the row.
    spent_usd: Callable[[], float | None] | None = Field(default=None, exclude=True)
    # Adds what a tool's own model calls cost (USD; its own gateway, not the turn's — e.g. the
    # blocks calls of `new_goal`) to the turn's spend, so `spent_usd`, the row's turn cost and
    # the turn's cost include them. None: nothing records it.
    charge: Callable[[float | None], None] | None = Field(default=None, exclude=True)
    # A long tool says which step it is on (one plain line, e.g. 「正在把目标分成几大块」): the turn's live
    # status line (StatusLine; never logged or replayed). None: nobody shows it.
    progress: Callable[[str], None] | None = Field(default=None, exclude=True)


class Tool(Protocol):
    spec: ToolSpec

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


class PausingTool(Tool, Protocol):
    """A tool that may ask the learner (`spec.pauses`): `run` returns a result with `pause` and the
    run ends there; his next message resumes it, and `resume` gets the same arguments, the pause
    and that message, and returns the call's result."""

    def resume(self, args: dict[str, Any], ctx: ToolContext, pause: Pause,
               reply: Reply) -> ToolResult: ...
