"""RSI cycle controller: track A (A1 fitter) end to end on the scenarios (MemoryRsiStore, fixed clock).
A stubbed `fit_infer` stands in where a test needs a fit the data would not give (a wrong fit)."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.goals.contracts import Aspect, cell_id
from evowiki.teach import cycle as cycle_mod
from evowiki.teach import infer_paths as ip
from evowiki.teach import registry
from evowiki.teach.contracts import (
    FitPatchOp,
    GateResult,
    InferParams,
    Localization,
    ParamPatchOp,
    Patch,
    PatchMemoryEntry,
    RsiSettings,
    SpecPatchOp,
)
from evowiki.teach.cycle import (
    Budget,
    RsiCycle,
    UserData,
    due,
)
from evowiki.teach.fit.fitter import FitResult, PathPosterior
from evowiki.teach.memory import signature
from evowiki.teach.predict import predict_outcomes
from evowiki.teach.rsi_store import MemoryRecord, MemoryRsiStore, PatchRecord
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach import scenarios

U = "u1"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SELF = "evidence_weight.self"
D = InferParams()


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"


@pytest.fixture(scope="module")
def oc():
    events, dom = scenarios.overconfident_self(seed=0)
    return events, dom, predict_outcomes(events, ASPECT_CONFIG_V1, D, dom)


def _data(events, dom, signals=0):
    return UserData(events=events, domain_of=dom, domain_label={"d1": "代数", "d2": "几何"},
                    signals_since=lambda t: signals)


def _cycle(store, **kw):
    return RsiCycle(store, ASPECT_CONFIG_V1, new_id=Ids(), **kw)


def _stub_fit(value):
    """fit_infer stand-in: the self-report weight fitted to `value` (whatever the data say)."""
    def fit(events, base, start, domain_of, *, model, priors, cutoff):
        n = sum(1 for o in predict_outcomes(events, base, start, domain_of) if o.ts < cutoff)
        post = PathPosterior(path=SELF, value=value, lo=value - 0.1, hi=value + 0.1,
                             prior_mean=ip.get(D, SELF))
        return FitResult(params=ip.set_(start, SELF, value), posteriors=(post,), loglik_train=0.0,
                         loglik_start=0.0, n_train=n)
    return fit


def _until(events, outcomes, k):
    """The log up to just after the k-th outcome, and that moment."""
    at = outcomes[k - 1].ts + timedelta(minutes=1)
    return [e for e in events if e.ts <= at], at


# ---- due ----

@pytest.mark.parametrize("cycle,last,signals,expect", [
    ("every_10_feedback", None, 9, None),
    ("every_10_feedback", NOW - timedelta(hours=1), 10, "every_10_feedback"),
    ("every_5_feedback", None, 4, None),
    ("every_5_feedback", None, 5, "every_5_feedback"),
    ("every_5_feedback", NOW - timedelta(hours=1), 7, "every_5_feedback"),
    ("weekly", None, 9, None),                       # fewer than 10 new signals: never due
    ("weekly", None, 10, "weekly"),
    ("weekly", NOW - timedelta(days=6), 50, None),   # at most once a week
    ("weekly", NOW - timedelta(days=7), 9, None),
    ("weekly", NOW - timedelta(days=7), 10, "weekly"),
    ("weekly", NOW - timedelta(days=60), 3, None),   # an unused wiki waits
    ("manual", None, 99, None),
])
def test_due(cycle, last, signals, expect):
    assert due(RsiSettings(cycle=cycle), last, signals, NOW) == expect


def test_budget():
    b = Budget(limit_usd=0.3)
    assert b.can_spend(0.3) and not b.can_spend(0.31)
    b.spend(0.2)
    assert b.spent_usd == pytest.approx(0.2) and not b.can_spend(0.2)
    with pytest.raises(ValueError):
        b.spend(-1)


# ---- accept, change list, undo ----

def test_a_bot_cycle_records_its_trigger_and_its_changes_say_so(oc):
    """B5c: trigger "bot" with the Bot trigger in the record; the change list's 因为 starts with
    the Bot's reason (only for the changes that cycle made)."""
    events, dom, _ = oc
    store = MemoryRsiStore()
    c = _cycle(store)
    res = c.run(U, _data(events, dom), trigger=cycle_mod.BOT_TRIGGER, now=NOW,
                context={"bot_trigger_id": "btr_1"})
    (rec,) = store.cycles(U)
    assert rec.trigger == "bot" and rec.result["bot_trigger_id"] == "btr_1"
    assert rec.result["trigger"] == "bot" and rec.result["patch_ids"] == list(res.patch_ids)
    (ch,) = c.changes(U, _data(events, dom))
    assert ch.why.startswith("之前总觉得你比实际更")     # the plain reason; the Bot's reason is no longer prefixed
    other = MemoryRsiStore()
    _cycle(other).run(U, _data(events, dom), trigger="manual", now=NOW)
    (plain,) = _cycle(other).changes(U, _data(events, dom))
    assert plain.why == ch.why


