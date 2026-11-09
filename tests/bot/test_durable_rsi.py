"""B5c on DBOS: a strategy_negative intent runs the Bot's own RSI workflow (bot_rsi); a busy
cycle lock defers it and the sweep retries. Fake runner only (no paid API)."""
from datetime import UTC, datetime, timedelta

import pytest
from dbos import DBOS

from evowiki.agent import dbos_app
from evowiki.bot import durable
from evowiki.bot.durable import BotRuntime
from evowiki.store import bot as store
from evowiki.teach.contracts import Signal
from evowiki.wikiapp.events import SignalRecorded, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.bot.test_durable import NOW, Clock, new_id, tick
from tests.runtime.helpers import make_user


class Runner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, user_id, trigger_id):
        self.calls.append((user_id, trigger_id))
        return self.results.pop(0)


RAN = {"status": "ran", "a": "no_cluster", "b": "personal_accepted pb_1", "patch_ids": ["pb_1"]}


def boot(db_url, pool, clock, runner):
    make_user(pool, "u1")
    dbos_app.launch(db_url, app_version=dbos_app.APP_VERSION)
    durable.register(BotRuntime(pool, clock, new_id, rsi=runner), schedule=False)


@pytest.fixture
def clock():
    return Clock(NOW)


def seed_unclear(pool, kind="section_deeper", n=3):
    r = PgRepo(pool)
    evs = [WikiEvent(event_id=f"wev_sig{i}", ts=NOW - timedelta(minutes=10 - i),
                     body=SignalRecorded(signal=Signal(signal_id=f"sig{i}", kind=kind,
                                                       at=NOW - timedelta(minutes=10 - i),
                                                       outcome=0.0 if kind == "check_answer"
                                                       else None)))
           for i in range(n)]
    r.commit(r.load("u1"), Commit(wiki=evs))


def trigger(pool, tid):
    with pool.connection() as conn:
        return store.get_trigger(conn, tid)


def rsi_runs(pool):
    with pool.connection() as conn:
        rows = conn.execute("select run_id, status from bot_run where kind = 'rsi' "
                            "order by started_at, run_id").fetchall()
    return [(r["run_id"], r["status"]) for r in rows]


def test_three_unclear_signals_run_one_rsi_cycle(db_url, pool, clock):
    runner = Runner(RAN)
    boot(db_url, pool, clock, runner)
    try:
        seed_unclear(pool)
        [tid] = durable.enqueue(durable.bot_check, "u1", "bot:check:u1:t", "u1").get_result()
        assert DBOS.retrieve_workflow(durable.rsi_id(tid, 0)).get_result() == [tid]
        assert runner.calls == [("u1", tid)]
        t = trigger(pool, tid)
        assert (t.kind, t.status, t.gate_detail, t.run_id) == (
            "strategy_negative", "handed_off", "改了一条讲解规则", durable.rsi_id(tid, 0))
        assert rsi_runs(pool) == [(durable.rsi_id(tid, 0), "ok")]
        # the same signals never start a second cycle
        assert durable.enqueue(durable.bot_check, "u1", "bot:check:u1:t2", "u1").get_result() == []
    finally:
        dbos_app.shutdown()


def test_three_wrong_answers_do_not_reach_rsi(db_url, pool, clock):
    runner = Runner()
    boot(db_url, pool, clock, runner)
    try:
        seed_unclear(pool, kind="check_answer")
        assert durable.enqueue(durable.bot_check, "u1", "bot:check:u1:t", "u1").get_result() == []
        assert runner.calls == []
    finally:
        dbos_app.shutdown()


def test_busy_lock_defers_and_the_sweep_retries(db_url, pool, clock):
    runner = Runner({"status": "busy"}, RAN)
    boot(db_url, pool, clock, runner)
    try:
        seed_unclear(pool)
        [tid] = durable.enqueue(durable.bot_check, "u1", "bot:check:u1:t", "u1").get_result()
        assert DBOS.retrieve_workflow(durable.rsi_id(tid, 0)).get_result() == []
        t = trigger(pool, tid)
        assert (t.status, t.gate_outcome, t.gate_reason) == ("new", "defer", "busy")
        at = datetime(2026, 10, 14, 3, 10, tzinfo=UTC)
        clock.now = at
        tick("bot-rsi-sweep", at)
        DBOS.retrieve_workflow(f"bot:dispatch:{tid}:20261014T0310").get_result()
        assert DBOS.retrieve_workflow(durable.rsi_id(tid, 1)).get_result() == [tid]
        assert trigger(pool, tid).status == "handed_off" and len(runner.calls) == 2
        assert rsi_runs(pool) == [(durable.rsi_id(tid, 0), "no_action"),
                                  (durable.rsi_id(tid, 1), "ok")]
    finally:
        dbos_app.shutdown()


def test_a_lost_rsi_run_is_enqueued_again_by_the_sweep(db_url, pool, clock):
    runner = Runner(RAN)
    boot(db_url, pool, clock, runner)
    try:
        make = store.TriggerRow(
            trigger_id="t_lost", user_id="u1", kind="strategy_negative",
            rule_id="strategy_negative", rule_version=2, source_key="strategy:x",
            dedup_key="strategy_negative", route="rsi", evidence=[], payload={"signals": []},
            priority=30, reason="r", created_at=NOW, status="awaiting_rsi", gate_outcome="pass",
            gated_at=NOW)
        with pool.connection() as conn:
            store.insert_trigger(conn, make)
            # a run left `running` whose workflow is gone (cancelled / crashed): it does not block
            store.insert_run(conn, run_id="bot:rsi:t_lost:0", user_id="u1", kind="rsi",
                             trigger_id="t_lost", now=NOW)
        at = datetime(2026, 10, 14, 3, 10, tzinfo=UTC)
        clock.now = at
        tick("bot-rsi-lost", at)
        assert DBOS.retrieve_workflow(
            durable.rsi_id("t_lost", "sweep-20261014T0310")).get_result() == ["t_lost"]
        assert trigger(pool, "t_lost").status == "handed_off"
    finally:
        dbos_app.shutdown()


def test_a_gate_error_records_a_failed_run_not_a_handed_off_rejection(db_url, pool, clock):
    """Live 2026-10-20: the Gate judge's request was an HTTP 400. The cycle ran, but its track B
    round is an error: the trigger is failed with the error line and the rsi run is `failed`
    (failure streak), not `ok` / handed_off as if the patch had been rejected."""
    err = {"status": "ran", "a": "no_cluster", "patch_ids": [],
           "b": "error: gate_error: judge: BadRequestError: output_config.format.schema"}
    runner = Runner(err)
    boot(db_url, pool, clock, runner)
    try:
        seed_unclear(pool)
        [tid] = durable.enqueue(durable.bot_check, "u1", "bot:check:u1:t", "u1").get_result()
        with pytest.raises(Exception, match="复核时模型调用失败"):
            DBOS.retrieve_workflow(durable.rsi_id(tid, 0)).get_result()
        t = trigger(pool, tid)
        assert (t.status, t.gate_detail) == ("failed", "复核时模型调用失败，未修改，之后会重试")
        assert rsi_runs(pool) == [(durable.rsi_id(tid, 0), "failed")]
    finally:
        dbos_app.shutdown()
