"""Suggestion read/react layer (spec 2026-10-14 §3/§7): list, react, badge. Expiry and showing a
card are not reactions and never touch the learned-suppression counters. A card is started
(`accepted`, stays listed), declined (`not_interested`), or - once started - marked `done`."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from evowiki.agent.effects import payload_hash
from evowiki.bot.facts import remedial_is_current
from evowiki.bot.labels import md_hm
from evowiki.bot.schedule import local_day_bounds
from evowiki.bot.suppression import Reaction, suppressed_until
from evowiki.bot.suppression import react as react_counters
from evowiki.store import bot as store
from evowiki.store.bot import ActionRow, SuggestionRow

OPEN = ("pending", "shown")            # a card that can still be started or declined


class BotConflict(ValueError):
    """The suggestion can no longer take a reaction (expired or already reacted) → HTTP 409."""


def _remedial_current(conn, row: SuggestionRow, now: datetime) -> bool:
    if row.kind != "wrong_answers":
        return True
    trigger = store.get_trigger(conn, row.trigger_id) if row.trigger_id else None
    return trigger is not None and remedial_is_current(
        conn, row.user_id, cell=trigger.payload["cell"], evidence=trigger.evidence,
        page_id=row.page_id, now=now, settings=store.get_settings(conn, row.user_id))


def list_suggestions(pool, user_id: str, *, placement: Literal["today", "page"],
                     page_id: str | None, now: datetime) -> list[SuggestionRow]:
    with pool.connection() as conn, conn.transaction():
        store.expire_suggestions(conn, user_id, now)
        out = []
        for r in store.open_suggestions(conn, user_id, placement=placement, page_id=page_id,
                                        now=now):
            if not _remedial_current(conn, r, now):
                store.set_suggestion(conn, r.suggestion_id, status="expired", at=now)
                continue
            if r.status == "pending":
                store.mark_shown(conn, r.suggestion_id)      # a reaction that won stays
                r = store.lock_suggestion(conn, r.suggestion_id)
                if r.status not in (*OPEN, "accepted"):
                    continue
            out.append(r)
    return out


def react(pool, user_id: str, suggestion_id: str, reaction: Reaction, *,
          now: datetime) -> SuggestionRow:
    with pool.connection() as conn, conn.transaction():
        row = store.lock_suggestion(conn, suggestion_id)
        if row is None or row.user_id != user_id:
            raise LookupError("没有找到这张卡片")
        if reaction == "accepted" and not _remedial_current(conn, row, now):
            raise BotConflict("这张补讲卡片的答题记录已变化，请刷新待办")
        if reaction == "done":
            if row.status != "accepted":                # 完成 only follows 开始
                raise BotConflict("这张卡片还没有开始，不能标记完成")
            if row.expires_at <= now:                   # the card's day is over (status stays)
                raise BotConflict("这张卡片已过期")
        else:
            if row.status not in OPEN:
                raise BotConflict("这张卡片已经处理过了")
            if row.expires_at <= now:
                raise BotConflict("这张卡片已过期")
        store.set_suggestion(conn, suggestion_id, status=reaction, at=now)
        counters = store.get_counters(conn, user_id, row.dedup_key)
        updated = react_counters(counters, reaction, now)
        if updated != counters:
            store.put_counters(conn, user_id, row.dedup_key, updated)
        if reaction == "not_interested":
            _record_preference(conn, row, updated.not_interested, now)
        return store.lock_suggestion(conn, suggestion_id)


PREFERENCE_KIND = "preference"


def preference_key(suggestion_id: str) -> str:
    return f"pref:{suggestion_id}"


def _record_preference(conn, row: SuggestionRow, count: int, now: datetime) -> None:
    """The ledger row of a 不感兴趣 click (spec §5 偏好改了), in the reaction's transaction. Its
    undo (`PreferenceExecutor`) takes the counter and the card back."""
    content = {"suggestion_id": row.suggestion_id, "dedup_key": row.dedup_key}
    store.upsert_action_row(conn, _preference_row(
        row.user_id, summary=f"以后少提醒「{row.title}」", why="你点了「不感兴趣」", content=content,
        key=preference_key(row.suggestion_id), now=now))


def _preference_row(user_id: str, *, summary: str, why: str, content: dict, key: str,
                    now: datetime) -> ActionRow:
    return ActionRow(
        action_id=store.new_action_id(), user_id=user_id, kind=PREFERENCE_KIND,
        scope="preference", level="auto_undo", status="executed", source="bot", summary=summary,
        why=why, content=content, content_hash=payload_hash(content), idempotency_key=key,
        created_at=now, decided_at=now)


def preference_ids(pool, user_id: str, suggestion_ids: list[str]) -> dict[str, str]:
    """suggestion id -> action id of its 偏好 row, for the cards that have one in force."""
    with pool.connection() as conn:
        return store.preference_action_ids(conn, user_id, suggestion_ids)


# ---- 今日复习: the card is the plugin's own, its cooldown is the review_due suggestion's ----

REVIEW_DEDUP_KEY = "review_due"        # rules._review_due
ReviewReaction = Literal["accepted", "not_interested"]


@dataclass(frozen=True)
class ReviewReminder:
    hidden: bool
    preference_action_id: str | None = None      # 减少提醒: the ledger row it wrote, for 撤回


def _review_until(conn, user_id: str, counters, now: datetime) -> datetime | None:
    """When the 今日复习 card comes back; None when it is not cooling down."""
    base = timedelta(hours=store.get_settings(conn, user_id).cooldown_hours)
    until = suppressed_until(counters, base)
    return until if until is not None and until > now else None


def review_cooldown_line(conn, user_id: str, *, now: datetime) -> str | None:
    """The assistant's sentence about a cooling 今日复习 card (`assistant.tools.read_more`)."""
    counters = store.get_counters(conn, user_id, REVIEW_DEDUP_KEY)
    until = _review_until(conn, user_id, counters, now)
    if until is None:
        return None
    tz = store.get_settings(conn, user_id).timezone
    return (f"他点过 {counters.not_interested} 次「减少提醒」，所以 {md_hm(until, tz)} 前不显示"
            "「今日复习」卡；这是他自己选的，开始复习或撤销后恢复")