def test_manual_run_accepts_then_undo(oc):
    events, dom, _ = oc
    store = MemoryRsiStore()
    c = _cycle(store)
    res = c.run(U, _data(events, dom), trigger="manual", now=NOW)
    assert res.ran and res.trigger == "manual"
    assert res.a.startswith("accepted ") and len(res.patch_ids) == 1, res
    pid = res.patch_ids[0]
    assert registry.current(store, U, "A").version == 2
    assert ip.get(registry.infer_for(store, U), SELF) < ip.get(D, SELF)
    (rec,) = store.patches(U)
    assert rec.patch.status == "personal_accepted" and rec.patch.from_version == 1
    assert rec.patch.to_version == 2 and rec.gate_runs == 1
    body = rec.patch.body
    assert isinstance(body, FitPatchOp) and SELF in body.values
    assert rec.patch.localization.ref == "fit" and rec.patch.localization.failure == "overconfident"
    (m,) = store.memory(U)
    assert m.entry.status == "personal_accepted" and m.entry.online_helpful == pytest.approx(1.0)
    assert m.entry.delta_utility is not None and m.entry.delta_utility > 0
    assert m.direction == 0 and m.complexity == len(body.values) and m.entry.path == "fit"
    assert m.entry.signature == "overconfident|fit|*" and m.from_value is None and m.to_value is None

    (ch,) = c.changes(U, _data(events, dom))
    assert ch.track == "A" and ch.can_undo and ch.patch_id == pid
    assert "你自己说的话" in ch.summary
    assert ch.why == "之前总觉得你比实际更会，这样调一下更准"
    assert ch.signals[1].endswith("道题") and ch.before.startswith("确认题预测误差")
    assert float(ch.after) < float(ch.before.rsplit(" ", 1)[1])

    undone = c.undo(U, pid, now=NOW + timedelta(hours=1))
    assert undone.patch.status == "rolled_back"
    assert registry.current(store, U, "A").version == 3
    assert registry.infer_for(store, U) == D
    (m,) = store.memory(U)
    assert m.entry.status == "rolled_back" and m.entry.online_harmful == pytest.approx(1.0)
    (ch,) = c.changes(U, _data(events, dom))
    assert ch.status == "rolled_back" and not ch.can_undo
    with pytest.raises(ValueError):
        c.undo(U, pid, now=NOW + timedelta(hours=2))
    with pytest.raises(LookupError):
        c.undo(U, "nope", now=NOW)


def test_cycle_record_has_recursion_metrics(oc):
    events, dom, _ = oc
    store = MemoryRsiStore()
    _cycle(store).run(U, _data(events, dom), trigger="manual", now=NOW)
    (cr,) = store.cycles(U)
    assert cr.trigger == "manual" and cr.result["a"].startswith("accepted")
    rm = cr.result["recursion_metrics"]
    assert rm["A"]["proposals"] == 1 and rm["A"]["accepted"] == 1
    assert set(rm) == {"A"}                              # track B retired (2026-10-29)


# ---- negative closes and memory ----

