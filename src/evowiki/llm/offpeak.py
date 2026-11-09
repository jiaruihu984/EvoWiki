"""DeepSeek's off-peak pricing window (spec 2026-10-21-offpeak-writes §1).

From https://api-docs.deepseek.com/quick_start/pricing/, checked 2026-10-21: peak is Monday to
Friday 09:00-12:00 and 14:00-18:00 Beijing time, excluding Chinese public holidays; every other
hour (weekends and public holidays in full) is off-peak, at half the peak rates. Pure functions of
an aware datetime (callers pass their injected clock's time). `holidays` are Beijing dates that are
off-peak all day; by default the PRC public holidays of the `holidays` package (which also lists
the weekday rest days swapped for a weekend, 调休). Pass `()` for none."""

from collections.abc import Collection
from datetime import date, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo

import holidays as _holidays

BEIJING = ZoneInfo("Asia/Shanghai")
PEAK_HOURS: tuple[tuple[time, time], ...] = ((time(9), time(12)), (time(14), time(18)))


@cache
def cn_holidays() -> Collection[date]:
    """PRC public holidays (years are added on lookup)."""
    return _holidays.country_holidays("CN")


def _local(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError(f"naive datetime {at!r}: pass an aware time")
    return at.astimezone(BEIJING)


def _peak_day(d: date, holidays: Collection[date] | None) -> bool:
    if holidays is None:
        holidays = cn_holidays()
    return d.weekday() < 5 and d not in holidays


def is_off_peak(at: datetime, holidays: Collection[date] | None = None) -> bool:
    local = _local(at)
    if not _peak_day(local.date(), holidays):
        return True
    t = local.time()
    return not any(a <= t < b for a, b in PEAK_HOURS)


def next_peak(at: datetime, holidays: Collection[date] | None = None) -> datetime:
    """The first peak instant at or after `at` (`at` itself when it is in a peak hour)."""
    local = _local(at)
    for n in range(30):          # the longest holiday run plus its weekends is under a month
        d = local.date() + timedelta(days=n)
        if not _peak_day(d, holidays):
            continue
        for a, b in PEAK_HOURS:
            start = datetime.combine(d, a, tzinfo=BEIJING)
            if datetime.combine(d, b, tzinfo=BEIJING) > local:
                return max(start, local).astimezone(at.tzinfo)
    raise AssertionError("unreachable: some day within a month ahead is a peak day")


def off_peak_through(start: datetime, end: datetime, holidays: Collection[date] | None = None) -> bool:
    """No peak instant in [start, end): a call or a write over this span is all off-peak."""
    if end < start:
        raise ValueError(f"end {end} before start {start}")
    return is_off_peak(start, holidays) and next_peak(start, holidays) >= end
