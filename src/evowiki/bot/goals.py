"""The user's goals as the Bot sees them (B5 spec §1.4, §3): parked or not, the latest weekly
review, this week's reading time; park / resume. Parking touches only the Bot (no goal_idle card,
no weekly review): the goal and its pages stay as they are, and nothing goes into the wiki event
log (no 409 for the user's flows)."""

from datetime import date, datetime

from pydantic import BaseModel

from evowiki.bot.goal_week import window
from evowiki.store import bot as store_bot
from evowiki.store import bot_goals
from evowiki.wikiapp.repo import load_in


class GoalReviewOut(BaseModel):
    week_start: date
    situation: str
    trigger_id: str | None


class GoalOut(BaseModel):
    path_id: str
    goal: str
    parked: bool
    parked_at: datetime | None
    last_review: GoalReviewOut | None
    reading_minutes: int | None          # this review window; None: unknown (§1.6)


def list_goals(pool, user_id: str, *, now: datetime) -> list[GoalOut]:
    with pool.connection() as conn:
        state = load_in(conn, user_id).state
        tz = store_bot.get_settings(conn, user_id).timezone
        parked = bot_goals.parked(conn, user_id)
        reviews = bot_goals.latest_reviews(conn, user_id)
        _, first, last = window(now, tz)
        since = bot_goals.first_reading_day(conn, user_id)
        known = since is not None and since <= first
        out = []
        for pid in state.path_order:
            gs = state.goal_settings.get(pid)
            if gs is not None and gs.archived:           # taken back: not listed
                continue
            rec = state.paths[pid]
            minutes = None
            if known:
                pages = [p for p in rec.page_ids if p is not None]
                secs = bot_goals.reading_by_page(conn, user_id, pages, first, last)
                minutes = sum(secs.values()) // 60
            r = reviews.get(pid)
            out.append(GoalOut(
                path_id=pid, goal=rec.path.goal, parked=pid in parked,
                parked_at=parked.get(pid), reading_minutes=minutes,
                last_review=None if r is None else GoalReviewOut(
                    week_start=r.week_start, situation=r.situation, trigger_id=r.trigger_id)))
    return out


def _one(pool, user_id: str, path_id: str, now: datetime) -> GoalOut:
    return next(g for g in list_goals(pool, user_id, now=now) if g.path_id == path_id)


def set_parked(pool, user_id: str, path_id: str, parked: bool, *, now: datetime) -> GoalOut:
    """LookupError for a path the user does not have. Idempotent."""
    with pool.connection() as conn, conn.transaction():
        state = load_in(conn, user_id).state
        if path_id not in state.paths:
            raise LookupError("没有找到这条学习路径")
        gs = state.goal_settings.get(path_id)
        if gs is not None and gs.archived:               # taken back: list_goals skips it
            error = LookupError("这个目标已经收回了")
            error.public_message = "This learning goal has been taken back."
            error.error_code = "goal_taken_back"
            error.params = {}
            raise error
        if parked:
            bot_goals.park(conn, user_id, path_id, now)
        else:
            bot_goals.resume(conn, user_id, path_id, now)
    return _one(pool, user_id, path_id, now)