def test_wrong_fit_gate_fails_and_is_remembered(oc, monkeypatch):
    events, dom, _ = oc
    monkeypatch.setattr(cycle_mod, "fit_infer", _stub_fit(1.5))
    store = MemoryRsiStore()
    c = _cycle(store)
    res = c.run(U, _data(events, dom), trigger="manual", now=NOW)
    assert res.a.startswith("gate_failed ")
    (rec,) = store.patches(U)
    assert rec.patch.status == "gate_failed" and rec.patch.body.values == {SELF: 1.5}
    assert registry.current(store, U, "A").version == 1
    (m,) = store.memory(U)
    assert m.entry.status == "gate_failed" and m.entry.online_harmful == pytest.approx(1.0)
    assert m.entry.path == "fit" and m.direction == 0 and m.to_value is None
    assert not m.repeat_of_rejected and m.complexity == 1
    (ch,) = c.changes(U, _data(events, dom))
    assert ch.status == "gate_failed" and not ch.can_undo
    assert "你自己说的话" in ch.summary
    # no cooling / repeat machinery on fits: the next run fits and gates again
    res2 = c.run(U, _data(events, dom), trigger="manual", now=NOW + timedelta(hours=1))
    assert res2.a.startswith("gate_failed ") and len(store.patches(U)) == 2


# ---- candidate, re-gate ----

def _small():
    """A 48-outcome log (prospective split), the moment after it, and later fuzzy checks on the
    self-reported cells (`rounds` × 12 outcomes, ≈70% `~`)."""
    events, dom = scenarios.overconfident_self(n_concepts=6)
    at = max(e.ts for e in events) + timedelta(minutes=1)

    def later(rounds):
        rng = random.Random(7)
        ts, out = at, []
        for r in range(rounds):
            for i in range(6):
                for a in (Aspect.TERM, Aspect.INTERPRET):
                    ts += timedelta(hours=2)
                    pol = "~" if rng.random() < 0.7 else "+"
                    out.append(scenarios._obs(f"late{r}-{i}-{a.value}", ts, cell_id(f"oc{i}", a),
                                              "check", pol))
        return out, ts + timedelta(minutes=1)
    return events, dom, at, later


def test_small_log_candidate_then_regated():
    small, dom, at, later = _small()
    store = MemoryRsiStore()
    c = _cycle(store)
    res = c.run(U, _data(small, dom), trigger="manual", now=at)
    assert res.a.startswith("candidate "), res
    pid = res.patch_ids[0]
    (rec,) = store.patches(U)
    assert rec.patch.status == "candidate" and rec.cutoff == at and rec.gate_runs == 0
    assert store.memory(U) == []
    (ch,) = c.changes(U, _data(small, dom))
    assert ch.status == "candidate" and not ch.can_undo and "你自己说的话" in ch.summary

    extra, at2 = later(5)
    res2 = c.run(U, _data(small + extra, dom), trigger="manual", now=at2)
    assert res2.a == f"accepted {pid}" and res2.patch_ids == (pid,)
    assert len(store.patches(U)) == 1                    # re-gated, not re-fitted
    (rec,) = store.patches(U)
    assert rec.gate_runs == 1 and rec.patch.status == "personal_accepted"


def test_candidate_waits_without_new_outcomes():
    small, dom, at, later = _small()
    store = MemoryRsiStore()
    c = _cycle(store)
    pid = c.run(U, _data(small, dom), trigger="manual", now=at).patch_ids[0]
    extra, at2 = later(1)
    extra = extra[:9]                                    # only 9 new held-out outcomes
    res = c.run(U, _data(small + extra, dom), trigger="manual", now=at2)
    assert res.a == f"waiting {pid}" and res.patch_ids == ()
    assert len(store.patches(U)) == 1


def test_too_few_outcomes(oc):
    events, dom, outs = oc
    small, at = _until(events, outs, 19)
    res = _cycle(MemoryRsiStore()).run(U, _data(small, dom), trigger="manual", now=at)
    assert res.a == "too_few_outcomes"