def review_reminder(pool, user_id: str, *, now: datetime) -> ReviewReminder:
    with pool.connection() as conn:
        counters = store.get_counters(conn, user_id, REVIEW_DEDUP_KEY)
        return ReviewReminder(_review_until(conn, user_id, counters, now) is not None)


def review_react(pool, user_id: str, reaction: ReviewReaction, *, now: datetime) -> ReviewReminder:
    """开始复习 or 减少提醒, with or without a review_due suggestion today: the counters always
    move; an open suggestion takes the same reaction. 减少提醒 writes one ledger row per click,
    with what its undo needs (`PreferenceExecutor`)."""
    with pool.connection() as conn, conn.transaction():
        rows = store.open_suggestions(conn, user_id, placement="today", page_id=None, now=now)
        linked = next((r.suggestion_id for r in rows
                       if r.dedup_key == REVIEW_DEDUP_KEY and r.status in OPEN), None)
        if linked is not None:
            store.set_suggestion(conn, linked, status=reaction, at=now)
        before = store.get_counters(conn, user_id, REVIEW_DEDUP_KEY)
        counters = react_counters(before, reaction, now)
        store.put_counters(conn, user_id, REVIEW_DEDUP_KEY, counters)
        action_id = None
        if reaction == "not_interested":
            last = before.last_not_interested_at
            row = _preference_row(
                user_id, summary="以后少提醒复习", why="你点了「减少提醒」",
                content={"suggestion_id": linked, "dedup_key": REVIEW_DEDUP_KEY,
                         "accepted": before.accepted,
                         "previous_at": last.isoformat() if last is not None else None},
                key=f"pref:{REVIEW_DEDUP_KEY}:{store.new_action_id()}", now=now)
            store.upsert_action_row(conn, row)
            action_id = row.action_id
        return ReviewReminder(_review_until(conn, user_id, counters, now) is not None, action_id)


def badge(pool, user_id: str, *, now: datetime) -> tuple[int, int]:
    """(today cards created today and not yet reacted, proposed actions waiting)."""
    with pool.connection() as conn:
        tz = store.get_settings(conn, user_id).timezone
        start, end = local_day_bounds(now, tz)
        today = store.count_open_today(conn, user_id, start=start, end=end, now=now)
        pending = store.count_pending(conn, user_id, now)
    return today, pending
