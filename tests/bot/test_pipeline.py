from datetime import UTC, date, datetime, timedelta

import pytest

from evowiki.belief.aspects import DefineConcept, Observe
from evowiki.bot import pipeline
from evowiki.bot.facts import new_event_ids
from evowiki.bot.pipeline import dispatch, run_daily, scan_events, undispatched
from evowiki.bot.schedule import Slot
from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.bot.suppression import Counters
from evowiki.goals.contracts import Aspect, Observation
from evowiki.store import bot as store
from evowiki.wikiapp.events import PageOpened, PathRecord, PathSaved, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import make_user
from tests.wikiapp.test_k1_state import lp

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)     # 17:00 in Asia/Shanghai
_n = iter(range(100_000))


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


@pytest.fixture
def repo(pool):
    make_user(pool, "u1")
    r = PgRepo(pool)
    r.commit(r.load("u1"), Commit(aspect=[DefineConcept(
        event_id="ae_def", ts=NOW - timedelta(days=30), concept_id="c1",
        aspects=(Aspect.INTERPRET,))]))
    return r


def wrong(repo, n):
    evs = [Observe(event_id=f"ae_w{i}", ts=NOW - timedelta(hours=3 - i), observation=Observation(
        cell="c1.interpret", polarity="~", strength=0.8, kind="check",
        evidence_ref=f"check:pg_1:s{i}")) for i in range(n)]
    repo.commit(repo.load("u1"), Commit(aspect=evs))


def trig(tid="t1", *, kind="review_due", route="direct", key=None, dedup="review_due",
         payload=None, created=NOW, **kw):
    base = {"count": 2, "cells": [], "page_id": None} if kind == "review_due" else {}
    return store.TriggerRow(
        trigger_id=tid, user_id="u1", kind=kind, rule_id=kind, rule_version=1,
        source_key=key or tid, dedup_key=dedup, route=route, evidence=[],
        payload=payload if payload is not None else base, priority=50, reason="原因",
        created_at=created, **kw)


def put(pool, row):
    with pool.connection() as conn:
        assert store.insert_trigger(conn, row) == row.trigger_id


def get(pool, tid):
    with pool.connection() as conn:
        return store.get_trigger(conn, tid)


def n_sugg(pool):
    with pool.connection() as conn:
        return len(store.suggestions(conn, "u1"))


def test_scan_advances_cursor_and_answers_without_an_available_page_make_no_trigger(pool, repo):
    wrong(repo, 3)
    with pool.connection() as conn:
        _, wmax, amax = new_event_ids(conn, "u1", 0, 0)
    assert scan_events(pool, "u1", now=NOW, new_id=new_id) == []
    with pool.connection() as conn:
        assert store.lock_cursor(conn, "u1", NOW) == (wmax, amax)


def test_scan_cursor_and_intents_are_atomic(pool, repo, monkeypatch):
    wrong(repo, 3)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(pipeline.store_bot, "set_cursor", boom)
    with pytest.raises(RuntimeError):
        scan_events(pool, "u1", now=NOW, new_id=new_id)
    with pool.connection() as conn:
        assert store.lock_cursor(conn, "u1", NOW) == (0, 0)
        assert store.new_triggers(conn, NOW + timedelta(days=1)) == []


def test_dispatch_direct_makes_one_suggestion_and_is_idempotent(pool, repo):
    put(pool, trig())
    d = dispatch(pool, "t1", now=NOW, new_id=new_id)
    assert (d.outcome, d.status) == ("pass", "gated") and d.suggestion_id
    with pool.connection() as conn:
        [s] = store.suggestions(conn, "u1")
    assert s.title == "复习 2 张卡片" and s.action == {"type": "start_practice"}
    assert (s.status, s.why, s.trigger_id) == ("pending", "原因", "t1")
    assert s.expires_at == datetime(2026, 10, 14, 16, 0, tzinfo=UTC)   # end of local day
    again = dispatch(pool, "t1", now=NOW, new_id=new_id)
    assert again.outcome is None and again.status == "gated" and n_sugg(pool) == 1


def test_cap_reached(pool, repo):
    with pool.connection() as conn:
        for i in range(5):
            store.insert_suggestion(conn, store.SuggestionRow(
                suggestion_id=f"s{i}", user_id="u1", kind="goal_idle", title="t", why="w",
                action={"type": "open_settings"}, placement="today", priority=1,
                dedup_key=f"other{i}", expires_at=NOW + timedelta(hours=5),
                created_at=NOW - timedelta(hours=1)))
    put(pool, trig())
    d = dispatch(pool, "t1", now=NOW, new_id=new_id)
    assert (d.outcome, d.reason) == ("skip", "cap_reached") and n_sugg(pool) == 5
    t = get(pool, "t1")
    assert (t.status, t.gate_outcome, t.gate_reason) == ("gated", "skip", "cap_reached")


