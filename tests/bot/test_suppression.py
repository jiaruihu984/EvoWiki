from datetime import UTC, datetime, timedelta

from evowiki.bot.suppression import Counters, react, suppressed_until

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
BASE = timedelta(hours=20)


def test_not_interested_counts_and_sets_time():
    c = react(Counters(), "not_interested", NOW)
    assert c.not_interested == 1 and c.last_not_interested_at == NOW


def test_done_changes_nothing():
    c = react(Counters(accepted=1, not_interested=2), "done", NOW)
    assert c == Counters(accepted=1, not_interested=2)


def test_accepted_resets_not_interested():
    c = react(react(Counters(), "not_interested", NOW), "accepted", NOW)
    assert c.accepted == 1 and c.not_interested == 0


def test_suppressed_until_none_without_not_interested():
    assert suppressed_until(Counters(later=3, accepted=2), BASE) is None


def test_suppression_doubles():
    c = react(Counters(), "not_interested", NOW)
    assert suppressed_until(c, BASE) == NOW + 2 * BASE
    c = react(c, "not_interested", NOW + timedelta(days=3))
    assert suppressed_until(c, BASE) == NOW + timedelta(days=3) + 4 * BASE


def test_accepted_ends_suppression():
    c = react(react(Counters(), "not_interested", NOW), "accepted", NOW)
    assert suppressed_until(c, BASE) is None
