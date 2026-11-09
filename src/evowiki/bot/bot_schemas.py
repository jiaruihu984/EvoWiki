"""Wire shapes for /wiki/bot/*."""

from collections.abc import Collection
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from evowiki.bot.actions import TURN_COST, can_undo, made_of
from evowiki.bot.revise_schemas import ReviseOut
from evowiki.bot.suggestions import ReviewReaction
from evowiki.bot.suppression import Reaction
from evowiki.store.bot import ActionRow, SuggestionRow


class SuggestionOut(BaseModel):
    suggestion_id: str
    kind: str
    title: str
    why: str
    action: dict
    placement: str
    page_id: str | None
    priority: int
    status: str
    created_at: datetime
    expires_at: datetime
    trigger_id: str | None
    preference_action_id: str | None = None      # the 偏好 row of a 不感兴趣 click, for 撤回

    @classmethod
    def of(cls, r: SuggestionRow, preference_action_id: str | None = None) -> "SuggestionOut":
        data = {k: getattr(r, k) for k in cls.model_fields if k != "preference_action_id"}
        return cls(**data, preference_action_id=preference_action_id)


class ReactIn(BaseModel):
    reaction: Reaction


class ReviewReactIn(BaseModel):
    reaction: ReviewReaction


class ReviewReminderOut(BaseModel):
    hidden: bool                                 # the 今日复习 card is cooling down
    preference_action_id: str | None = None      # 减少提醒: its ledger row, for 撤回


class BadgeOut(BaseModel):
    today_new: int
    pending_actions: int


class PauseIn(BaseModel):
    until: Literal["tomorrow", "forever", "off"]


class DiffLine(BaseModel):
    kind: Literal["ctx", "add", "del"]
    text: str


class MadeOut(BaseModel):
    """A page an executed action made: the draft `key` it came from, its path position `n`
    (1-based, None when not on a path) and its folder."""
    key: str | None = None
    page_id: str
    n: int | None = None
    folder: str = ""


GOAL_RESULT = ("draft_id", "map_id", "path_id", "page_id", "open")


class ActionOut(BaseModel):
    action_id: str
    kind: str
    scope: str
    level: str
    status: str
    source: str
    summary: str
    why: str
    content: dict
    page_id: str | None
    detail: str
    created_at: datetime
    decided_at: datetime | None
    expires_at: datetime | None
    can_undo: bool
    confirm: bool = False
    cost_usd: float | None = None
    diff: list[DiffLine] = []
    made: list[MadeOut] = []             # the pages an executed row made (their writing status: GET /wiki/writes)
    revise: ReviseOut | None = None      # page_revise: the draft as track changes in the page
    # what an executed goal_new opened (学点新的 continues with that draft / map after a chat 确认);
    # only these ids, never the rest of the executor's result (undo data, internals)
    result: dict[str, str | bool | None] = {}
    # the section the change is in, when one is known (an assistant edit: the section it rewrote or
    # added): a 参数 change card opens its page there
    section_id: str | None = None

    @classmethod
    def of(cls, r: ActionRow, diff: list[dict] | None = None,
           superseded: Collection[str] = (), revise: ReviseOut | None = None) -> "ActionOut":
        data = {k: getattr(r, k) for k in cls.model_fields
                if k not in ("can_undo", "confirm", "cost_usd", "diff", "made", "revise", "result",
                             "section_id")}
        from evowiki.bot.presentation import action_detail, action_summary
        data["summary"] = action_summary(r)
        data["detail"] = action_detail(r)
        res = r.result or {}
        # a graph split: the points it made as its list (a model may have chosen them after 确认),
        # or why it left the node whole; the same as the chat card (assistant tools card_of)
        if res.get("parts"):
            data["content"] = {**r.content, "pages": res["parts"]}
        if res.get("kept") and not r.detail:
            data["detail"] = res["kept"]
        own = res.get("cost_usd")
        if own is None and revise is not None:
            own = r.content.get("cost_usd")             # a draft's writing cost, before approval
        # the row's own work (cost_usd) plus the assistant turn's calls behind it (turn_cost_usd)
        parts = [float(v) for v in (own, res.get(TURN_COST))
                 if isinstance(v, int | float) and not isinstance(v, bool)]
        return cls(**data, can_undo=can_undo(r, superseded), confirm=bool(r.content.get("confirm")),
                   cost_usd=sum(parts) if parts else None,
                   diff=[DiffLine(**ln) for ln in diff or []], made=[MadeOut(**m) for m in made_of(r)],
                   revise=revise, result={k: res[k] for k in GOAL_RESULT if k in res},
                   section_id=section_of(r))


def section_of(r: ActionRow) -> str | None:
    """The section an executed edit touched (its undo data), else the one its content names, else the
    one its draft names (加交互, page_practice: the section the interactive goes into)."""
    draft = r.content.get("draft") if isinstance(r.content.get("draft"), dict) else {}
    for src in (r.undo or {}, r.content, draft):
        sid = src.get("section_id")
        if isinstance(sid, str) and sid:
            return sid
    return None


class ApproveIn(BaseModel):
    """`draft`: the user's edited version of the proposal, applied before it executes."""
    draft: dict | None = None


class DraftIn(BaseModel):
    """`draft`: the user's edited version of a waiting proposal, kept without approving it
    (POST /wiki/bot/actions/{id}/draft)."""
    draft: dict


class WhyTrigger(BaseModel):
    kind: str
    rule_id: str
    rule_version: int
    reason: str
    evidence: list
    created_at: datetime


class WhyGate(BaseModel):
    outcome: str | None
    reason: str | None
    detail: str | None
    at: datetime | None


class WhyOutput(BaseModel):
    """`rsi_change`: an RSI patch a Bot-started cycle made (B5c); its id is the change list's
    `patch_id` (参数 · 最近改了什么)."""
    type: Literal["suggestion", "action", "rsi_change"]
    id: str
    kind: str
    title: str
    status: str


class WhyReaction(BaseModel):
    status: str
    reacted_at: datetime | None


class WhyOut(BaseModel):
    trigger: WhyTrigger | None
    gate: WhyGate | None
    outputs: list[WhyOutput]
    reaction: WhyReaction | None


class WriteActivity(BaseModel):
    """The page-write side of a 「Bot 最近做了什么」 row (`kind` idle_write / user_write): where the
    pieces of its 为什么？ chain come from. `action_id`: the run's own ledger row (`bot_write` /
    `user_write`) that 撤销 undoes: the page back to unwritten; the live status and cost of a
    page still writing come from GET /wiki/writes."""
    page_id: str
    title: str
    folder: str = ""
    goal: str | None = None             # the path the page is on, and its place there
    n: int | None = None
    asked: str | None = None            # user_write: the assistant action that asked for it
    origin: str = ""                    # the run's origin: idle (the Bot), open / background (asked for)
    action_id: str | None = None
    can_undo: bool = False
    undone: bool = False
    opened: bool = False                # the learner opened the page after it was written
    calls: int = 0
    outline_calls: int = 0              # of `calls`: the organizer side (domain pick, outline)
    section_calls: int = 0              # and the writer's


class ActivityOut(BaseModel):
    trigger_id: str | None
    run_id: str | None = None
    kind: str
    title: str
    reason: str
    at: datetime
    gate_outcome: str | None
    gate_reason: str | None
    gate_detail: str | None
    result: Literal["suggested", "skipped", "handed_to_rsi", "reviewed", "writing",
                    "written"]
    suggestion_title: str | None
    suggestion_status: str | None
    cost_usd: float
    write: WriteActivity | None = None
    page_id: str | None = None        # the page its card is about (个性化 · 活动记录 opens it)
