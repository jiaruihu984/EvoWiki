from datetime import UTC, datetime, timedelta

import pytest

from evowiki.bot.pipeline import dispatch
from evowiki.bot.suggestions import BotConflict, badge, list_suggestions, react
from evowiki.bot.suppression import Counters
from evowiki.store import bot as store
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)     # 17:00 in Asia/Shanghai
END = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)    # next local midnight


@pytest.fixture(autouse=True)
def _users(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")


def put(pool, sid, *, uid="u1", dedup="review_due", **kw):
    base = {"suggestion_id": sid, "user_id": uid, "kind": "review_due", "title": "t", "why": "w",
            "action": {"type": "open_settings"}, "placement": "today", "priority": 1,
            "dedup_key": dedup, "expires_at": END, "created_at": NOW - timedelta(hours=1)}
    with pool.connection() as conn:
        store.insert_suggestion(conn, store.SuggestionRow(**{**base, **kw}))


def get(pool, sid):
    with pool.connection() as conn:
        return store.lock_suggestion(conn, sid)


def ids(rows):
    return [r.suggestion_id for r in rows]


def test_list_marks_shown_orders_and_filters_placement(pool):
    put(pool, "a", priority=1)
    put(pool, "b", priority=5)
    put(pool, "c", priority=5, created_at=NOW - timedelta(hours=2))
    put(pool, "p", placement="page", page_id="pg1")
    put(pool, "q", placement="page", page_id="pg2")
    put(pool, "o", uid="u2")
    rows = list_suggestions(pool, "u1", placement="today", page_id=None, now=NOW)
    assert ids(rows) == ["c", "b", "a"] and {r.status for r in rows} == {"shown"}
    assert get(pool, "a").status == "shown" and get(pool, "a").reacted_at is None
    assert ids(list_suggestions(pool, "u1", placement="page", page_id="pg1", now=NOW)) == ["p"]
    assert get(pool, "q").status == "pending"


def test_expiry_does_not_touch_counters(pool):
    put(pool, "a", expires_at=NOW)
    put(pool, "b", status="shown", expires_at=NOW - timedelta(minutes=1))
    assert list_suggestions(pool, "u1", placement="today", page_id=None, now=NOW) == []
    assert get(pool, "a").status == "expired" and get(pool, "b").status == "expired"
    assert get(pool, "a").reacted_at is None
    with pool.connection() as conn:
        assert store.get_counters(conn, "u1", "review_due") == Counters()


def test_not_interested_counts_and_suppresses_next_dispatch(pool):
    put(pool, "a", created_at=NOW - timedelta(hours=25))     # past the 20 h cooldown
    r = react(pool, "u1", "a", "not_interested", now=NOW)
    assert r.status == "not_interested" and r.reacted_at == NOW
    with pool.connection() as conn:
        c = store.get_counters(conn, "u1", "review_due")
        assert c.not_interested == 1 and c.last_not_interested_at == NOW
        store.insert_trigger(conn, store.TriggerRow(
            trigger_id="t1", user_id="u1", kind="review_due", rule_id="review_due",
            rule_version=1, source_key="k", dedup_key="review_due", route="direct", evidence=[],
            payload={"count": 2, "cells": [], "page_id": None}, priority=50, reason="r",
            created_at=NOW))
    d = dispatch(pool, "t1", now=NOW + timedelta(minutes=1), new_id=lambda p: f"{p}_1")
    assert (d.outcome, d.reason) == ("skip", "suppressed")


def test_accepted_resets_counters_and_stays_listed_until_done(pool):
    put(pool, "a")
    with pool.connection() as conn:
        store.put_counters(conn, "u1", "review_due", Counters(not_interested=2))
    assert react(pool, "u1", "a", "accepted", now=NOW).status == "accepted"
    with pool.connection() as conn:
        c = store.get_counters(conn, "u1", "review_due")
        assert (c.accepted, c.not_interested) == (1, 0)
    # a started card stays on the list (so 完成 can follow), a done one leaves it
    [card] = list_suggestions(pool, "u1", placement="today", page_id=None, now=NOW)
    assert card.suggestion_id == "a" and card.status == "accepted"
    done = react(pool, "u1", "a", "done", now=NOW + timedelta(hours=1))
    assert done.status == "done" and done.reacted_at == NOW        # 完成 is not a new reaction
    assert list_suggestions(pool, "u1", placement="today", page_id=None, now=NOW) == []
    with pool.connection() as conn:
        assert store.get_counters(conn, "u1", "review_due") == c        # no counter change


def test_done_only_follows_a_started_card(pool):
    put(pool, "a")
    put(pool, "b")
    with pytest.raises(BotConflict):
        react(pool, "u1", "a", "done", now=NOW)                         # still pending
    react(pool, "u1", "b", "not_interested", now=NOW)
    with pytest.raises(BotConflict):
        react(pool, "u1", "b", "done", now=NOW)
    assert get(pool, "a").status == "pending"
    react(pool, "u1", "a", "accepted", now=NOW)
    react(pool, "u1", "a", "done", now=NOW)
    with pytest.raises(BotConflict):
        react(pool, "u1", "a", "done", now=NOW)                         # once


def test_a_started_card_leaves_the_list_with_its_day_but_stays_accepted(pool):
    put(pool, "a")
    react(pool, "u1", "a", "accepted", now=NOW)
    later = END + timedelta(minutes=1)
    assert list_suggestions(pool, "u1", placement="today", page_id=None, now=later) == []
    assert get(pool, "a").status == "accepted"                # the 开始 is not erased
    with pytest.raises(BotConflict, match="这张卡片已过期"):
        react(pool, "u1", "a", "done", now=later)


def test_react_errors(pool):
    put(pool, "a")
    put(pool, "x", expires_at=NOW)
    put(pool, "o", uid="u2")
    with pytest.raises(LookupError):
        react(pool, "u1", "nope", "accepted", now=NOW)
    with pytest.raises(LookupError):
        react(pool, "u1", "o", "accepted", now=NOW)
    with pytest.raises(BotConflict):
        react(pool, "u1", "x", "accepted", now=NOW)
    react(pool, "u1", "a", "not_interested", now=NOW)
    for reaction in ("accepted", "done"):
        with pytest.raises(BotConflict):
            react(pool, "u1", "a", reaction, now=NOW)
    with pool.connection() as conn:
        assert store.get_counters(conn, "u1", "review_due").not_interested == 1


def test_badge_counts_unreacted_cards_created_today(pool):
    put(pool, "a")                                                     # pending, today
    put(pool, "b", status="shown")
    put(pool, "c", status="not_interested")
    put(pool, "d", status="accepted")
    put(pool, "e", placement="page", page_id="pg")
    put(pool, "f", created_at=NOW - timedelta(days=1), expires_at=NOW + timedelta(hours=1))
    put(pool, "g", expires_at=NOW)
    put(pool, "o", uid="u2")
    assert badge(pool, "u1", now=NOW) == (2, 0)


def test_mark_shown_never_overwrites_a_reaction(pool):
    put(pool, "a")
    react(pool, "u1", "a", "accepted", now=NOW)      # committed after a list read it as pending
    with pool.connection() as conn:
        assert store.mark_shown(conn, "a") is False
    assert get(pool, "a").status == "accepted"
    put(pool, "b")
    with pool.connection() as conn:
        assert store.mark_shown(conn, "b") is True