def test_paused_and_kind_off(pool, repo):
    put(pool, trig("t1"))
    put(pool, trig("t2", kind="goal_idle", dedup="goal_idle:g", payload={"title": "G"}))
    with pool.connection() as conn:
        store.put_settings(conn, "u1", DEFAULT_SETTINGS.model_copy(update={"paused": "always"}), NOW)
    assert dispatch(pool, "t1", now=NOW, new_id=new_id).reason == "paused"
    with pool.connection() as conn:
        kinds = {**DEFAULT_SETTINGS.kinds, "goal_idle": False}
        store.put_settings(conn, "u1", DEFAULT_SETTINGS.model_copy(update={"kinds": kinds}), NOW)
    assert dispatch(pool, "t2", now=NOW, new_id=new_id).reason == "kind_off"
    assert n_sugg(pool) == 0


def test_not_interested_suppresses(pool, repo):
    with pool.connection() as conn:
        store.put_counters(conn, "u1", "review_due", Counters(
            not_interested=1, last_not_interested_at=NOW - timedelta(hours=1)))
    put(pool, trig())
    d = dispatch(pool, "t1", now=NOW, new_id=new_id)
    assert (d.outcome, d.reason) == ("skip", "suppressed") and n_sugg(pool) == 0


def test_rsi_route_awaits_its_rsi_run(pool, repo):
    """B5c: the route `rsi` waits for the Bot's own RSI run (tests/bot/test_pipeline_rsi.py)."""
    put(pool, trig("t1", kind="strategy_negative", route="rsi", dedup="strategy_negative"))
    d = dispatch(pool, "t1", now=NOW, new_id=new_id)
    assert (d.outcome, d.status, d.suggestion_id) == ("pass", "awaiting_rsi", None)


def test_empty_card_is_skipped(pool, repo):
    put(pool, trig("t1", payload={"count": 0, "cells": [], "page_id": None}))
    d = dispatch(pool, "t1", now=NOW, new_id=new_id)
    assert (d.outcome, d.reason) == ("skip", "empty") and n_sugg(pool) == 0


def test_card_for_goal_and_question():
    g = trig("g", kind="goal_idle", payload={"path_id": "pa", "title": "学统计", "page_id": "p1"})
    c = pipeline.card_for(g, DEFAULT_SETTINGS, NOW)
    assert (c.title, c.action) == ("学统计", {"type": "open_page", "page_id": "p1"})
    assert c.why == "有一阵没看了，接着上次往下学"
    g3 = trig("g3", kind="goal_idle", payload={"path_id": "pa", "title": "继续：学统计（2/5）",
                                                 "page_id": "p1", "days": 3})
    c3 = pipeline.card_for(g3, DEFAULT_SETTINGS, NOW)
    assert (c3.title, c3.why) == ("学统计", "三天没看了，接着上次往下学")
    g2 = trig("g2", kind="goal_idle", payload={"path_id": "pa", "title": "学统计", "page_id": None})
    assert pipeline.card_for(g2, DEFAULT_SETTINGS, NOW).action == {
        "type": "open_goal", "path_id": "pa"}
    q = trig("q", kind="open_question",
             payload={"page_id": "p1", "text": "为什么", "session_id": "s"})
    c = pipeline.card_for(q, DEFAULT_SETTINGS, NOW)
    assert (c.title, c.why) == ("为什么", "上次在这一页还没弄懂")
    assert c.action == {"type": "open_page", "page_id": "p1", "ask": "为什么"}


def test_undispatched_lists_only_old_new_triggers(pool, repo):
    put(pool, trig("old", created=NOW - timedelta(seconds=61)))
    put(pool, trig("fresh", created=NOW - timedelta(seconds=30)))
    put(pool, trig("done", created=NOW - timedelta(minutes=5)))
    dispatch(pool, "done", now=NOW, new_id=new_id)
    assert undispatched(pool, now=NOW) == [("u1", "old")]


def test_the_sweep_skips_a_user_whose_dispatch_kind_is_paused(pool, repo):
    put(pool, trig("old", created=NOW - timedelta(seconds=61)))
    with pool.connection() as conn:
        store.put_kind_pause(conn, "u1", "dispatch", NOW + timedelta(hours=1))
    assert undispatched(pool, now=NOW) == []
    assert undispatched(pool, now=NOW + timedelta(hours=2)) == [("u1", "old")]   # pause over


