"""The Bot's DBOS layer on a throwaway database: tick / slots, check, daily, per-user
serialisation and the flood guard. Fakes only (no paid API)."""
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from dbos import DBOS, SetWorkflowID

from evowiki.agent import dbos_app
from evowiki.belief.aspects import DefineConcept, Observe
from evowiki.bot import durable
from evowiki.bot.durable import BotRuntime
from evowiki.bot.settings import ACTIVE_WINDOW
from evowiki.goals.contracts import Aspect, Observation
from evowiki.store import bot as store
from evowiki.wikiapp.events import PageOpened, PathRecord, PathSaved, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import make_user
from tests.wikiapp.test_k1_state import lp

NOW = datetime(2026, 10, 14, 0, 30, tzinfo=UTC)     # 08:30 in Asia/Shanghai: inside the window
_n = iter(range(100_000))


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def bot(db_url, pool, clock):
    make_user(pool, "u1")
    with pool.connection() as conn:
        store.mark_seen(conn, "u1", NOW)
    dbos_app.launch(db_url, app_version=dbos_app.APP_VERSION)
    durable.register(BotRuntime(pool, clock, new_id), schedule=False)
    yield
    dbos_app.shutdown()


def result(wid):
    return DBOS.retrieve_workflow(wid).get_result()


def tick(wid, at):
    with SetWorkflowID(wid):
        return durable.bot_tick(at, None)


def slots(pool):
    with pool.connection() as conn:
        rows = conn.execute("select slot_date::text as d, status, reason from bot_slot "
                            "order by slot_date").fetchall()
    return [(r["d"], r["status"], r["reason"]) for r in rows]


def n_triggers(pool):
    with pool.connection() as conn:
        return conn.execute("select count(*) as n from bot_trigger").fetchone()["n"]


def seed_idle_goal(pool):
    r = PgRepo(pool)
    rec = PathRecord(path=lp(2), page_ids=("pg_1", None),
                     created_at=NOW - timedelta(days=5))
    r.commit(r.load("u1"), Commit(wiki=[
        WikiEvent(event_id="wev_p", ts=NOW - timedelta(days=5),
                  body=PathSaved(record=rec, action="page_started")),
        WikiEvent(event_id="wev_o", ts=NOW - timedelta(days=4), body=PageOpened(page_id="pg_1"))]))


def seed_wrong(pool, n):
    r = PgRepo(pool)
    r.commit(r.load("u1"), Commit(aspect=[DefineConcept(
        event_id="ae_def", ts=NOW - timedelta(days=30), concept_id="c1",
        aspects=(Aspect.INTERPRET,))]))
    r.commit(r.load("u1"), Commit(aspect=[Observe(
        event_id=f"ae_w{i}", ts=NOW - timedelta(hours=3 - i), observation=Observation(
            cell="c1.interpret", polarity="~", strength=0.8, kind="check",
            evidence_ref=f"check:pg_1:s{i}")) for i in range(n)]))


def test_same_slot_twice_is_one_slot_and_one_set_of_triggers(bot, pool):
    seed_idle_goal(pool)
    first = tick("bot-test-tick-1", NOW)
    assert first == [["u1", "2026-10-13", "missed"], ["u1", "2026-10-14", "enqueued"]]
    assert tick("bot-test-tick-2", NOW) == []               # same scheduled_at, new workflow id
    done = result("bot:slot:daily:u1:2026-10-14")
    assert len(done) == 2 and n_triggers(pool) == 2     # goal_idle + the weekly review (B5a)
    # the same workflow id enqueued again hands back the first result, nothing re-runs
    h = durable.enqueue(durable.bot_daily, "u1", "bot:slot:daily:u1:2026-10-14",
                        "u1", "2026-10-14", NOW.isoformat())
    assert h.get_result() == done and n_triggers(pool) == 2
    assert [s for s in slots(pool) if s[0] == "2026-10-14"] == [("2026-10-14", "enqueued", "")]


def test_down_for_more_than_a_day_runs_today_and_marks_yesterday_missed(bot, pool, clock):
    clock.now = datetime(2026, 10, 16, 0, 30, tzinfo=UTC)   # local 2026-10-16 08:30
    out = tick("bot-test-late", clock.now)
    assert out == [["u1", "2026-10-15", "missed"], ["u1", "2026-10-16", "enqueued"]]
    assert slots(pool) == [("2026-10-15", "missed", "past_date"),
                           ("2026-10-16", "enqueued", "")]
    result("bot:slot:daily:u1:2026-10-16")
    assert DBOS.list_workflows(workflow_id_prefix="bot:slot:daily:u1:2026-10-15") == []


def idle_cards(pool) -> int:
    with pool.connection() as conn:
        return conn.execute("select count(*) as n from bot_suggestion where kind = 'goal_idle'"
                            ).fetchone()["n"]


