"""The deterministic gate (B3 spec §3): one intent + the user's counters in, a decision out.

Pure: no DB, no clock reads. Order: paused, kind_off, (route rsi passes here), cap_reached,
cooldown, suppressed, pass. A one-shot event kind (its event is
never seen again) defers instead of skipping on paused / cap_reached, so it shows up later.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from evowiki.bot.labels import kind_zh, md, md_hm
from evowiki.bot.rules import Intent
from evowiki.bot.settings import BotSettings, effective_paused
from evowiki.bot.suppression import Counters, suppressed_until

SkipReason = Literal["paused", "kind_off", "cap_reached", "cooldown", "suppressed", "empty", "busy"]
SKIP_REASONS: tuple[SkipReason, ...] = ("paused", "kind_off", "cap_reached", "cooldown",
                                        "suppressed", "empty", "busy")


@dataclass(frozen=True)
class GateContext:
    settings: BotSettings
    now: datetime
    placed_today: int              # suggestions already created today (local day) for this placement
    placed_on_page: int            # for placement "page": suggestions for this page today
    last_suggested_at: datetime | None
    counters: Counters


@dataclass(frozen=True)
class GateDecision:
    outcome: Literal["pass", "defer", "skip"]
    reason: SkipReason | None
    # plain Chinese, in the user's time zone; not part of the decision's identity
    detail: str = field(default="", compare=False)


# Kinds fired once by an event the cursor then passes: a skip would lose them for good.
ONE_SHOT_KINDS = frozenset({"source_added", "card_stale"})
_WAIT_REASONS = frozenset({"paused", "cap_reached"})


def gate(intent: Intent, ctx: GateContext) -> GateDecision:
    d = _gate(intent, ctx)
    if d.outcome == "skip" and d.reason in _WAIT_REASONS and intent.kind in ONE_SHOT_KINDS:
        return GateDecision("defer", d.reason, d.detail)    # the sweep retries it
    return d


def _gate(intent: Intent, ctx: GateContext) -> GateDecision:
    s = ctx.settings
    zh = kind_zh(intent.kind)
    if effective_paused(s, ctx.now):
        return GateDecision("skip", "paused", "Bot 已暂停")
    if not s.kinds[intent.kind]:
        return GateDecision("skip", "kind_off", f"「{zh}」已关闭")
    if intent.route == "rsi":
        return GateDecision("pass", None, f"「{zh}」已开启")
    on_page = intent.placement == "page"
    placed, cap = ((ctx.placed_on_page, s.page_cap) if on_page else (ctx.placed_today, s.today_cap))
    if placed >= cap:
        return GateDecision("skip", "cap_reached", f"这一页今天的提示已达 {cap} 条上限" if on_page
                            else f"今天的待办已达 {cap} 项上限")
    cooldown = timedelta(hours=s.cooldown_hours)
    if ctx.last_suggested_at is not None and ctx.now - ctx.last_suggested_at < cooldown:
        return GateDecision("skip", "cooldown", md_hm(
            ctx.last_suggested_at + cooldown, s.timezone) + " 之后再提醒")
    until = suppressed_until(ctx.counters, cooldown)
    if until is not None and until > ctx.now:
        return GateDecision("skip", "suppressed",
                            f"你点过「不感兴趣」，{md(until, s.timezone)} 前不再安排")
    return GateDecision("pass", None, f"这一页今天第 {placed + 1} / {cap} 条" if on_page
                        else f"今日第 {placed + 1} / {cap} 项")


def allow_check(recent: Sequence[datetime], now: datetime, *, limit: int = 5,
                window: timedelta = timedelta(seconds=60)) -> bool:
    """Flood guard: False when `limit` or more checks happened in (now - window, now]."""
    return sum(1 for t in recent if now - window < t <= now) < limit
