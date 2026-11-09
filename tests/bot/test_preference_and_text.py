"""Task 16: the 偏好 ledger row of a 不感兴趣 click (and its undo), the titles / gate details the
pipeline persists, and what why / recent show of them. Fakes only."""
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.bot.actions import ActionLedger, NotDecidable
from evowiki.bot.executors import PreferenceExecutor
from evowiki.bot.pipeline import dispatch
from evowiki.bot.suggestions import (
    BotConflict,
    list_suggestions,
    preference_ids,
    react,
    review_react,
    review_reminder,
)
from evowiki.bot.suppression import suppressed_until
from evowiki.bot.why import recent, why
from evowiki.store import bot as store
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)     # 17:00 in Asia/Shanghai
END = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)    # next local midnight
_n = iter(range(100_000))


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


class Clock:
    now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    clock = Clock()
    ledger = ActionLedger(pool, clock, new_id, {"preference": PreferenceExecutor(pool, clock)})
    return pool, ledger, clock


def put(pool, sid, *, dedup="review_due", **kw):
    base = {"suggestion_id": sid, "user_id": "u1", "kind": "review_due", "title": "复习 2 张到期的卡",
            "why": "w", "action": {"type": "start_practice"}, "placement": "today", "priority": 1,
            "dedup_key": dedup, "expires_at": END, "created_at": NOW - timedelta(hours=1)}
    with pool.connection() as conn:
        store.insert_suggestion(conn, store.SuggestionRow(**{**base, **kw}))


def card(pool, sid):
    with pool.connection() as conn:
        return store.lock_suggestion(conn, sid)


def counters(pool, key="review_due"):
    with pool.connection() as conn:
        return store.get_counters(conn, "u1", key)


def pref_row(pool, sid):
    with pool.connection() as conn:
        return store.get_action_by_key(conn, f"pref:{sid}")


# --- preference rows --------------------------------------------------------------------------

def test_not_interested_writes_a_preference_row_in_the_same_transaction(env):
    pool, ledger, _ = env
    put(pool, "a")
    react(pool, "u1", "a", "not_interested", now=NOW)
    row = pref_row(pool, "a")
    assert (row.source, row.kind, row.scope, row.level, row.status) == (
        "bot", "preference", "preference", "auto_undo", "executed")
    assert row.summary == "以后少提醒「复习 2 张到期的卡」"
    assert row.why == "你点了「不感兴趣」"
    assert row.content == {"suggestion_id": "a", "dedup_key": "review_due"}
    assert preference_ids(pool, "u1", ["a", "zz"]) == {"a": row.action_id}
    assert [a.kind for a in ledger.list("u1")] == ["preference"]


def test_a_failed_reaction_leaves_no_preference_row(env):
    pool, _, _ = env
    put(pool, "a", status="accepted")
    with pytest.raises(BotConflict):
        react(pool, "u1", "a", "not_interested", now=NOW)
    assert pref_row(pool, "a") is None


def test_undo_through_the_ledger_restores_counter_and_card(env):
    pool, ledger, _ = env
    put(pool, "a")
    react(pool, "u1", "a", "not_interested", now=NOW)
    row = pref_row(pool, "a")
    assert counters(pool).not_interested == 1 and card(pool, "a").status == "not_interested"
    out = ledger.undo("u1", row.action_id)
    assert out.status == "undone"
    c = counters(pool)
    assert (c.not_interested, c.last_not_interested_at) == (0, None)
    assert card(pool, "a").status == "shown" and card(pool, "a").reacted_at is None
    assert [r.suggestion_id for r in list_suggestions(
        pool, "u1", placement="today", page_id=None, now=NOW)] == ["a"]
    assert preference_ids(pool, "u1", ["a"]) == {}
    with pytest.raises(NotDecidable):
        ledger.undo("u1", row.action_id)                     # only once
    with pytest.raises(LookupError):
        ledger.undo("u2", row.action_id)                     # not another user's


def test_undo_takes_one_off_a_larger_counter_and_floors_at_zero(env):
    pool, ledger, _ = env
    put(pool, "a")
    put(pool, "b", status="shown")
    react(pool, "u1", "a", "not_interested", now=NOW)
    react(pool, "u1", "b", "not_interested", now=NOW + timedelta(minutes=1))
    ledger.undo("u1", pref_row(pool, "b").action_id)
    c = counters(pool)
    assert c.not_interested == 1 and c.last_not_interested_at is not None
    with pool.connection() as conn:                       # something else already reset it
        store.put_counters(conn, "u1", "review_due", store.Counters())
    ledger.undo("u1", pref_row(pool, "a").action_id)
    assert counters(pool).not_interested == 0


