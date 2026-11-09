"""The "why" chain and the recent-activity list (service layer and routes). Fakes only."""
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.bot import runs
from evowiki.bot.pipeline import dispatch
from evowiki.bot.suggestions import react
from evowiki.bot.why import recent, why
from evowiki.store import bot as store
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
_n = iter(range(100_000))


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


def trig(tid, *, uid="u1", kind="review_due", route="direct", created=NOW, dedup=None,
         payload=None):
    return store.TriggerRow(
        trigger_id=tid, user_id=uid, kind=kind, rule_id=kind, rule_version=2, source_key=tid,
        dedup_key=dedup or tid, route=route, evidence=["ev1"],
        payload=payload if payload is not None else {"count": 2}, priority=50,
        reason=f"因为{tid}", created_at=created)


def put(pool, row):
    with pool.connection() as conn:
        store.insert_trigger(conn, row)


def gate(pool, tid, now=NOW):
    return dispatch(pool, tid, now=now, new_id=new_id)


@pytest.fixture
def two(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")


def test_why_of_a_direct_suggestion_after_reacting(pool, two):
    put(pool, trig("t1"))
    d = gate(pool, "t1")
    react(pool, "u1", d.suggestion_id, "accepted", now=NOW + timedelta(minutes=5))
    out = why(pool, "u1", suggestion_id=d.suggestion_id)
    assert (out.trigger.kind, out.trigger.rule_id, out.trigger.rule_version, out.trigger.reason,
            out.trigger.evidence) == ("review_due", "review_due", 2, "因为t1", ["ev1"])
    assert (out.gate.outcome, out.gate.reason) == ("pass", None)
    assert [(o.type, o.id) for o in out.outputs] == [("suggestion", d.suggestion_id)]
    assert (out.reaction.status, out.reaction.reacted_at) == (
        "accepted", NOW + timedelta(minutes=5))
    assert why(pool, "u1", trigger_id="t1") == out


def test_why_of_a_skipped_trigger(pool, two):
    put(pool, trig("t1", dedup="k"))
    put(pool, trig("t2", dedup="k"))
    gate(pool, "t1")
    gate(pool, "t2")                                   # cooldown
    out = why(pool, "u1", trigger_id="t2")
    assert (out.gate.outcome, out.gate.reason) == ("skip", "cooldown")
    assert out.outputs == [] and out.reaction is None


def test_why_lists_the_actions_of_a_trigger(pool, two):
    put(pool, trig("t1"))
    d = gate(pool, "t1")
    with pool.connection() as conn:
        store.insert_action(conn, store.ActionRow(
            action_id="act_1", user_id="u1", kind="x", scope="suggestion", level="auto",
            status="executed", source="bot", summary="做了一件事", content={}, content_hash="h",
            idempotency_key="k1", created_at=NOW, trigger_id="t1"))
    out = why(pool, "u1", trigger_id="t1")
    assert [(o.type, o.id) for o in out.outputs] == [
        ("suggestion", d.suggestion_id), ("action", "act_1")]


def test_why_validation_and_ownership(pool, two):
    put(pool, trig("t1"))
    d = gate(pool, "t1")
    with pytest.raises(ValueError):
        why(pool, "u1")
    with pytest.raises(ValueError):
        why(pool, "u1", suggestion_id=d.suggestion_id, trigger_id="t1")
    for kw in ({"suggestion_id": "nope"}, {"trigger_id": "nope"}):
        with pytest.raises(LookupError):
            why(pool, "u1", **kw)
    with pytest.raises(LookupError):
        why(pool, "u2", suggestion_id=d.suggestion_id)
    with pytest.raises(LookupError):
        why(pool, "u2", trigger_id="t1")


def test_why_of_a_card_without_a_trigger(pool, two):
    with pool.connection() as conn:
        store.insert_suggestion(conn, store.SuggestionRow(
            suggestion_id="s1", user_id="u1", kind="review_due", title="t", why="w",
            action={"type": "open_settings"}, placement="today", priority=90,
            dedup_key="review_due", expires_at=NOW + timedelta(hours=1), created_at=NOW))
    out = why(pool, "u1", suggestion_id="s1")
    assert out.trigger is None and out.gate is None
    assert [o.id for o in out.outputs] == ["s1"] and out.reaction.status == "pending"


def test_recent_results_and_skipped_filter(pool, two):
    put(pool, trig("t1", dedup="a", created=NOW - timedelta(hours=5)))             # suggested
    put(pool, trig("t2", dedup="a", created=NOW - timedelta(hours=4)))             # skipped
    put(pool, trig("t3", kind="strategy_negative", route="rsi", dedup="c",
                   created=NOW - timedelta(hours=3)))                              # handed off
    put(pool, trig("t5", dedup="e", created=NOW - timedelta(hours=1)))             # failed
    put(pool, trig("t6", dedup="f", created=NOW - timedelta(days=30)))             # outside window
    put(pool, trig("t7", uid="u2", dedup="g"))
    for t in ("t1", "t2", "t3"):
        gate(pool, t)
    with pool.connection() as conn:
        store.set_trigger_gate(conn, "t5", status="failed", outcome=None, reason=None, at=NOW)
    with pool.connection() as conn:
        conn.execute("update bot_suggestion set cost_usd = 0.5 where trigger_id = 't1'")
    shown = recent(pool, "u1", now=NOW)
    assert [(a.trigger_id, a.result) for a in shown] == [
        ("t3", "handed_to_rsi"), ("t1", "suggested")]                            # t5 failed: hidden
    t1 = shown[-1]
    assert (t1.suggestion_title, t1.suggestion_status, t1.cost_usd, t1.gate_outcome) == (
        "复习 2 张卡片", "pending", 0.5, "pass")
    assert t1.page_id is None                                   # a review card is about no page
    with pool.connection() as conn:
        conn.execute("update bot_suggestion set page_id = 'pg1' where trigger_id = 't1'")
    assert recent(pool, "u1", now=NOW)[-1].page_id == "pg1"     # 活动记录 opens its card's page
    everything = recent(pool, "u1", now=NOW, include_skipped=True)
    assert [(a.trigger_id, a.result, a.gate_reason) for a in everything
            if a.trigger_id == "t2"] == [("t2", "skipped", "cooldown")]
    assert len(everything) == 3 and all(a.trigger_id != "t5" for a in everything)


def test_recent_deferred_counts_as_skipped_and_a_failed_run_is_not_listed(pool, two):
    put(pool, trig("t1", kind="source_added"))
    with pool.connection() as conn:
        store.set_trigger_gate(conn, "t1", status="new", outcome="defer", reason="cap_reached",
                               at=NOW)
    runs.start_run(pool, run_id="c1", user_id="u1", kind="check", trigger_id=None,
                   now=NOW - timedelta(minutes=10))
    runs.finish_run(pool, "c1", status="failed", now=NOW, detail="scan exploded")
    assert recent(pool, "u1", now=NOW) == []           # a failed run is a fault, not an activity
    rows = recent(pool, "u1", now=NOW, include_skipped=True)
    assert [(a.kind, a.result) for a in rows] == [("source_added", "skipped")]
    assert recent(pool, "u2", now=NOW) == []


def test_why_of_a_bot_rsi_trigger_lists_the_changes_its_cycle_made(pool, two):
    """B5c: the chain ends in the RSI change(s) of the Bot's own cycle (参数 row ids)."""
    from psycopg.types.json import Jsonb
    put(pool, trig("t1", kind="strategy_negative", route="rsi", payload={"signals": []}))
    gate(pool, "t1")
    with pool.connection() as conn:
        conn.execute("insert into rsi_patch (patch_id, user_id, track, status, created_at, body) "
                     "values ('pb_1', 'u1', 'B', 'personal_accepted', %s, %s)",
                     (NOW, Jsonb({"why": "三次都说没懂：先举例再给定义"})))
        for uid, tid in (("u1", "t1"), ("u1", "other"), ("u2", "t1")):
            conn.execute("insert into rsi_cycle (user_id, at, trigger, result, cost_usd) "
                         "values (%s, %s, 'bot', %s, 0)",
                         (uid, NOW, Jsonb({"bot_trigger_id": tid, "patch_ids": ["pb_1"]})))
    out = why(pool, "u1", trigger_id="t1")
    assert [(o.type, o.id, o.kind, o.title, o.status) for o in out.outputs] == [
        ("rsi_change", "pb_1", "rsi_B", "三次都说没懂：先举例再给定义", "personal_accepted")]
    assert [a.result for a in recent(pool, "u1", now=NOW)] == ["handed_to_rsi"]
