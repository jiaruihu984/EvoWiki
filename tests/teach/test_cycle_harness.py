"""The harness loop in the RSI cycle (harness RSI phase 2, Task 9): with `RsiSettings.harness` on a
cycle runs the harness round (the one improvement round since RSI-B retired), is due by the signals
since the last round, and hands harness changes waiting for the user to the harness track; off, no
round runs (`b` = "off"). A spy track, memory stores."""

from datetime import UTC, datetime, timedelta

import pytest

from evowiki.teach.contracts import RSI_SWITCH_OFF, RSI_SWITCH_ON, RsiSettings, rsi_on
from evowiki.teach.cycle import RsiCycle, UserData, due
from evowiki.teach.rsi_store import MemoryRsiStore
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach.fake_approvals import Effect, FakeApprovals

U = "u1"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"


class SpyHarness:
    def __init__(self, last=None):
        self.runs, self.approved, self.rejected = [], [], []
        self.last = last
        self.asked = [("h_c1", "harness:u1:c1")]
        self.order: list[str] = []
        self.counted: list = []
        self.due = 10            # page signals + assistant feedback, as the round counts them

    def observe(self, user_id, *, now):
        self.order.append("observe")
        return [{"change_id": "h_c0", "verdict": "harmful", "rolled_back": True}]

    def last_round_at(self, user_id):
        return self.last

    def due_count(self, user_id, since):
        self.counted.append(since)
        return self.due

    def run(self, user_id, *, now, manual):
        self.runs.append((user_id, now, manual))
        self.order.append("run")
        return "harness applied r1: 关卡通过", 0.25

    def owns(self, change_id):
        return change_id.startswith("h_")

    def approve(self, user_id, change_id, *, now, on_accept):
        on_accept()
        self.approved.append(change_id)

    def reject(self, user_id, change_id, *, now):
        self.rejected.append(change_id)

    def asked_keys(self, user_id):
        return self.asked


def data(signals: int) -> UserData:
    return UserData(events=(), domain_of={}, domain_label={},
                    signals_since=lambda t: signals)


def cycle(store, harness, approvals=None):
    return RsiCycle(store, ASPECT_CONFIG_V1, new_id=Ids(), harness=harness, approvals=approvals)


@pytest.mark.parametrize("signals,expect", [(9, None), (10, "weekly")])
def test_the_switch_runs_weekly_with_ten_signals_and_the_default_never_comes_due(signals, expect):
    assert due(RSI_SWITCH_ON, None, signals, NOW) == expect
    assert due(RsiSettings(), None, signals, NOW) is None
    assert not rsi_on(RsiSettings()) and rsi_on(RSI_SWITCH_ON) and not rsi_on(RSI_SWITCH_OFF)


def test_with_harness_on_the_round_runs():
    store, h = MemoryRsiStore(), SpyHarness()
    store.put_settings(U, RsiSettings(harness=True, cycle="weekly"))
    res = cycle(store, h).run(U, data(10), trigger="every_10_feedback", now=NOW)
    assert h.runs == [(U, NOW, False)]
    assert res.b == "harness applied r1: 关卡通过" and res.cost_usd == pytest.approx(0.25)
    (rec,) = store.cycles(U)
    assert rec.cost_usd == pytest.approx(0.25)
    cycle(store, h).run(U, data(0), trigger="manual", now=NOW)
    assert h.runs[-1] == (U, NOW, True)             # only a manual run skips the round's due check


def test_with_harness_off_no_improvement_round_runs():
    store, h = MemoryRsiStore(), SpyHarness()
    store.put_settings(U, RsiSettings())
    res = cycle(store, h).run(U, data(10), trigger="every_10_feedback", now=NOW)
    assert h.runs == [] and res.b == "off" and res.cost_usd == 0.0