def test_run_daily_twice_same_slot(pool, repo):
    rec = PathRecord(path=lp(2), page_ids=("pg_1", None),
                     created_at=NOW - timedelta(days=5))
    repo.commit(repo.load("u1"), Commit(wiki=[
        WikiEvent(event_id="wev_p", ts=NOW - timedelta(days=5),
                  body=PathSaved(record=rec, action="page_started")),
        WikiEvent(event_id="wev_o", ts=NOW - timedelta(days=4), body=PageOpened(page_id="pg_1"))]))
    slot = Slot(date(2026, 10, 14), NOW - timedelta(hours=1))
    tid, weekly = run_daily(pool, "u1", slot, now=NOW, new_id=new_id)
    assert get(pool, weekly).kind == "goal_review"           # the weekly review (B5a)
    t = get(pool, tid)
    assert (t.kind, t.source_key) == ("goal_idle", "goal_idle:path-k:2026-10-14")
    assert run_daily(pool, "u1", slot, now=NOW, new_id=new_id) == []


def test_dispatch_waits_for_the_users_advisory_lock(pool, repo, db_url):
    import threading
    import time

    import psycopg
    put(pool, trig())
    done = threading.Event()
    holder = psycopg.connect(db_url, autocommit=True)
    holder.execute("select pg_advisory_lock(hashtextextended('bot:u1', 0))")
    t = threading.Thread(target=lambda: (dispatch(pool, "t1", now=NOW, new_id=new_id),
                                         done.set()))
    t.start()
    time.sleep(0.5)
    blocked = not done.is_set()
    holder.execute("select pg_advisory_unlock(hashtextextended('bot:u1', 0))")
    t.join(10)
    holder.close()
    assert blocked and done.is_set() and n_sugg(pool) == 1


def test_card_for_failure_marks_trigger_failed(pool, repo, caplog):
    put(pool, trig("t1", kind="goal_idle", dedup="goal_idle:g", payload={"title": "G"}))
    d = dispatch(pool, "t1", now=NOW, new_id=new_id)         # no path_id nor page_id -> KeyError
    assert (d.outcome, d.status, d.suggestion_id) == (None, "failed", None)
    t = get(pool, "t1")
    assert (t.status, t.gate_outcome) == ("failed", None) and n_sugg(pool) == 0
    assert "card_for failed" in caplog.text
    assert undispatched(pool, now=NOW + timedelta(hours=1)) == []


def test_deferred_trigger_is_swept_after_60_seconds(pool, repo):
    put(pool, trig("t1", kind="source_added", dedup="source_added:src1"))
    with pool.connection() as conn:
        store.put_settings(conn, "u1", DEFAULT_SETTINGS.model_copy(update={"paused": "always"}), NOW)
    assert dispatch(pool, "t1", now=NOW, new_id=new_id).outcome == "defer"
    assert undispatched(pool, now=NOW + timedelta(seconds=30)) == []
    assert undispatched(pool, now=NOW + timedelta(seconds=61)) == [("u1", "t1")]


def test_source_added_card_and_kind_gate(pool, repo):
    payload = {"source_id": "src1", "title": "讲义", "pages": [
        {"page_id": "p1", "title": "特征值"}, {"page_id": "p2", "title": "对角化"}]}
    put(pool, trig("t1", kind="source_added", dedup="source_added:src1", payload=payload,
                   placement="today", title="《讲义》讲到了 2 页已有的内容"))
    put(pool, trig("t2", kind="source_added", dedup="source_added:src2", payload=payload))
    with pool.connection() as conn:
        kinds = {**DEFAULT_SETTINGS.kinds, "source_added": False}
        store.put_settings(conn, "u1", DEFAULT_SETTINGS.model_copy(update={"kinds": kinds}), NOW)
    assert dispatch(pool, "t2", now=NOW, new_id=new_id).reason == "kind_off"
    with pool.connection() as conn:
        store.put_settings(conn, "u1", DEFAULT_SETTINGS, NOW)
    assert dispatch(pool, "t1", now=NOW, new_id=new_id).outcome == "pass"
    with pool.connection() as conn:
        [s] = store.suggestions(conn, "u1")
    assert s.placement == "today" and s.title == "《讲义》讲到了 2 页已有的内容"
    assert s.action == {"type": "revise_from_source", "source_id": "src1",
                        "page_ids": ["p1", "p2"]}
