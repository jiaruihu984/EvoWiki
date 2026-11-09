"""Long-term outcomes of one change (teach/layer2/outcomes.py) and the outcome series rule
(teach/layer2/store.py). Synthetic data only."""

from datetime import timedelta
from types import MappingProxyType

import pytest

from evowiki.teach import anchors
from evowiki.teach.contracts import AnchorDelta, InferParams, OutcomeOut
from evowiki.teach.layer2.outcomes import (
    NOTE_AUTO_ROLLBACK,
    NOTE_USER_UNDO,
    ChangeRef,
    outcome_a,
    outcome_method,
    undone_by_user,
)
from evowiki.teach.layer2.store import (
    MemoryLayer2Store,
    add_final_point,
    confirmed_harmful,
    due_for_point,
    is_new_point,
    record_point,
)
from evowiki.teach.predict import predict_outcomes
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach.fit.conftest import TRUE_SLIP, quiz_log
from tests.teach.spec_helpers import at

OLD = InferParams()                                  # slip 0.10
TRUE = InferParams(slip=TRUE_SLIP)


@pytest.fixture(scope="module")
def qlog():
    events, dom = quiz_log(400)
    outs = predict_outcomes(events, ASPECT_CONFIG_V1, OLD, dom)
    return events, dom, outs


def _ref(cid="c1", applied=None, kind="A:fit", layer=0):
    return ChangeRef(change_id=cid, layer=layer, kind=kind, applied_at=applied or at(0),
                     method_version=1, signal_kinds=())


# ---- track A ----

def test_outcome_a_true_theta_is_helpful_after_enough_outcomes(qlog):
    events, dom, outs = qlog
    now = outs[-1].ts + timedelta(minutes=1)
    ref = _ref(applied=outs[0].ts - timedelta(minutes=1))
    o = outcome_a(ref, events, ASPECT_CONFIG_V1, dom, OLD, TRUE, now=now)
    ll = next(d for d in o.deltas if d.metric == "pred_logloss")
    assert ll.n_after == len(outs) and ll.delta > 0 and ll.lo > 0
    assert {d.metric for d in o.deltas} == {"pred_logloss", "pred_auc"}
    assert o.verdict == "helpful" and o.change_id == "c1" and o.at == now
    assert o.note.startswith("改动后") and "变好" in o.note
    # the swapped change (true → wrong) is harmful
    assert outcome_a(ref, events, ASPECT_CONFIG_V1, dom, TRUE, OLD, now=now).verdict == "harmful"


def test_outcome_a_pending_before_min_events(qlog):
    events, dom, outs = qlog
    k = anchors.MIN_EVENTS - 1
    ref = _ref(applied=outs[-k - 1].ts)             # exactly k outcomes after the change
    o = outcome_a(ref, events, ASPECT_CONFIG_V1, dom, OLD, TRUE, now=outs[-1].ts)
    assert o.verdict == "pending" and all(d.n_after == k for d in o.deltas)
    assert o.note.startswith("还在观察")
    # outcomes after `now` do not count
    early = outcome_a(_ref(applied=outs[0].ts - timedelta(minutes=1)), events, ASPECT_CONFIG_V1,
                      dom, OLD, TRUE, now=outs[4].ts)
    assert early.verdict == "pending" and early.deltas[0].n_after == 5


def _delta(o: OutcomeOut, metric: str) -> AnchorDelta:
    return next(d for d in o.deltas if d.metric == metric)


def test_judge_is_bonferroni_over_deciding_metrics():
    # one marginal metric decides alone at 90 % but not at 90 % / k once a second metric decides
    xs_b, xs_a = [0.0, 1.0] * 10, [1.0] * 16 + [0.0] * 4
    one = anchors.rate_sampled("check_correct", xs_b, xs_a, higher_is_better=True, seed=4)
    flat = anchors.rate_sampled("review_recall", [1.0] * 20, [1.0] * 20, higher_is_better=True,
                                seed=5)
    tiny = anchors.rate_sampled("undo_rate", [0.0] * 3, [0.0] * 3, higher_is_better=False, seed=6)
    assert one.delta.lo > 0 and one.at(0.05).lo <= 0                  # premise of the test
    shown, v = anchors.judge([one, tiny])                             # k = 1 (tiny cannot decide)
    assert v == "helpful" and shown[0] == one.delta
    shown, v = anchors.judge([one, flat])                             # k = 2 → 95 % intervals
    assert v == "no_effect" and shown[0].lo == one.delta.lo           # display stays 90 %
    assert anchors.judge([tiny]) == ((tiny.delta,), "pending")


# ---- layer 2 ----

