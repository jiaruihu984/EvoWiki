"""Bot settings (spec 2026-10-14 §3 thresholds, §7 "Bot 设置"). A user without a stored row gets
DEFAULT_SETTINGS. Approval levels (spec §4 table) are per user: page_body / new_page /
delete_merge / structure / teaching_params / rsi choose `ask` or `auto_undo`; user_notes (`never`)
and suggestion (`auto`) are fixed; preference (a 不感兴趣 click) is fixed `auto_undo`.

`ask` per scope: page_body - the assistant's page edits wait in the ledger; structure /
delete_merge - the assistant's goal / graph / page-split drafts wait for the user; teaching_params (track-A fits of 学习状态评估) / rsi
(the harness loop's changes, A2 method changes) - the RSI cycle still gates every change, and one
that passed waits for the user instead of going live (`teach.approval`); new_page - every page is
created on the user's own click (a goal, a closed draft, 「下一页」), which is never gated; the
Bot's idle writing (`wikiapp.write_queue`, kind `idle_write`) checks this level: `auto_undo` writes
and records `bot_write` rows, `ask` proposes one 「要写 N 页」 row and waits; path (B5b, 「学习路径顺序」) - the weekly review's
move / insert of a learning-path page waits in the ledger instead of being applied. The demo's 「讲解规则和学习状态评估」 row sets
teaching_params and rsi together, so both default to `ask` (user ruling 2026-11-04: an RSI change to a prompt needs his approval)."""

from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

TriggerKind = Literal["review_due", "wrong_answers", "goal_idle", "strategy_negative",
                      "source_added", "open_question", "chat", "idle_write", "goal_review", "card_stale"]
TRIGGER_KINDS: tuple[TriggerKind, ...] = ("review_due", "wrong_answers", "goal_idle",
                                          "strategy_negative", "source_added", "open_question",
                                          "chat", "idle_write", "goal_review", "card_stale")
# `chat` (page suggestion chips): removed from the UI 2026-10-21; code kept until the architecture
# cleanup (its rule is not registered either)
# `idle_write`: writing pages nobody asked for spends money unseen; he turns it on (2026-11-04)
OFF_BY_DEFAULT: frozenset[str] = frozenset({"chat", "idle_write"})
Scope = Literal["user_notes", "page_body", "new_page", "delete_merge", "structure",
                "teaching_params", "rsi", "suggestion", "preference", "path"]
Level = Literal["never", "ask", "auto_undo", "auto"]
LEVELS: dict[Scope, Level] = {
    "user_notes": "never", "page_body": "auto_undo", "new_page": "auto_undo",
    "delete_merge": "ask", "structure": "auto_undo", "teaching_params": "ask",
    "rsi": "ask", "suggestion": "auto", "preference": "auto_undo", "path": "ask",
}                                   # the defaults
CHOOSABLE: dict[Scope, tuple[Level, ...]] = {
    "page_body": ("ask", "auto_undo"), "new_page": ("ask", "auto_undo"),
    "delete_merge": ("ask", "auto_undo"), "structure": ("ask", "auto_undo"),
    "teaching_params": ("ask", "auto_undo"), "rsi": ("ask", "auto_undo"),
    "path": ("ask", "auto_undo"),
}
SCOPE_ZH: dict[str, str] = {
    "user_notes": "你自己的笔记", "page_body": "页面正文", "new_page": "新页面",
    "delete_merge": "删除和合并", "structure": "知识结构", "teaching_params": "学习状态评估",
    "rsi": "讲解规则", "suggestion": "建议", "preference": "偏好", "path": "学习路径顺序",
}
LEVEL_ZH: dict[str, str] = {"never": "从不", "ask": "先问我", "auto_undo": "直接做",
                            "auto": "自动"}
Paused = Literal["no", "until", "always"]


class BotSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    timezone: str = "Asia/Shanghai"
    paused: Paused = "no"
    paused_until: AwareDatetime | None = None
    kinds: dict[TriggerKind, bool] = Field(
        default_factory=lambda: {k: k not in OFF_BY_DEFAULT for k in TRIGGER_KINDS})
    today_cap: int = Field(default=5, ge=0, le=50)
    page_cap: int = Field(default=2, ge=0, le=20)
    wrong_count: int = Field(default=3, ge=2, le=20)
    wrong_days: int = Field(default=7, ge=1, le=60)
    idle_days: int = Field(default=3, ge=1, le=60)
    open_question_days: int = Field(default=3, ge=1, le=60)
    daily_hour: int = Field(default=8, ge=0, le=23)
    daily_minute: int = Field(default=0, ge=0, le=59)
    cooldown_hours: float = Field(default=20.0, gt=0, le=24 * 30)
    failure_streak: int = Field(default=3, ge=1, le=20)
    # weekly goal review (B5 spec §1): runs at the daily check of this weekday (0 = Monday) or the
    # first daily check after it in the same ISO week
    review_weekday: int = Field(default=0, ge=0, le=6)
    weekly_debt_ratio: float = Field(default=2.0, gt=0, le=20)      # due + overdue >= ratio x max/day
    weekly_debt_pages: int = Field(default=2, ge=1, le=50)          # ... and new pages read this week
    weekly_known_share: float = Field(default=0.5, gt=0, le=1)      # remaining cells already known
    weekly_park_days: int = Field(default=14, ge=1, le=365)         # not opened for this long
    weekly_park_declines: int = Field(default=2, ge=1, le=20)       # goal_idle 不感兴趣 in 30 days
    weekly_stuck_minutes: int = Field(default=60, ge=1, le=24 * 60)  # read on one unfinished page
    levels: dict[Scope, Level] = Field(default_factory=lambda: dict(LEVELS))

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown time zone {v!r}") from e
        return v

    @field_validator("kinds")
    @classmethod
    def _all_kinds(cls, v: dict) -> dict:
        return {k: bool(v.get(k, k not in OFF_BY_DEFAULT)) for k in TRIGGER_KINDS}

    @field_validator("levels")
    @classmethod
    def _levels(cls, v: dict) -> dict:
        out = {**LEVELS, **v}
        for scope, level in out.items():
            if scope in CHOOSABLE:
                if level in CHOOSABLE[scope]:
                    continue
                choices = "、".join(f"「{LEVEL_ZH[x]}」" for x in CHOOSABLE[scope])
                raise ValueError(f"「{SCOPE_ZH.get(scope, '这项')}」的审批方式只能选{choices}")
            if level != LEVELS[scope]:
                raise ValueError(f"「{SCOPE_ZH.get(scope, '这项')}」的审批方式固定为"
                             f"「{LEVEL_ZH[LEVELS[scope]]}」，不能修改")
        return out

    @model_validator(mode="after")
    def _until(self) -> "BotSettings":
        if self.paused == "until" and self.paused_until is None:
            raise ValueError("paused=until needs paused_until")
        return self


DEFAULT_SETTINGS = BotSettings()


# how long after his client was last in front of him the Bot keeps working for him (`bot_serves`)
ACTIVE_WINDOW = timedelta(days=7)


def bot_serves(s: BotSettings, last_seen: datetime | None, now: datetime) -> bool:
    """Whether the Bot works for this user now, the one rule behind its schedule and its idle writing
    (everything it does costs money or lands on a screen). The Bot is on unless he paused it, and it
    serves only someone who is there: his Obsidian window was in front of him within `ACTIVE_WINDOW`
    (`POST /me/seen`). An account only a script ever used has no `last_seen` and is never served. Back
    after a longer absence: the next tick serves him, today's daily slot included."""
    return (not effective_paused(s, now) and last_seen is not None
            and now - last_seen <= ACTIVE_WINDOW)


def effective_paused(s: BotSettings, now: datetime) -> bool:
    if s.paused == "always":
        return True
    return s.paused == "until" and s.paused_until is not None and now < s.paused_until