def test_fit_no_change(oc, monkeypatch):
    events, dom, _ = oc
    monkeypatch.setattr(cycle_mod, "fit_infer", _stub_fit(ip.get(D, SELF) + 0.01))   # < 1 % of [0, 2]
    store = MemoryRsiStore()
    res = _cycle(store).run(U, _data(events, dom), trigger="m", now=NOW)
    assert res.a == "fit_no_change" and res.patch_ids == ()
    assert store.patches(U) == [] and store.memory(U) == []


# ---- caps, once per run ----

def _accepted(pid, at):
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref=SELF,
                       signals=("x",), reason="r")
    p = Patch(patch_id=pid, track="A", localization=loc, body=ParamPatchOp(track="A", path=SELF, value=0.5),
              why="w", status="personal_accepted", from_version=1, to_version=1, created_at=at)
    return PatchRecord(user_id=U, patch=p, cutoff=at, gate_runs=1, updated_at=at)


def test_daily_accept_cap_keeps_candidate(oc):
    events, dom, _ = oc
    store = MemoryRsiStore()
    for i in range(2):
        store.put_patch(_accepted(f"old{i}", NOW - timedelta(hours=i + 1)))
    res = _cycle(store).run(U, _data(events, dom), trigger="m", now=NOW)
    assert res.a.startswith("capped ")
    rec = next(r for r in store.patches(U) if r.patch.patch_id == res.patch_ids[0])
    assert rec.patch.status == "candidate" and rec.gate_runs == 1
    assert registry.current(store, U, "A").version == 1


def test_at_most_one_a_patch_per_run(oc):
    events, dom, _ = oc
    store = MemoryRsiStore()
    c = _cycle(store)
    for k in range(3):
        before = len(store.patches(U))
        res = c.run(U, _data(events, dom), trigger="m", now=NOW + timedelta(days=k))
        assert len(store.patches(U)) - before <= 1 and len(res.patch_ids) <= 1


# ---- no improvement round with the harness loop off ----

def test_harness_off_runs_no_improvement_round(oc, monkeypatch):
    events, dom, _ = oc
    monkeypatch.setattr(cycle_mod, "fit_infer", _stub_fit(ip.get(D, SELF)))   # track A: no change
    store = MemoryRsiStore()
    res = _cycle(store).run(U, _data(events, dom), trigger="m", now=NOW)
    assert res.b == "off" and res.cost_usd == 0.0
    assert store.cycles(U)[-1].result["b"] == "off"


# ---- pre-A1 set_param rows stay readable and undoable; track-B bodies never reach track-A code ----

def _legacy(store, status="personal_accepted"):
    """A pre-A1 accepted set_param patch (self 0.60 → 0.50) with its version and memory row."""
    registry.commit(store, U, "A", infer=ip.set_(D, SELF, 0.5), patch_id="old", now=NOW)
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref=SELF,
                       signals=("x",), reason="领域 d1 中主要依据「你的自述」的知识点")
    p = Patch(patch_id="old", track="A", localization=loc,
              body=ParamPatchOp(track="A", path=SELF, value=0.5), why="自述偏乐观", status=status,
              from_version=1, to_version=2, created_at=NOW)
    store.put_patch(PatchRecord(user_id=U, patch=p, cutoff=NOW, gate_runs=1, updated_at=NOW))
    entry = PatchMemoryEntry(patch_id="old", track="A", signature=signature("overconfident", SELF, "d1"),
                             path=SELF, gate=GateResult(patch_id="old", channel="A", passed=True),
                             status=status, at=NOW)
    store.put_memory(MemoryRecord(user_id=U, entry=entry, direction=-1, from_value=0.6, to_value=0.5,
                                  counts_at=NOW, repeat_of_rejected=False, outcomes_at_close=10))


def test_pre_a1_param_patch_is_listed_and_undone(oc):
    events, dom, _ = oc
    store = MemoryRsiStore()
    _legacy(store)
    c = _cycle(store)
    (ch,) = c.changes(U, _data(events, dom))
    assert ch.summary == "你自己说的话没那么被相信了" and ch.can_undo
    out = c.undo(U, "old", now=NOW + timedelta(hours=1))
    assert out.patch.status == "rolled_back" and registry.infer_for(store, U) == D
    (m,) = store.memory(U)
    assert m.entry.status == "rolled_back" and m.from_value == 0.6 and m.to_value == 0.5