def test_undo_of_an_expired_card_leaves_it_alone(env):
    pool, ledger, clock = env
    put(pool, "a")
    react(pool, "u1", "a", "not_interested", now=NOW)
    clock.now = END + timedelta(minutes=1)
    ledger.undo("u1", pref_row(pool, "a").action_id)
    assert card(pool, "a").status == "not_interested"
    assert counters(pool).not_interested == 0


def test_the_change_list_shows_the_preference_row_and_undoes_it(env):
    from evowiki.bot.change_source import LedgerChangeSource
    pool, ledger, _ = env
    put(pool, "a")
    react(pool, "u1", "a", "not_interested", now=NOW)
    src = LedgerChangeSource(ledger)
    [ch] = src.list("u1")
    assert ch.summary.startswith("以后少提醒「") and ch.can_undo and ch.status == "personal_accepted"
    assert src.undo("u1", ch.patch_id, now=NOW).status == "rolled_back"
    assert card(pool, "a").status == "shown"


# --- titles and gate details ------------------------------------------------------------------

def trig(tid, *, kind="review_due", route="direct", dedup=None, **kw):
    return store.TriggerRow(
        trigger_id=tid, user_id="u1", kind=kind, rule_id=kind, rule_version=1, source_key=tid,
        dedup_key=dedup or kind, route=route, evidence=[], payload={"count": 2}, priority=50,
        reason=f"因为{tid}", created_at=NOW, **kw)


def put_trigger(pool, row):
    with pool.connection() as conn:
        store.insert_trigger(conn, row)


def get_trigger(pool, tid):
    with pool.connection() as conn:
        return store.get_trigger(conn, tid)


def test_dispatch_persists_the_gate_detail_for_every_outcome(env):
    pool, _, _ = env
    put_trigger(pool, trig("t1", title="复习 2 张到期的卡"))
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    t = get_trigger(pool, "t1")
    assert t.title == "复习 2 张到期的卡"
    assert t.gate_detail == "今日第 1 / 5 项"
    put_trigger(pool, trig("t2"))
    dispatch(pool, "t2", now=NOW + timedelta(hours=1), new_id=new_id)       # inside the cooldown
    # a daily kind counts from its slot (08:00 local), not from when the check ran: + 20 h
    assert get_trigger(pool, "t2").gate_detail == "10月15日 04:00 之后再提醒"


def test_an_empty_card_says_so(env):
    pool, _, _ = env
    put_trigger(pool, store.TriggerRow(**{**trig("t1").__dict__, "payload": {"count": 0}}))
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    t = get_trigger(pool, "t1")
    assert (t.gate_reason, t.gate_detail) == ("empty", "没有要做的")


def test_why_and_recent_expose_the_detail_and_title(env):
    pool, _, _ = env
    put_trigger(pool, trig("t1", title="复习 2 张到期的卡"))
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    out = why(pool, "u1", trigger_id="t1")
    assert out.gate.detail == "今日第 1 / 5 项"
    [a] = recent(pool, "u1", now=NOW, include_skipped=True)
    assert (a.title, a.gate_detail) == ("安排了今日复习（2 张卡片）", out.gate.detail)


def test_recent_lists_the_title_of_skipped_rows_too(env):
    pool, _, _ = env
    put_trigger(pool, trig("t1", title="复习 2 张到期的卡"))
    put_trigger(pool, trig("t2", title="再复习一次"))
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    dispatch(pool, "t2", now=NOW, new_id=new_id)                     # cooldown
    rows = {a.trigger_id: a for a in recent(pool, "u1", now=NOW, include_skipped=True)}
    assert rows["t2"].result == "skipped" and rows["t2"].title == "再复习一次"
    assert rows["t2"].gate_detail.endswith("之后再提醒")


def test_declining_again_after_an_undo_reuses_the_row(env):
    pool, ledger, _ = env
    put(pool, "a")
    react(pool, "u1", "a", "not_interested", now=NOW)
    first = pref_row(pool, "a")
    ledger.undo("u1", first.action_id)
    react(pool, "u1", "a", "not_interested", now=NOW + timedelta(minutes=5))
    again = pref_row(pool, "a")
    assert (again.action_id, again.status) == (first.action_id, "executed")
    assert preference_ids(pool, "u1", ["a"]) == {"a": first.action_id}
    assert counters(pool).not_interested == 1