def test_first_start_late_in_the_day_runs_today_once_and_next_morning_is_not_cooled(
        bot, pool, clock):
    seed_idle_goal(pool)
    clock.now = datetime(2026, 10, 14, 13, 0, tzinfo=UTC)   # local 2026-10-14 21:00, slot was 08:00
    assert tick("bot-test-evening", clock.now) == [["u1", "2026-10-13", "missed"],
                                                   ["u1", "2026-10-14", "enqueued"]]
    result("bot:slot:daily:u1:2026-10-14")
    n = n_triggers(pool)
    assert idle_cards(pool) == 1
    clock.now = datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
    assert tick("bot-test-evening-2", clock.now) == [] and n_triggers(pool) == n

    clock.now = datetime(2026, 10, 15, 0, 1, tzinfo=UTC)    # 08:01 next morning: 11 h later
    tick("bot-test-next-morning", clock.now)
    result("bot:slot:daily:u1:2026-10-15")
    assert idle_cards(pool) == 2


def test_one_users_checks_never_overlap(bot, pool, monkeypatch):
    make_user(pool, "u2")                    # bot_run rows need a real user
    spans, lock = [], threading.Lock()

    def slow_scan(pool, user_id, *, now, new_id):
        t0 = time.monotonic()
        time.sleep(0.3)
        with lock:
            spans.append((user_id, t0, time.monotonic()))
        return []

    monkeypatch.setattr(durable.pipeline, "scan_events", slow_scan)
    hs = [durable.enqueue(durable.bot_check, u, f"bot:check:{u}:{i}", u)
          for i, u in enumerate(("u1", "u1", "u1", "u2"))]
    for h in hs:
        h.get_result()
    u1 = [s for s in spans if s[0] == "u1"]
    assert len(u1) == 3
    assert not any(a[1] < b[2] and b[1] < a[2] for i, a in enumerate(u1) for b in u1[i + 1:])
    u2 = next(s for s in spans if s[0] == "u2")
    assert any(u2[1] < a[2] and a[1] < u2[2] for a in u1)