def test_track_a_code_refuses_a_spec_body():
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref=SELF,
                       signals=("x",), reason="r")
    p = Patch(patch_id="bad", track="A", localization=loc, body=SpecPatchOp(op="add_item", text="t"),
              why="w", from_version=1, created_at=NOW)
    with pytest.raises(TypeError):
        RsiCycle._a_values(p)
    with pytest.raises(TypeError):                       # a track-B set_param is not a θ_infer change
        RsiCycle._a_values(p.model_copy(update={"body": ParamPatchOp(track="B", path=SELF, value=1.0)}))


# ---- maybe_run, online check, utc ----

def test_maybe_run(oc, monkeypatch):
    events, dom, _ = oc
    monkeypatch.setattr(cycle_mod, "fit_infer", _stub_fit(ip.get(D, SELF)))
    store = MemoryRsiStore()
    store.put_settings(U, RsiSettings(cycle="every_5_feedback"))
    c = _cycle(store)
    assert c.maybe_run(U, _data(events, dom, signals=4), now=NOW) is None
    res = c.maybe_run(U, _data(events, dom, signals=5), now=NOW)
    assert res is not None and res.trigger == "every_5_feedback"
    store.put_settings(U, RsiSettings(cycle="manual"))
    assert c.maybe_run(U, _data(events, dom, signals=50), now=NOW) is None


def test_long_term_outcome_after_acceptance(oc, monkeypatch):
    from evowiki.teach.layer2.store import MemoryLayer2Store

    events, dom, outs = oc
    part, at = _until(events, outs, 200)
    store, l2 = MemoryRsiStore(), MemoryLayer2Store()
    c = _cycle(store, layer2=l2)
    pid = c.run(U, _data(part, dom), trigger="m", now=at).patch_ids[0]
    assert store.patches(U)[0].patch.status == "personal_accepted"
    assert store.cycles(U)[-1].result["outcomes"] == []      # measured before the acceptance
    assert l2.outcome_points(U) == []                    # nothing after the change yet
    monkeypatch.setattr(cycle_mod, "fit_infer",          # the second fit changes nothing
                        _stub_fit(ip.get(registry.infer_for(store, U), SELF)))
    res = c.run(U, _data(events, dom), trigger="m", now=NOW)
    assert res.a == "fit_no_change", res
    (o,) = store.cycles(U)[-1].result["outcomes"]
    assert o["change_id"] == pid and o["n"] >= 10 and o["point"] and not o["rolled_back"]
    assert o["verdict"] == "helpful", o
    (pt,) = l2.outcome_points(U, pid)
    assert pt.verdict == o["verdict"] and {d.metric for d in pt.deltas} == {"pred_logloss", "pred_auc"}
    m = next(r for r in store.memory(U) if r.entry.patch_id == pid)
    assert m.entry.long_term == o["verdict"]
    assert m.entry.online_helpful > 1.0                  # acceptance + the new helpful
    c.run(U, _data(events, dom), trigger="m", now=NOW + timedelta(hours=1))   # no new outcomes
    assert store.cycles(U)[-1].result["outcomes"] == []      # not recomputed (bounded work)
    assert len(l2.outcome_points(U, pid)) == 1


