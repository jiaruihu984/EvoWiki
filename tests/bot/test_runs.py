"""Run records, the failure pause, and the DBOS workflows that write them. Fakes only."""
from datetime import UTC, datetime, timedelta

import pytest
from dbos import DBOS

from evowiki.agent import dbos_app
from evowiki.bot import durable, runs
from evowiki.bot.durable import BotRuntime
from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.store import bot as store
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)      # 17:00 in Asia/Shanghai
_n = iter(range(100_000))


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


@pytest.fixture(autouse=True)
def _user(pool):
    make_user(pool, "u1")


def run(pool, rid, status, i, *, kind="plan", detail=""):
    at = NOW + timedelta(minutes=i)
    runs.start_run(pool, run_id=rid, user_id="u1", kind=kind, trigger_id=None, now=at)
    if status != "running":
        runs.finish_run(pool, rid, status=status, now=at, detail=detail)


SLOT = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)     # 08:00 in Asia/Shanghai, the next day


def pause(pool, i, kind="plan", settings=DEFAULT_SETTINGS):
    return runs.pause_after_failure(pool, "u1", kind, now=NOW + timedelta(minutes=i, seconds=30),
                                    settings=settings)


def paused_until(pool, kind="plan", *, at=NOW):
    return runs.paused_until(pool, "u1", kind, now=at)


def cards(pool):
    with pool.connection() as conn:
        return store.suggestions(conn, "u1")


def test_start_is_idempotent_and_finish_records(pool):
    run(pool, "r1", "running", 0)
    run(pool, "r1", "running", 5)                      # a recovered workflow: no duplicate
    runs.finish_run(pool, "r1", status="ok", now=NOW, calls=2, cost_usd=0.5, reads=["a"],
                    detail="d")
    with pool.connection() as conn:
        r = store.get_run(conn, "r1")
        assert conn.execute("select count(*) as n from bot_run").fetchone()["n"] == 1
    assert (r.status, r.calls, r.cost_usd, r.reads, r.detail, r.started_at) == (
        "ok", 2, 0.5, ["a"], "d", NOW)
    with pytest.raises(LookupError):
        runs.finish_run(pool, "nope", status="ok", now=NOW)


def test_three_failures_pause_the_kind_until_the_next_daily_slot(pool, caplog):
    for i in range(3):
        run(pool, f"r{i}", "failed", i, detail=f"boom {i}")
        until = pause(pool, i)
        assert (until is not None) == (i == 2)
    assert until == SLOT and paused_until(pool) == SLOT
    assert "failed 3 times in a row" in caplog.text and "boom 2" in caplog.text
    assert paused_until(pool, "check") is None                        # kinds are separate
    assert paused_until(pool, at=SLOT) is None                        # over at the slot itself
    assert cards(pool) == []                                          # nothing shown to the user


def test_the_pause_ends_at_the_next_slot_whichever_side_of_todays_it_is(pool):
    s1 = DEFAULT_SETTINGS.model_copy(update={"failure_streak": 1})
    run(pool, "r0", "failed", 0)
    after_slot = datetime(2026, 10, 14, 0, 30, tzinfo=UTC)         # 08:30 CST: today's slot passed
    before_slot = datetime(2026, 10, 13, 23, 30, tzinfo=UTC)       # 07:30 CST: it is still ahead
    assert runs.pause_after_failure(pool, "u1", "plan", now=after_slot, settings=s1) == SLOT
    assert runs.pause_after_failure(pool, "u1", "plan", now=before_slot, settings=s1) == (
        datetime(2026, 10, 14, 0, 0, tzinfo=UTC))


def test_success_in_between_resets_streak(pool):
    run(pool, "r0", "failed", 0)
    run(pool, "r1", "failed", 1)
    run(pool, "r2", "ok", 2)
    run(pool, "r3", "failed", 3)
    assert pause(pool, 3) is None
    run(pool, "r4", "failed", 4)
    assert pause(pool, 4) is None
    run(pool, "r5", "failed", 5)
    assert pause(pool, 5) == SLOT