def test_due_counts_from_the_last_harness_round_when_on():
    store = MemoryRsiStore()
    store.put_settings(U, RsiSettings(harness=True, cycle="every_10_feedback"))   # no week cap
    seen = []
    d = UserData(events=(), domain_of={}, domain_label={},
                 signals_since=lambda t: seen.append(t) or 10)
    h = SpyHarness(last=NOW - timedelta(hours=3))
    assert cycle(store, h).maybe_run(U, d, now=NOW) is not None
    # on: the harness's own count (page signals + assistant feedback), not the page signals
    assert h.counted == [NOW - timedelta(hours=3)] and seen == [] and len(h.runs) == 1
    h.due = 9
    assert cycle(store, h).maybe_run(U, d, now=NOW) is None
    store.put_settings(U, RsiSettings(cycle="every_10_feedback"))   # off: from the last cycle
    seen.clear()
    cycle(store, h).maybe_run(U, d, now=NOW + timedelta(hours=1))
    assert seen == [NOW]


def test_harness_changes_are_approved_and_rejected_through_the_harness_track():
    store, h = MemoryRsiStore(), SpyHarness()
    c = cycle(store, h)
    effect = Effect()
    c.approve_pending(U, "h_c1", now=NOW, on_accept=effect)
    c.reject_pending(U, "h_c2", now=NOW)
    assert h.approved == ["h_c1"] and h.rejected == ["h_c2"] and effect.n == 1


def test_a_rejected_ledger_row_of_a_harness_change_is_closed_by_the_next_run():
    store, h = MemoryRsiStore(), SpyHarness()
    ap = FakeApprovals(ask=False)
    ap.rejected_keys.add("harness:u1:c1")
    cycle(store, h, approvals=ap).run(U, data(0), trigger="manual", now=NOW)
    assert h.rejected == ["h_c1"]


def test_a_harness_track_that_raises_still_writes_the_cycle_record():
    store, h = MemoryRsiStore(), SpyHarness()
    store.put_settings(U, RsiSettings(harness=True, cycle="weekly"))

    def boom(*a, **k):
        raise RuntimeError("db down")
    h.run = boom
    res = cycle(store, h).run(U, data(10), trigger="manual", now=NOW)
    assert res.b == "error: RuntimeError" and len(store.cycles(U)) == 1


def test_harness_on_without_a_harness_track_is_skipped():
    store = MemoryRsiStore()
    store.put_settings(U, RsiSettings(harness=True, cycle="weekly"))
    res = cycle(store, None).run(U, data(10), trigger="every_10_feedback", now=NOW)
    assert res.b == "harness skipped: 没有可用的写页面模型，这一轮跳过"


def test_with_harness_on_applied_changes_are_observed_before_the_round():
    store, h = MemoryRsiStore(), SpyHarness()
    store.put_settings(U, RsiSettings(harness=True, cycle="weekly"))
    cycle(store, h).run(U, data(10), trigger="every_10_feedback", now=NOW)
    assert h.order == ["observe", "run"]
    (rec,) = store.cycles(U)
    assert {"change_id": "h_c0", "verdict": "harmful", "rolled_back": True} in rec.result["outcomes"]



def test_with_harness_off_applied_changes_are_still_observed_but_no_round_runs():
    # the loop turned off must not leave its applied changes unwatched (final review I3)
    store, h = MemoryRsiStore(), SpyHarness()
    store.put_settings(U, RsiSettings())
    res = cycle(store, h).run(U, data(10), trigger="every_10_feedback", now=NOW)
    assert h.order == ["observe"] and h.runs == [] and res.b == "off"
    (rec,) = store.cycles(U)
    assert {"change_id": "h_c0", "verdict": "harmful", "rolled_back": True} in rec.result["outcomes"]

    def boom(*a, **k):
        raise RuntimeError("db down")
    h.observe = boom
    res = cycle(store, h).run(U, data(10), trigger="manual", now=NOW)
    assert res.b == "off; observe error: RuntimeError"


def test_a_failing_harness_observation_is_named_in_the_line_and_the_round_still_runs():
    store, h = MemoryRsiStore(), SpyHarness()
    store.put_settings(U, RsiSettings(harness=True, cycle="weekly"))

    def boom(*a, **k):
        raise RuntimeError("db down")
    h.observe = boom
    res = cycle(store, h).run(U, data(10), trigger="every_10_feedback", now=NOW)
    assert res.b == "harness applied r1: 关卡通过; observe error: RuntimeError"
    assert len(h.runs) == 1 and len(store.cycles(U)) == 1