def test_harmful_accepted_change_is_rolled_back_automatically(oc, monkeypatch):
    """An accepted change whose new θ predicts worse after it went live (self-reports trusted
    far more on an overconfident learner) is undone by the cycle, with the reason shown."""
    from evowiki.teach.cycle import A_ROLLBACK_REASON
    from evowiki.teach.layer2.store import MemoryLayer2Store

    events, dom, outs = oc
    store, l2 = MemoryRsiStore(), MemoryLayer2Store()
    c = _cycle(store, layer2=l2)
    applied = outs[len(outs) // 3].ts
    bad = ip.set_(D, SELF, 1.8)
    v = registry.commit(store, U, "A", infer=bad, patch_id="bad", now=applied)
    loc = Localization(track="A", failure="underconfident", target="infer_param", ref="fit",
                       signals=("x",), reason="r")
    patch = Patch(patch_id="bad", track="A", localization=loc,
                  body=FitPatchOp(values={SELF: 1.8}, intervals={SELF: (1.7, 1.9)}, model_version=1),
                  why="w", status="personal_accepted", from_version=1, to_version=v.version,
                  created_at=applied)
    store.put_patch(PatchRecord(user_id=U, patch=patch, cutoff=applied, gate_runs=1,
                                updated_at=applied))
    store.put_memory(c._new_memory(U, patch, status="personal_accepted",
                                   gate=GateResult(patch_id="bad", channel="A", passed=True),
                                   domain=None, old=D, n_outcomes=0, now=applied))
    monkeypatch.setattr(cycle_mod, "fit_infer", _stub_fit(1.8))      # the fit changes nothing
    part, at1 = _until(events, outs, 2 * len(outs) // 3)
    c.run(U, _data(part, dom), trigger="m", now=at1)
    (o,) = store.cycles(U)[-1].result["outcomes"]       # one harmful look is not enough
    assert o["verdict"] == "harmful" and o["point"] and not o["rolled_back"]
    assert store.patches(U)[0].patch.status == "personal_accepted"
    assert store.memory(U)[0].entry.long_term == "harmful"
    res = c.run(U, _data(events, dom), trigger="m", now=NOW)
    assert res.a == "fit_no_change", res
    (o,) = store.cycles(U)[-1].result["outcomes"]
    assert o["change_id"] == "bad" and o["verdict"] == "harmful" and o["rolled_back"]
    assert o["note"] == A_ROLLBACK_REASON
    (rec,) = store.patches(U)
    assert rec.patch.status == "rolled_back"
    assert ip.get(registry.infer_for(store, U), SELF) == pytest.approx(ip.get(D, SELF))
    (m,) = store.memory(U)
    assert m.entry.status == "rolled_back" and m.entry.long_term == "harmful"
    assert m.entry.online_harmful == pytest.approx(1.0)
    from evowiki.teach.layer2.outcomes import NOTE_AUTO_ROLLBACK
    _, pt, final = l2.outcome_points(U, "bad")
    d = next(x for x in pt.deltas if x.metric == "pred_logloss")
    assert pt.verdict == "harmful" and d.hi < 0 and "变差" in pt.note
    assert final.note == NOTE_AUTO_ROLLBACK and final.deltas == pt.deltas
    (ch,) = c.changes(U, _data(events, dom))
    assert "你自己说的话" in ch.summary and not ch.why.startswith(A_ROLLBACK_REASON)
    assert not ch.can_undo and ch.effect_line.endswith("变差，已自动撤回")


def test_naive_now_rejected(oc):
    events, dom, _ = oc
    with pytest.raises(ValueError):
        _cycle(MemoryRsiStore()).run(U, _data(events, dom), trigger="m",
                                     now=datetime(2026, 6, 1))  # noqa: DTZ001


def test_writes_run_inside_store_transaction(oc):
    from contextlib import contextmanager

    events, dom, _ = oc

    class TxStore(MemoryRsiStore):
        def __init__(self):
            super().__init__()
            self.depth, self.opened, self.outside = 0, 0, 0

        @contextmanager
        def transaction(self):
            self.depth += 1
            self.opened += 1
            try:
                yield
            finally:
                self.depth -= 1

        def put_patch(self, rec):
            self.outside += self.depth == 0
            super().put_patch(rec)

        def add_cycle(self, rec):
            self.outside += self.depth == 0
            super().add_cycle(rec)

    store = TxStore()
    c = _cycle(store)
    pid = c.run(U, _data(events, dom), trigger="m", now=NOW).patch_ids[0]
    c.undo(U, pid, now=NOW + timedelta(hours=1))
    assert store.opened >= 3 and store.outside == 0


def test_a_plain_covers_every_infer_path():
    from evowiki.teach.infer_paths import PATHS
    assert set(PATHS) <= set(cycle_mod._A_PLAIN)
