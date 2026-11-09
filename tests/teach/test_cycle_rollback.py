"""Track-A rollback over overlapping fit patches (an older patch is not rolled back over a newer
one), who rolled a change back (final point notes, effect lines), and bounded per-run work."""

from datetime import UTC, datetime, timedelta

import pytest

from evowiki.teach import registry
from evowiki.teach.contracts import (
    AnchorDelta,
    FitPatchOp,
    InferParams,
    Localization,
    MethodChange,
    OutcomeOut,
    Patch,
)
from evowiki.teach.cycle import RsiCycle, UserData
from evowiki.teach.layer2 import method_registry as mr
from evowiki.teach.layer2.outcomes import (
    NOTE_AUTO_ROLLBACK,
    NOTE_ROLLBACK_BLOCKED,
    NOTE_USER_UNDO,
)
from evowiki.teach.layer2.store import MAX_SHOWN_POINTS, MemoryLayer2Store
from evowiki.teach.rsi_store import MemoryRsiStore, PatchRecord
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

U = "u1"
T = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
DATA = UserData(events=(), domain_of={}, domain_label={}, signals_since=lambda t: 0)


def _cycle(store, l2):
    return RsiCycle(store, ASPECT_CONFIG_V1, new_id=lambda p: f"{p}_1", layer2=l2)


def _fit(store, pid, values, at, from_v, to_v):
    loc = Localization(track="A", failure="overconfident", target="infer_param", ref="fit",
                       signals=("e1",), reason="r")
    p = Patch(patch_id=pid, track="A", localization=loc, why="w", status="personal_accepted",
              body=FitPatchOp(values=values, model_version=1), from_version=from_v,
              to_version=to_v, created_at=at)
    store.put_patch(PatchRecord(user_id=U, patch=p, cutoff=at, updated_at=at))


def _two_overlapping():
    """P1 slip 0.10 → 0.20 (v2), then P2 slip 0.20 → 0.24 and guess_scale (v3)."""
    store, l2 = MemoryRsiStore(), MemoryLayer2Store()
    v2 = registry.commit(store, U, "A", infer=InferParams(slip=0.2), patch_id="p1", now=T)
    _fit(store, "p1", {"slip": 0.2}, T, 1, v2.version)
    v3 = registry.commit(store, U, "A", infer=InferParams(slip=0.24, guess_scale=1.2),
                         patch_id="p2", now=T + timedelta(days=1))
    _fit(store, "p2", {"slip": 0.24, "guess_scale": 1.2}, T + timedelta(days=1), v2.version,
         v3.version)
    return store, l2


def _harm(cid, n, at):
    return OutcomeOut(change_id=cid, at=at, verdict="harmful", note="变差",
                      deltas=(AnchorDelta(metric="pred_logloss", before=0.4, after=0.5,
                                          delta=-0.1, lo=-0.2, hi=-0.05, n_before=n, n_after=n),))


def test_undo_of_an_older_fit_is_refused_while_a_newer_one_holds_its_values():
    store, l2 = _two_overlapping()
    c = _cycle(store, l2)
    rows = {r.patch_id: r for r in c.changes(U, DATA)}
    assert not rows["p1"].can_undo and rows["p2"].can_undo
    with pytest.raises(ValueError, match="p2"):
        c.undo(U, "p1", now=T + timedelta(days=2))
    assert registry.infer_for(store, U).slip == pytest.approx(0.24)          # nothing written
    # undo the newer one first: its values go back to P1's; then P1 can be undone
    c.undo(U, "p2", now=T + timedelta(days=2))
    infer = registry.infer_for(store, U)
    assert infer.slip == pytest.approx(0.2) and infer.guess_scale == pytest.approx(1.0)
    rows = {r.patch_id: r for r in c.changes(U, DATA)}
    assert rows["p1"].can_undo and rows["p2"].effect_line == "你撤销了"
    c.undo(U, "p1", now=T + timedelta(days=3))
    assert registry.infer_for(store, U).slip == pytest.approx(InferParams().slip)
    (pt,) = l2.outcome_points(U, "p1")
    assert pt.note == NOTE_USER_UNDO and pt.verdict == "pending"
    row = next(r for r in c.changes(U, DATA) if r.patch_id == "p1")
    assert row.status == "rolled_back" and row.effect_line == "你撤销了"


def test_a_user_undo_after_a_harmful_point_is_not_called_automatic():
    store, l2 = _two_overlapping()
    l2.add_outcome_point(U, _harm("p2", 20, T + timedelta(days=2)))     # one unconfirmed look
    c = _cycle(store, l2)
    c.undo(U, "p2", now=T + timedelta(days=3))
    row = next(r for r in c.changes(U, DATA) if r.patch_id == "p2")
    assert row.effect_line == "你撤销了"
    assert l2.outcome_points(U, "p2")[-1].note == NOTE_USER_UNDO


