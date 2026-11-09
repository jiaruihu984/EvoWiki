"""Bounded production RSI approval/version and evidence sequences; scripted selection only.

The models keep expected user decisions and evidence counts, not production status helpers.
Real PostgreSQL mirror/transaction replays live in store/test_rsi_atomic and harness/test_change_source.
"""

from collections import Counter
from datetime import timedelta

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from evowiki.teach.approval import PendingGone, PendingStale
from evowiki.teach.cycle import RsiCycle, UserData
from evowiki.teach.layer2 import method_registry as mr
from evowiki.teach.layer2.store import MemoryLayer2Store
from evowiki.teach.rsi_store import MemoryRsiStore
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach.fake_approvals import FakeApprovals
from tests.teach.fit.conftest import quiz_log
from tests.teach.test_cycle_a2 import ASK_MODEL, CMP, T, U, _fit_patch, _world


def test_approval_version_sequences(monkeypatch):
    from evowiki.teach.fit import select

    events, domains = quiz_log(120)
    full = UserData(events=events, domain_of=domains, domain_label={}, signals_since=lambda _: 0)
    thin = UserData(events=(), domain_of={}, domain_label={}, signals_since=lambda _: 0)
    coverage = Counter()

    class Protocol(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.version = 1
            self.versions = {1: mr.DEFAULT_FIT_MODEL}
            self.records = {}
            self.effects = 0
            self.tick = 0
            self.ids = 0
            self.cycle = RsiCycle(MemoryRsiStore(), ASPECT_CONFIG_V1, new_id=self.new_id,
                                  layer2=MemoryLayer2Store(), approvals=FakeApprovals())

        def new_id(self, prefix):
            self.ids += 1
            return f"{prefix}_{self.ids}"

        def now(self):
            self.tick += 1
            return T + timedelta(days=30, seconds=self.tick)

        def effect(self):
            self.effects += 1

        @rule(enough=st.booleans(), better=st.booleans())
        def propose(self, enough, better):
            coverage["propose"] += 1
            before = len(self.records)
            pending = next((key for key, c in self.records.items() if c["status"] == "awaiting_user"), None)
            target = mr.DEFAULT_FIT_MODEL if self.versions[self.version].paths == ASK_MODEL.paths else ASK_MODEL
            banned = {frozenset(c["model"].paths) for c in self.records.values() if c["status"] in ("rejected", "rolled_back")}

            def selection(*args, **kwargs):
                assert {frozenset(key[0]) for key in kwargs["banned"]} == banned
                return (("add evidence_weight.ask", target, dict(CMP))
                        if better and frozenset(target.paths) not in banned else None)

            monkeypatch.setattr(select, "select_model", selection)
            result, _ = self.cycle._run_a2(U, full if enough else thin, self.now())
            if not enough:
                coverage["thin_refused"] += 1
                assert result == "too_few_outcomes"
            elif pending:
                coverage["pending_deduplicated"] += 1
                assert result == f"awaiting_user {pending}"
            elif better and frozenset(target.paths) not in banned:
                status, cid = result.split()
                assert status == "awaiting_user"
                self.records[cid] = {"status": "awaiting_user", "from": self.version, "to": None, "model": target}
            else:
                assert result == "no_better"
            assert len(self.cycle.layer2.method_changes(U)) == len(self.records)
            assert len(self.records) <= before + 1

        @rule(index=st.integers(0, 10), action=st.sampled_from(["approve", "reject", "undo"]))
        def decide(self, index, action):
            coverage[action] += 1
            keys = [*self.records, "unknown"]
            eligible = [cid for cid, c in self.records.items()
                        if c["status"] == ("applied" if action == "undo" else "awaiting_user")]
            if eligible and index % 3:
                keys = eligible
            cid = keys[index % len(keys)]
            record = self.records.get(cid)
            now = self.now()
            if action == "undo":
                applied = {key: c for key, c in self.records.items() if c["status"] == "applied"}
                latest = max(applied, key=lambda key: applied[key]["to"], default=None)
                if record is None:
                    coverage["unknown_undo_refused"] += 1
                    with pytest.raises(LookupError):
                        self.cycle.undo_method(U, cid, now=now)
                elif cid != latest:
                    coverage["undo_refused"] += 1
                    with pytest.raises(ValueError):
                        self.cycle.undo_method(U, cid, now=now)
                else:
                    coverage["undo_applied"] += 1
                    self.cycle.undo_method(U, cid, now=now)
                    record["status"] = "rolled_back"
                    self.version += 1
                    self.versions[self.version] = self.versions[record["from"]]
                return
            operation = (lambda: self.cycle.approve_pending(U, cid, now=now, on_accept=self.effect)
                         if action == "approve" else self.cycle.reject_pending(U, cid, now=now))
            if record is None or record["status"] != "awaiting_user":
                coverage["decision_refused"] += 1
                with pytest.raises(PendingGone):
                    operation()
            elif action == "reject":
                operation()
                record["status"] = "rejected"
            elif record["from"] != self.version:
                coverage["stale_refused"] += 1
                with pytest.raises(PendingStale):
                    operation()
                record["status"] = "gate_failed"
            else:
                coverage["approve_applied"] += 1
                operation()
                self.version += 1
                self.versions[self.version] = record["model"]
                record["status"], record["to"] = "applied", self.version

        @rule()
        def external_version(self):
            coverage["version"] += 1
            model = self.versions[self.version]
            mr.commit(self.cycle.layer2, U, "A2", fit_model=model, change_id=None, now=self.now())
            self.version += 1
            self.versions[self.version] = model

        @invariant()
        def decisions_and_live_version_agree(self):
            assert {c.change_id: c.status for c in self.cycle.layer2.method_changes(U)} == {
                cid: c["status"] for cid, c in self.records.items()}
            assert mr.current(self.cycle.layer2, U, "A2").version == self.version
            assert self.cycle.fit_model(U).paths == self.versions[self.version].paths
            assert self.effects == sum(c["to"] is not None for c in self.records.values())

    run_state_machine_as_test(Protocol, settings=settings(max_examples=200, stateful_step_count=50, deadline=None))
    assert all(coverage[key] for key in ("thin_refused", "pending_deduplicated", "stale_refused", "approve_applied", "undo_applied"))
    print("S4 approval command coverage", dict(coverage))


def test_online_evidence_confirmation_sequences():
    coverage = Counter()

    class Observation(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            store, layer2 = _world()
            self.cycle = RsiCycle(store, ASPECT_CONFIG_V1, new_id=lambda _: "unused", layer2=layer2)
            self.count = 12
            self.last = None
            self.confirmations = 0
            self.closed = False
            self.tick = 0

        def now(self):
            self.tick += 1
            return T + timedelta(days=2, seconds=self.tick)

        @rule(n=st.integers(0, 10))
        def evidence(self, n):
            coverage["evidence"] += 1
            for _ in range(n):
                _fit_patch(self.cycle.store, f"more{self.count}", 2, "gate_failed", self.now())
                self.count += 1

        @rule()
        def observe(self):
            coverage["observe"] += 1
            rows = self.cycle._monitor_a2(U, self.now())
            if self.closed or (self.last is not None and self.count - self.last < 10):
                coverage["observe_unchanged"] += 1
                assert rows == []
                return
            self.last = self.count
            self.confirmations += 1
            self.closed = self.confirmations >= 2
            coverage["automatic_rollback" if self.closed else "first_harmful"] += 1
            assert len(rows) == 1 and rows[0]["rolled_back"] == self.closed

        @rule()
        def undo(self):
            coverage["manual_undo"] += 1
            if self.closed:
                with pytest.raises(ValueError):
                    self.cycle.undo_method(U, "a2_x", now=self.now())
            else:
                coverage["manual_undo_applied"] += 1
                self.cycle.undo_method(U, "a2_x", now=self.now())
                self.closed = True

        @invariant()
        def terminal_effect_matches_registry(self):
            (change,) = self.cycle.layer2.method_changes(U)
            assert change.status == ("rolled_back" if self.closed else "applied")
            assert mr.current(self.cycle.layer2, U, "A2").version == (3 if self.closed else 2)

    # Generated users may undo before enough independent evidence accumulates.
    # Exercise both terminal paths explicitly, then explore arbitrary interleavings.
    sample = Observation()
    sample.observe()
    sample.observe()
    sample.evidence(10)
    sample.observe()
    sample.terminal_effect_matches_registry()
    sample.teardown()
    sample = Observation()
    sample.undo()
    sample.terminal_effect_matches_registry()
    sample.teardown()
    assert all(coverage[key] for key in ("automatic_rollback", "first_harmful", "observe_unchanged", "manual_undo_applied"))
    run_state_machine_as_test(Observation, settings=settings(max_examples=200, stateful_step_count=50, deadline=None))
    assert all(coverage[key] for key in ("automatic_rollback", "first_harmful", "observe_unchanged", "manual_undo_applied"))
    print("S4 observation command coverage", dict(coverage))