# ---- 今日复习: 减少提醒 without a suggestion, its cooldown and its undo ----

BASE = timedelta(hours=20)      # the default cooldown_hours


def less_rows(pool):
    with pool.connection() as conn:
        rows = conn.execute("select action_id from agent_action where user_id = 'u1' "
                            "and summary = '以后少提醒复习' order by created_at").fetchall()
    return [r["action_id"] for r in rows]


def hidden(pool, at):
    return review_reminder(pool, "u1", now=at).hidden


def test_review_less_records_without_a_suggestion_and_doubles(env):
    pool, ledger, _ = env
    r = review_react(pool, "u1", "not_interested", now=NOW)
    assert r.hidden and r.preference_action_id == less_rows(pool)[0]
    row = ledger.get("u1", r.preference_action_id)
    assert row.status == "executed" and row.why == "你点了「减少提醒」"
    assert hidden(pool, NOW + 2 * BASE - timedelta(minutes=1))
    t1 = NOW + 2 * BASE                                   # 40 h after one click: back
    assert not hidden(pool, t1)
    review_react(pool, "u1", "not_interested", now=t1)
    assert hidden(pool, t1 + 4 * BASE - timedelta(minutes=1)) and not hidden(pool, t1 + 4 * BASE)
    t2 = t1 + 4 * BASE
    review_react(pool, "u1", "not_interested", now=t2)
    assert hidden(pool, t2 + 8 * BASE - timedelta(minutes=1)) and not hidden(pool, t2 + 8 * BASE)


def test_review_less_reacts_on_an_open_suggestion_and_its_undo_restores_it(env):
    pool, ledger, _ = env
    put(pool, "a")
    r = review_react(pool, "u1", "not_interested", now=NOW)
    assert card(pool, "a").status == "not_interested" and counters(pool).not_interested == 1
    assert ledger.undo("u1", r.preference_action_id).status == "undone"
    assert card(pool, "a").status == "shown"
    assert not hidden(pool, NOW) and counters(pool).not_interested == 0


def test_review_less_after_a_started_suggestion_still_records(env):
    pool, _, _ = env
    put(pool, "a")
    review_react(pool, "u1", "accepted", now=NOW)
    assert card(pool, "a").status == "accepted"
    assert review_react(pool, "u1", "not_interested", now=NOW).hidden
    assert card(pool, "a").status == "accepted"


def test_starting_a_review_resets_the_cooldown(env):
    pool, _, _ = env
    review_react(pool, "u1", "not_interested", now=NOW)
    assert not review_react(pool, "u1", "accepted", now=NOW + timedelta(minutes=1)).hidden
    assert not hidden(pool, NOW + timedelta(minutes=2))
    assert counters(pool).not_interested == 0


def test_undo_of_the_newest_click_gives_the_earlier_cooldown_back(env):
    pool, ledger, _ = env
    review_react(pool, "u1", "not_interested", now=NOW)
    t1 = NOW + 2 * BASE
    second = review_react(pool, "u1", "not_interested", now=t1).preference_action_id
    assert hidden(pool, t1)
    ledger.undo("u1", second)
    c = counters(pool)
    assert c.not_interested == 1 and c.last_not_interested_at == NOW
    assert not hidden(pool, t1)                           # the first click's 40 h are over: back


def test_undo_of_an_older_click_leaves_the_newest_in_force(env):
    pool, ledger, _ = env
    first = review_react(pool, "u1", "not_interested", now=NOW).preference_action_id
    t1 = NOW + 2 * BASE
    review_react(pool, "u1", "not_interested", now=t1)
    ledger.undo("u1", first)
    c = counters(pool)
    assert c.not_interested == 1 and c.last_not_interested_at == t1


def test_undo_of_a_click_from_before_a_started_review_takes_nothing_from_a_later_one(env):
    pool, ledger, _ = env
    old = review_react(pool, "u1", "not_interested", now=NOW).preference_action_id
    t1 = NOW + timedelta(hours=1)
    review_react(pool, "u1", "accepted", now=t1)
    review_react(pool, "u1", "not_interested", now=t1)
    assert ledger.undo("u1", old).status == "undone"
    assert counters(pool).not_interested == 1 and hidden(pool, t1)


def test_the_bot_gate_reads_the_same_counter(env):
    pool, _, _ = env
    review_react(pool, "u1", "not_interested", now=NOW)
    with pool.connection() as conn:
        c = store.get_counters(conn, "u1", "review_due")
    assert suppressed_until(c, BASE) == NOW + 2 * BASE
