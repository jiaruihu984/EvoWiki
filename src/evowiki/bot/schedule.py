"""Daily-slot math (B3 spec §3): pure, DST-safe (slots are built in the user's zone)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from evowiki.bot.settings import BotSettings
from evowiki.localday import local_date


@dataclass(frozen=True)
class Slot:
    local_date: date
    at: datetime                   # UTC


def _slot(d: date, tz: ZoneInfo, settings: BotSettings) -> Slot:
    local = datetime.combine(d, time(settings.daily_hour, settings.daily_minute), tzinfo=tz)
    return Slot(d, local.astimezone(UTC))


def daily_slots(now: datetime, settings: BotSettings) -> list[Slot]:
    """The daily slots of the local yesterday and today with `at <= now`, oldest first."""
    tz = ZoneInfo(settings.timezone)
    today = local_date(now, settings.timezone)
    slots = [_slot(today - timedelta(days=1), tz, settings), _slot(today, tz, settings)]
    return [s for s in slots if s.at <= now]


def next_daily_slot(now: datetime, settings: BotSettings) -> datetime:
    """The first daily slot strictly after `now` (UTC)."""
    tz = ZoneInfo(settings.timezone)
    today = local_date(now, settings.timezone)
    for d in (today, today + timedelta(days=1)):
        slot = _slot(d, tz, settings).at
        if slot > now:
            return slot
    raise AssertionError("unreachable: tomorrow's slot is after now")


def slot_decision(slot: Slot, now: datetime, tz: str) -> Literal["run", "missed"]:
    """Anacron's rule: a laptop is often off at the slot, so today's slot stays good all day and a
    past date is never caught up. The slot row and the date-based workflow id keep it to one run."""
    return "run" if slot.local_date == local_date(now, tz) else "missed"


def nominal_time(at: datetime, settings: BotSettings) -> datetime:
    """When a daily check that ran at `at` was due. The cooldown counts from here: a check caught
    up at 21:00 must not make tomorrow's 08:00 check skip, since that check is not retried."""
    return min(at, _slot(local_date(at, settings.timezone), ZoneInfo(settings.timezone), settings).at)


def local_day_bounds(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """UTC start and end of the local day containing `now`."""
    zone = ZoneInfo(tz)
    d = local_date(now, tz)
    start = datetime.combine(d, time(0), tzinfo=zone)
    end = datetime.combine(d + timedelta(days=1), time(0), tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)