def _o(cid, v):
    return OutcomeOut(change_id=cid, at=at(0), verdict=v)


def test_outcome_method_child_helpful_and_undo_rate():
    base = [_o(f"b{i}", "helpful" if i < 3 else "no_effect") for i in range(12)]
    kids = [_o(f"k{i}", "helpful" if i < 10 else "no_effect") for i in range(12)] + [_o("kp", "pending")]
    ref = _ref(cid="m2", kind="A2:model", layer=2)
    o = outcome_method(ref, kids, base, now=at(5), undone={"b0", "b1", "b2", "b3"})
    h = _delta(o, "child_helpful")
    assert h.n_before == 12 and h.n_after == 12 and h.delta == pytest.approx(10 / 12 - 3 / 12)
    u = _delta(o, "undo_rate")
    assert u.n_after == 13 and u.delta == pytest.approx(4 / 12)
    assert o.verdict == "helpful"
    assert outcome_method(ref, kids[:3], base, now=at(5)).verdict == "pending"


# ---- the series rule ----

def _pt(v, n, t=0):
    return OutcomeOut(change_id="c", at=at(t), verdict=v,
                      deltas=(AnchorDelta(metric="check_correct", n_before=20, n_after=n),))


def test_series_appends_only_on_more_evidence_or_a_new_verdict():
    """A point needs MIN_EVENTS more evidence than the last one, or a new verdict."""
    assert not is_new_point(None, _pt("pending", 0))
    assert not is_new_point(None, _pt("pending", 9))
    assert is_new_point(None, _pt("pending", 10)) and is_new_point(None, _pt("no_effect", 3))
    st = MemoryLayer2Store()
    assert record_point(st, "u", _pt("pending", 10, 1))
    assert not record_point(st, "u", _pt("pending", 19, 2))
    assert record_point(st, "u", _pt("pending", 20, 3))
    assert record_point(st, "u", _pt("no_effect", 21, 4))
    assert [p.verdict for p in st.outcome_points("u", "c")] == ["pending", "pending", "no_effect"]
    assert st.outcome_points("u", "other") == [] and st.outcome_points("v") == []


def test_due_for_point_needs_min_events_more_evidence():
    assert not due_for_point([], 9) and due_for_point([], 10)
    assert not due_for_point([_pt("pending", 10)], 19) and due_for_point([_pt("pending", 10)], 20)


def test_final_point_copies_the_last_measurement_with_who_rolled_back():
    st = MemoryLayer2Store()
    add_final_point(st, "u", "c", NOTE_USER_UNDO, at(1))            # never measured: pending
    (p,) = st.outcome_points("u", "c")
    assert p.verdict == "pending" and p.note == NOTE_USER_UNDO
    st.add_outcome_point("u", _pt("harmful", 30, 2).model_copy(update={"change_id": "d"}))
    add_final_point(st, "u", "d", NOTE_AUTO_ROLLBACK, at(3))
    a, b = st.outcome_points("u", "d")
    assert b.verdict == "harmful" and b.deltas == a.deltas and b.note == NOTE_AUTO_ROLLBACK
    assert not confirmed_harmful([a, b])                     # same evidence never confirms
    assert undone_by_user([b], legacy_harmful=False) is False
    assert undone_by_user([p], legacy_harmful=True) is True
    assert undone_by_user([a], legacy_harmful=True) is False      # before the notes existed


# ---- anchors (Task 5 leftovers) ----

def test_logloss_delta_refuses_unpaired_inputs_and_sandbox_is_read_only():
    with pytest.raises(ValueError):
        anchors.logloss_delta([(0.5, 1.0)], [(0.5, 1.0), (0.5, 0.0)], seed=1)
    with pytest.raises(ValueError):
        anchors.auc_delta([(0.5, 1.0)], [], seed=1)
    assert isinstance(anchors.SANDBOX, MappingProxyType)
    with pytest.raises(TypeError):
        anchors.SANDBOX["A"] = "x"                  # type: ignore[index]


def test_auc_delta_matches_auc():
    old = [(0.4, 1.0), (0.6, 0.0), (0.5, 1.0), (0.5, 0.0)] * 5
    new = [(0.9, 1.0), (0.1, 0.0), (0.7, 1.0), (0.3, 0.0)] * 5
    d = anchors.auc_delta(old, new, seed=2)
    assert d.before == pytest.approx(anchors.auc(old)) and d.after == pytest.approx(anchors.auc(new))
    assert d.delta > 0 and d.lo > 0 and d.n_before == 20
    assert anchors.auc_delta([(0.3, 1.0)] * 12, [(0.6, 1.0)] * 12, seed=2).delta is None