def test_automatic_rollback_blocked_by_a_newer_fit_is_skipped_and_retried_later():
    store, l2 = _two_overlapping()
    l2.add_outcome_point(U, _harm("p1", 20, T + timedelta(days=2)))
    l2.add_outcome_point(U, _harm("p1", 30, T + timedelta(days=3)))     # confirmed harmful
    c = _cycle(store, l2)
    (row,) = c._apply_outcomes_a(U, [], T + timedelta(days=4))
    assert row["change_id"] == "p1" and not row["rolled_back"]
    assert row["note"] == NOTE_ROLLBACK_BLOCKED
    assert registry.infer_for(store, U).slip == pytest.approx(0.24)          # P2 untouched
    assert l2.outcome_points(U, "p1")[-1].note == NOTE_ROLLBACK_BLOCKED
    line = next(r for r in c.changes(U, DATA) if r.patch_id == "p1").effect_line
    assert line.endswith("没有自动撤回")
    assert c._apply_outcomes_a(U, [], T + timedelta(days=4, hours=1)) == []   # no repeat marker
    assert len(l2.outcome_points(U, "p1")) == 3
    # the user undoes P2; the next harmful point (more evidence) rolls P1 back automatically
    c.undo(U, "p2", now=T + timedelta(days=5))
    l2.add_outcome_point(U, _harm("p1", 40, T + timedelta(days=6)))
    (row,) = c._apply_outcomes_a(U, [], T + timedelta(days=6, hours=1))
    assert row["rolled_back"]
    assert registry.infer_for(store, U).slip == pytest.approx(InferParams().slip)
    assert l2.outcome_points(U, "p1")[-1].note == NOTE_AUTO_ROLLBACK
    row = next(r for r in c.changes(U, DATA) if r.patch_id == "p1")
    assert row.effect_line.endswith("变差，已自动撤回")


def test_method_undo_writes_the_user_point_and_the_list_keeps_the_newest_points():
    store, l2 = MemoryRsiStore(), MemoryLayer2Store()
    from evowiki.teach.contracts import DEFAULT_FIT_PATHS, FitModel
    v = mr.commit(l2, U, "A2", fit_model=FitModel(version=1, paths=DEFAULT_FIT_PATHS[:2]),
                  change_id="a2_x", now=T)
    l2.put_method_change(U, MethodChange(change_id="a2_x", kind="A2", status="applied",
                                         summary="s", why="w", from_version=1,
                                         to_version=v.version, created_at=T))
    for i in range(MAX_SHOWN_POINTS + 5):
        l2.add_outcome_point(U, OutcomeOut(change_id="a2_x", at=T + timedelta(hours=i),
                                           verdict="no_effect"))
    c = _cycle(store, l2)
    row = next(r for r in c.changes(U, DATA) if r.patch_id == "a2_x")
    assert len(row.effect) == MAX_SHOWN_POINTS and row.effect[-1].at == T + timedelta(hours=24)
    c.undo_method(U, "a2_x", now=T + timedelta(days=2))
    row = next(r for r in c.changes(U, DATA) if r.patch_id == "a2_x")
    assert row.effect_line == "你撤销了" and row.effect[-1].note == NOTE_USER_UNDO


def test_accepted_patches_are_not_measured_without_new_evidence(monkeypatch):
    """`_measure_a` runs the outcome replays only for a patch whose outcomes after acceptance grew
    by MIN_EVENTS since its last point (the count is one shared replay)."""
    from evowiki.teach import cycle as cycle_mod
    from tests.teach.fit.conftest import quiz_log

    events, dom = quiz_log(60)
    store, l2 = MemoryRsiStore(), MemoryLayer2Store()
    applied = events[29].ts
    v2 = registry.commit(store, U, "A", infer=InferParams(slip=0.2), patch_id="p1", now=applied)
    _fit(store, "p1", {"slip": 0.2}, applied, 1, v2.version)
    calls = []
    real = cycle_mod.outcome_a

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(cycle_mod, "outcome_a", counting)
    data = UserData(events=events, domain_of=dom, domain_label={}, signals_since=lambda t: 0)
    c = _cycle(store, l2)
    now = events[-1].ts + timedelta(minutes=1)
    assert len(c._measure_a(U, data, now)) == 1 and len(calls) == 1
    ((_, o),) = c._measure_a(U, data, now)
    l2.add_outcome_point(U, o)
    calls.clear()
    assert c._measure_a(U, data, now + timedelta(hours=1)) == [] and calls == []
