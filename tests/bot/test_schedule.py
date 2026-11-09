from datetime import UTC, date, datetime, timedelta

from evowiki.bot.schedule import (
    Slot,
    daily_slots,
    local_day_bounds,
    next_daily_slot,
    nominal_time,
    slot_decision,
)
from evowiki.bot.settings import DEFAULT_SETTINGS


def test_daily_slots_shanghai_morning():
    now = datetime(2026, 10, 14, 0, 30, tzinfo=UTC)          # 08:30 Shanghai
    assert daily_slots(now, DEFAULT_SETTINGS) == [
        Slot(date(2026, 10, 13), datetime(2026, 10, 13, 0, 0, tzinfo=UTC)),
        Slot(date(2026, 10, 14), datetime(2026, 10, 14, 0, 0, tzinfo=UTC))]


def test_daily_slots_before_today_slot():
    now = datetime(2026, 10, 13, 23, 0, tzinfo=UTC)          # 07:00 Shanghai on the 14th
    assert [s.local_date for s in daily_slots(now, DEFAULT_SETTINGS)] == [date(2026, 10, 13)]


def test_slot_stays_good_to_the_end_of_its_local_day():
    slot = Slot(date(2026, 10, 14), datetime(2026, 10, 14, 0, 0, tzinfo=UTC))    # 08:00 Shanghai
    assert slot_decision(slot, slot.at + timedelta(hours=13), "Asia/Shanghai") == "run"    # 21:00
    assert slot_decision(slot, slot.at + timedelta(hours=16), "Asia/Shanghai") == "missed"  # 00:00


def test_cooldown_counts_from_the_slot_of_a_caught_up_check():
    slot = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)
    assert nominal_time(slot + timedelta(hours=13), DEFAULT_SETTINGS) == slot
    assert nominal_time(slot - timedelta(hours=1), DEFAULT_SETTINGS) == slot - timedelta(hours=1)


def test_dst_berlin_keeps_local_time():
    s = DEFAULT_SETTINGS.model_copy(update={"timezone": "Europe/Berlin", "daily_hour": 8})
    slots = daily_slots(datetime(2026, 10, 25, 12, 0, tzinfo=UTC), s)
    assert slots == [
        Slot(date(2026, 10, 24), datetime(2026, 10, 24, 6, 0, tzinfo=UTC)),   # CEST +2
        Slot(date(2026, 10, 25), datetime(2026, 10, 25, 7, 0, tzinfo=UTC))]   # CET +1


def test_local_day_bounds():
    now = datetime(2026, 10, 14, 0, 30, tzinfo=UTC)
    assert local_day_bounds(now, "Asia/Shanghai") == (
        datetime(2026, 10, 13, 16, 0, tzinfo=UTC), datetime(2026, 10, 14, 16, 0, tzinfo=UTC))


def test_next_daily_slot_is_strictly_after_now():
    slot_today = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)               # 08:00 Shanghai
    assert next_daily_slot(datetime(2026, 10, 13, 23, 59, tzinfo=UTC), DEFAULT_SETTINGS) == slot_today
    assert next_daily_slot(slot_today, DEFAULT_SETTINGS) == slot_today + timedelta(days=1)
    assert next_daily_slot(slot_today + timedelta(hours=9), DEFAULT_SETTINGS) == (
        slot_today + timedelta(days=1))
