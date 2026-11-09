"""B5c service glue: the Bot's RSI runner runs one full cycle (trigger `bot`) the way the tick
does, respects the user's manual setting and the cycle lock, and ties a change waiting for the
user to the Bot trigger. A spy records the cycles that run (harness off: no improvement round, no
LLM)."""

from datetime import timedelta

from evowiki.bot.actions import ActionLedger
from evowiki.bot.rsi_approvals import BOT_TRIGGER, LedgerApprovals
from evowiki.service import rsi_runs
from evowiki.service.rsi_runs import bot_rsi_runner, cycle_lock
from evowiki.store import bot as store
from evowiki.store import pinned
from evowiki.store.rsi import PgRsiStore
from evowiki.teach.contracts import RsiSettings
from tests.runtime.helpers import T0
from tests.service.conftest import signup


class Spy:
    def __init__(self):
        self.rounds = []


def spy_track(monkeypatch) -> Spy:
    spy = Spy()
    real = rsi_runs._cycle

    def cycle(svc, uid):
        c = real(svc, uid)
        run = c.run

        def spied(user_id, *a, **kw):
            spy.rounds.append(user_id)
            return run(user_id, *a, **kw)
        c.run = spied
        return c
    monkeypatch.setattr(rsi_runs, "_cycle", cycle)
    return spy


def test_one_bot_cycle_with_the_trigger_in_its_record(client, services, monkeypatch):
    signup(client, "u1")
    PgRsiStore(services.pool).put_settings("u1", RsiSettings(cycle="weekly"))
    spy = spy_track(monkeypatch)
    out = bot_rsi_runner(services)("u1", "btr_1")
    assert out["status"] == "ran" and out["b"] == "off" and spy.rounds == ["u1"]
    [rec] = PgRsiStore(services.pool).cycles("u1")
    assert rec.trigger == "bot" and rec.result["bot_trigger_id"] == "btr_1"
    assert rec.result["trigger"] == "bot"


def test_manual_setting_runs_nothing(client, services, monkeypatch):
    signup(client, "u1")
    spy = spy_track(monkeypatch)
    PgRsiStore(services.pool).put_settings("u1", RsiSettings(cycle="manual"))
    assert bot_rsi_runner(services)("u1", "btr_1") == {"status": "manual"}
    assert spy.rounds == [] and PgRsiStore(services.pool).cycles("u1") == []


def test_weekly_runs_at_most_one_cycle_a_week(client, services, monkeypatch):
    """The `weekly` setting caps Bot cycles too: a cycle this week → not_due, nothing
    runs; a week later it runs."""
    signup(client, "u1")
    spy = spy_track(monkeypatch)
    PgRsiStore(services.pool).put_settings("u1", RsiSettings(cycle="weekly"))
    assert bot_rsi_runner(services)("u1", "btr_1")["status"] == "ran"
    assert bot_rsi_runner(services)("u1", "btr_2") == {"status": "not_due"}
    assert spy.rounds == ["u1"] and len(PgRsiStore(services.pool).cycles("u1")) == 1
    later = services.clock() + timedelta(days=7, minutes=1)
    monkeypatch.setattr(services, "clock", lambda: later)
    assert bot_rsi_runner(services)("u1", "btr_3")["status"] == "ran"


def test_a_running_cycle_makes_it_busy(client, services, monkeypatch):
    signup(client, "u1")
    PgRsiStore(services.pool).put_settings("u1", RsiSettings(cycle="weekly"))
    spy = spy_track(monkeypatch)
    with cycle_lock(services, "u1") as got:
        assert got
        assert bot_rsi_runner(services)("u1", "btr_1") == {"status": "busy"}
    assert spy.rounds == []
    assert bot_rsi_runner(services)("u1", "btr_1")["status"] == "ran"


def test_a_change_waiting_for_the_user_is_tied_to_the_bot_trigger(client, services):
    """`ask`: an RSI change that passed its Gate waits in the ledger; inside a Bot cycle the row
    carries the trigger (why chain) and the Bot's 因为."""
    signup(client, "u1")
    ledger = ActionLedger(services.pool, services.clock, services.new_id, {})
    approvals = LedgerApprovals(ledger, services.pool)
    token = BOT_TRIGGER.set("btr_9")
    try:
        with pinned.transaction(services.pool):
            approvals.propose("u1", kind="rsi_patch", scope="rsi", key="rsi:pb_1",
                              summary="先举例再给定义", why="三次都说看不懂", content={"patch_id": "pb_1"},
                              at=T0 + timedelta(minutes=1))
    finally:
        BOT_TRIGGER.reset(token)
    with services.pool.connection() as conn:
        row = store.get_action_by_key(conn, "rsi:pb_1")
        assert [a.action_id for a in store.actions_of_trigger(conn, "btr_9")] == [row.action_id]
    assert (row.status, row.trigger_id, row.why) == ("proposed", "btr_9", "三次都说看不懂")