def test_running_run_breaks_streak_and_kinds_are_separate(pool):
    run(pool, "r0", "failed", 0, kind="check")
    run(pool, "r1", "failed", 1, kind="check")
    run(pool, "r2", "failed", 2, kind="daily")
    assert pause(pool, 2, "check") is None and pause(pool, 2, "daily") is None
    run(pool, "r3", "failed", 3, kind="check")
    run(pool, "r4", "running", 4, kind="check")                  # still running: no streak
    assert pause(pool, 4, "check") is None


def test_a_paused_kind_needs_a_fresh_streak_after_the_pause(pool):
    for i in range(3):
        run(pool, f"a{i}", "failed", i)
    assert pause(pool, 2) == SLOT
    run(pool, "skipped", "no_action", 3, detail=runs.PAUSED_DETAIL)     # a run during the pause
    assert pause(pool, 3) is None


def test_custom_streak_and_an_existing_later_pause_is_kept(pool):
    s = DEFAULT_SETTINGS.model_copy(update={"failure_streak": 1})
    run(pool, "r0", "failed", 0)
    assert pause(pool, 0, settings=s) == SLOT
    with pool.connection() as conn:
        store.put_kind_pause(conn, "u1", "plan", SLOT + timedelta(days=5))
        store.put_kind_pause(conn, "u1", "plan", SLOT)
    assert paused_until(pool) == SLOT + timedelta(days=5)


# --- DBOS workflows ------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 14, 0, 30, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def bot(db_url, pool):
    dbos_app.launch(db_url, app_version=dbos_app.APP_VERSION)
    durable.register(BotRuntime(pool, Clock(), new_id), schedule=False)
    yield
    dbos_app.shutdown()


def all_runs(pool):
    with pool.connection() as conn:
        return conn.execute("select run_id, kind, status, detail, finished_at from bot_run "
                            "order by started_at, run_id").fetchall()


def test_workflows_record_ok_and_no_action(bot, pool):
    durable.enqueue(durable.bot_check, "u1", "bot:check:u1:a", "u1").get_result()
    [r] = all_runs(pool)
    assert (r["run_id"], r["kind"], r["status"]) == ("bot:check:u1:a", "check", "no_action")
    assert r["finished_at"] is not None


def test_failing_workflow_records_failed_then_pauses_after_the_streak(bot, pool, monkeypatch,
                                                                    caplog):
    calls = []

    def boom(pool, user_id, *, now, new_id):
        calls.append(1)
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(durable.pipeline, "scan_events", boom)
    for i in range(3):
        h = durable.enqueue(durable.bot_check, "u1", f"bot:check:u1:f{i}", "u1")
        with pytest.raises(Exception, match="scan exploded"):
            h.get_result()
        assert DBOS.retrieve_workflow(f"bot:check:u1:f{i}").get_status().status == "ERROR"
    rows = all_runs(pool)
    assert [r["status"] for r in rows] == ["failed"] * 3
    assert "scan exploded" in rows[0]["detail"]
    assert cards(pool) == []                                     # no card for the user
    with pool.connection() as conn:
        assert store.kind_paused_until(conn, "u1", "check", datetime(2026, 10, 14, 1, tzinfo=UTC))
    # the paused kind records no_action, does nothing, and logs a warning
    durable.enqueue(durable.bot_check, "u1", "bot:check:u1:paused", "u1").get_result()
    last = all_runs(pool)[-1]
    assert (last["status"], last["detail"]) == ("no_action", runs.PAUSED_DETAIL)
    assert len(calls) == 3
    assert runs.PAUSED_DETAIL in caplog.text


def test_dispatch_workflow_records_its_trigger(bot, pool):
    t = store.TriggerRow(
        trigger_id="t1", user_id="u1", kind="review_due", rule_id="review_due", rule_version=1,
        source_key="t1", dedup_key="review_due", route="direct", evidence=[],
        payload={"count": 2}, priority=50, reason="r", created_at=NOW)
    with pool.connection() as conn:
        store.insert_trigger(conn, t)
    durable.enqueue(durable.bot_dispatch, "u1", "bot:dispatch:t1:x", "t1").get_result()
    [r] = all_runs(pool)
    assert (r["kind"], r["status"]) == ("dispatch", "ok")
    with pool.connection() as conn:
        assert store.get_run(conn, "bot:dispatch:t1:x").trigger_id == "t1"