def test_step_error_marks_the_workflow_error(bot, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(durable.pipeline, "scan_events", boom)
    h = durable.enqueue(durable.bot_check, "u1", "bot:check:u1:err", "u1")
    with pytest.raises(Exception, match="db down"):
        h.get_result()
    assert DBOS.retrieve_workflow("bot:check:u1:err").get_status().status == "ERROR"


def test_notify_enqueues_once_per_state(bot, pool):
    seed_wrong(pool, 1)
    durable.notify("u1")
    durable.notify("u1")
    ws = DBOS.list_workflows(workflow_id_prefix="bot:check:u1:")
    assert len(ws) == 1
    DBOS.retrieve_workflow(ws[0].workflow_id).get_result()


def test_notify_flood_guard_drops_the_sixth_wakeup(bot, pool):
    with pool.connection() as conn:
        for i in range(5):
            store.record_check(conn, "u1", NOW - timedelta(seconds=10 + i), dropped=False)
    durable.notify("u1")
    assert DBOS.list_workflows(workflow_id_prefix="bot:check:") == []
    with pool.connection() as conn:
        rows = conn.execute("select dropped from bot_check order by id").fetchall()
    assert [r["dropped"] for r in rows] == [False] * 5 + [True]


def test_tick_every_ten_minutes_sweeps(bot, pool, clock):
    seed_wrong(pool, 3)                                      # repeated wrong answers make no trigger
    at = datetime(2026, 10, 14, 3, 10, tzinfo=UTC)
    clock.now = at
    tick("bot-test-sweep-0", at.replace(minute=11))          # no sweep on minute 11
    assert DBOS.list_workflows(workflow_id_prefix="bot:sweep:") == []
    with pool.connection() as conn:
        # a trigger the check never dispatched is dispatched by the sweep
        store.insert_trigger(conn, store.TriggerRow(
            trigger_id="t_new", user_id="u1", kind="review_due", rule_id="review_due",
            rule_version=1, source_key="t_new", dedup_key="review_due", route="direct",
            evidence=[], payload={"count": 2, "cells": [], "page_id": None}, priority=50,
            reason="原因", created_at=at - timedelta(minutes=5)))
    tick("bot-test-sweep-1", at)
    assert result("bot:sweep:u1:20261014T0310") == []
    result("bot:dispatch:t_new:20261014T0310")
    with pool.connection() as conn:
        assert store.get_trigger(conn, "t_new").status == "gated"


def test_register_schedule_is_idempotent_and_fires_a_tick(bot, pool):
    rt = durable._rt()
    durable.register(rt)
    durable.register(rt)
    scheds = [s for s in DBOS.list_schedules() if s["schedule_name"] == "bot:tick"]
    assert len(scheds) == 1 and scheds[0]["schedule"] == "* * * * *"
    assert scheds[0]["automatic_backfill"] is False
    DBOS.trigger_schedule("bot:tick").get_result()           # a datetime crosses the serializer
    assert [s[0] for s in slots(pool)] == ["2026-10-13", "2026-10-14"]


def test_route_tick_wakes_the_bot_only_when_wired():
    from types import SimpleNamespace

    from fastapi import BackgroundTasks

    from evowiki.service.routes_wiki import _tick
    seen = []
    on = SimpleNamespace(bot_notify=seen.append, pool=None)
    off = SimpleNamespace(bot_notify=None, pool=None)
    t1, t2 = BackgroundTasks(), BackgroundTasks()
    _tick(t1, on, "u1")
    _tick(t2, off, "u1")
    assert [t.func for t in t1.tasks][-1] == seen.append and len(t1.tasks) == len(t2.tasks) + 1


def test_tick_enqueues_a_daily_whose_slot_row_exists_without_a_workflow(bot, pool):
    """A `_due_slots` re-run after its commit finds the row already there: still enqueue."""
    with pool.connection() as conn:
        store.record_slot(conn, "u1", NOW.date(), "enqueued", "",
                          datetime(2026, 10, 14, 0, 0, tzinfo=UTC), NOW)
    assert DBOS.list_workflows(workflow_id_prefix="bot:slot:daily:") == []
    assert tick("bot-test-lost", NOW) == [["u1", "2026-10-13", "missed"]]
    result("bot:slot:daily:u1:2026-10-14")
    assert [s for s in slots(pool) if s[0] == "2026-10-14"] == [("2026-10-14", "enqueued", "")]


# --- a Bot-off process, stale ticks, paused users ---------------------------------------------

def test_bot_off_process_skips_every_bot_workflow_with_one_log_line(bot, pool, caplog):
    """DBOS runs the shared `bot:tick` schedule and dequeues the `bot` queue in every launched
    process; with no Bot runtime installed (EVOWIKI_BOT=off) they end cleanly, touch no data."""
    durable.uninstall()                                       # what a Bot-off process looks like
    with caplog.at_level("WARNING", logger="evowiki.bot.durable"):
        assert tick("bot-off-tick", NOW) == []
        with SetWorkflowID("bot-off-check"):
            assert durable.bot_check("u1") == []
        with SetWorkflowID("bot-off-daily"):
            assert durable.bot_daily("u1", "2026-10-14", NOW.isoformat()) == []
        with SetWorkflowID("bot-off-dispatch"):
            durable.bot_dispatch("t1")
        with SetWorkflowID("bot-off-rsi"):
            assert durable.bot_rsi("t1") == []
    msgs = [r.getMessage() for r in caplog.records if "Bot off in this process" in r.getMessage()]
    assert len(msgs) == 5 and "bot-off-tick" in msgs[0]
    assert DBOS.retrieve_workflow("bot-off-tick").get_status().status == "SUCCESS"
    assert slots(pool) == [] and n_triggers(pool) == 0


def test_stale_scheduler_tick_is_skipped_with_a_log_line(bot, pool, caplog):
    wall = datetime.now(UTC)
    stale = "sched-bot:tick-stale"
    with caplog.at_level("WARNING", logger="evowiki.bot.durable"):
        assert tick(stale, wall - durable.TICK_MAX_AGE - timedelta(seconds=5)) == []
    assert any(stale in r.getMessage() and "skipped" in r.getMessage() for r in caplog.records)
    assert slots(pool) == []
    # a scheduler tick inside the limit still runs
    assert tick("sched-bot:tick-fresh", wall - durable.TICK_MAX_AGE + timedelta(seconds=30)) != []


def test_an_account_nobody_has_in_front_of_him_gets_no_bot(bot, pool, clock):
    """The failure: live test accounts got daily slots, sweeps and (idle) page writing."""
    make_user(pool, "on-demand-live-1")                   # only ever driven by a script
    make_user(pool, "away")
    at = NOW.replace(minute=10)
    with pool.connection() as conn:
        store.mark_seen(conn, "away", at - ACTIVE_WINDOW - timedelta(minutes=1))
    clock.now = at
    assert tick("bot-served", at) != []
    with pool.connection() as conn:
        users = {r["user_id"] for r in conn.execute("select user_id from bot_slot").fetchall()}
    assert users == {"u1"}
    sweeps = [w.workflow_id for w in DBOS.list_workflows(workflow_id_prefix="bot:sweep:")]
    assert len(sweeps) == 1 and sweeps[0].startswith("bot:sweep:u1:")


def test_paused_user_gets_no_daily_slot_and_no_sweep_check(bot, pool, clock):
    from evowiki.bot.settings import BotSettings
    with pool.connection() as conn:
        store.put_settings(conn, "u1", BotSettings(paused="always"), NOW)
    at = NOW.replace(minute=10)
    clock.now = at
    assert tick("bot-paused", at) == []
    assert slots(pool) == []
    assert DBOS.list_workflows(workflow_id_prefix="bot:sweep:") == []
    with pool.connection() as conn:
        store.put_settings(conn, "u1", BotSettings(), NOW)
    tick("bot-unpaused", at)
    assert slots(pool) != []
    assert len(DBOS.list_workflows(workflow_id_prefix="bot:sweep:u1")) == 1


def test_tick_takes_no_new_work_while_drained(bot, pool, clock):
    from evowiki.agent import lifecycle
    lifecycle.DRAINED.set()
    try:
        assert tick("tick-drained", NOW) == []
    finally:
        lifecycle.DRAINED.clear()
    assert slots(pool) == []
