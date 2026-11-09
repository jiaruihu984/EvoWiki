"""Prequential outcome replay (channel A) and the scenario builders' properties."""

from datetime import UTC, datetime, timedelta

import pytest

from evowiki.belief.aspects import (
    AspectK,
    AspectRejected,
    Declare,
    DefineConcept,
    Observe,
    RevokeInference,
)
from evowiki.goals.contracts import Aspect, Band, CellBelief, Observation
from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import InferParams
from evowiki.teach.metrics import log_loss, paired_bootstrap, seed_of
from evowiki.teach.predict import OTHER_DOMAIN, Outcome, p_correct, predict_outcomes
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach import scenarios

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
D = InferParams()
BUILDERS = (scenarios.overconfident_self, scenarios.forgetting_fast, scenarios.calibrated)


def _obs(eid, hours, cell, kind, pol, strength=1.0):
    return Observe(event_id=eid, ts=T0 + timedelta(hours=hours), observation=Observation(
        cell=cell, polarity=pol, strength=strength, kind=kind, evidence_ref="r",
        quote="会" if kind == "self" else None))


def _defs():
    return [DefineConcept(event_id="dp", ts=T0, concept_id="p", aspects=tuple(Aspect)),
            DefineConcept(event_id="dc", ts=T0, concept_id="c", aspects=tuple(Aspect),
                          prereqs=("p",))]


def _run(events, infer=D, domain_of=None):
    return predict_outcomes(events, ASPECT_CONFIG_V1, infer, domain_of or {"c": "d1"})


_BASE_CACHE: dict[int, tuple[list, list[Outcome]]] = {}


def _base(events, domain_of) -> list[Outcome]:
    """Default replay, cached per events list (the scenario property tests sweep many θ)."""
    hit = _BASE_CACHE.get(id(events))
    if hit is None or hit[0] is not events:
        hit = _BASE_CACHE[id(events)] = (events, _run(events, D, domain_of))
    return hit[1]


def _gain(events, domain_of, cand: InferParams) -> float:
    """Mean log-loss gain (default − candidate). Both predict the same event ids (R3)."""
    base = _base(events, domain_of)
    out = _run(events, cand, domain_of)
    assert [o.event_id for o in out] == [o.event_id for o in base]
    diffs = [log_loss(a.p, a.y) - log_loss(b.p, b.y) for a, b in zip(base, out, strict=True)]
    return sum(diffs) / len(diffs)


# ---- p_correct ----

def _belief(m, recall=1.0):
    return CellBelief(cell="c.term", m=m, u=0.1, recall=recall, band=Band.NONE)


@pytest.mark.parametrize("kind", ["quiz", "check"])
def test_p_correct_quiz_check(kind):
    b = _belief(0.6, recall=0.5)
    q = 0.3
    assert p_correct(b, kind, D) == pytest.approx(0.9 * q + 0.25 * (1 - q))
    i = InferParams(slip=0.2, guess_scale=1.5)
    assert p_correct(b, kind, i) == pytest.approx(0.8 * q + 0.375 * (1 - q))


def test_p_correct_recall_and_clip():
    assert p_correct(_belief(0.6, 0.5), "recall", D) == pytest.approx(0.9 * 0.3)
    assert p_correct(_belief(0.0), "recall", D) == 0.02
    assert p_correct(_belief(1.0), "recall", InferParams(slip=0.0)) == 0.98
    with pytest.raises(ValueError):
        p_correct(_belief(0.5), "self", D)


def test_p_correct_guess_is_capped():
    # guess_scale's bound (1.5) keeps g <= 0.375 < MAX_GUESS; the cap is still applied
    i = InferParams(guess_scale=1.5, slip=0.0)
    assert p_correct(_belief(0.0), "quiz", i) == pytest.approx(0.375)


# ---- labels, filtering, bookkeeping ----

def test_labels_and_kinds():
    ev = _defs() + [
        _obs("a", 1, "c.term", "check", "+"), _obs("b", 2, "c.term", "check", "~"),
        _obs("c", 3, "c.term", "quiz", "-"), _obs("d", 4, "c.term", "recall", "~"),
        _obs("e", 5, "c.term", "recall", "-"), _obs("f", 6, "c.term", "recall", "+"),
        _obs("g", 7, "c.term", "self", "+"), _obs("h", 8, "c.term", "feedback", "-"),
        _obs("i", 9, "c.term", "ask", "~")]
    out = _run(ev)
    assert [(o.event_id, o.kind, o.y) for o in out] == [
        ("a", "check", 1.0), ("b", "check", 0.0), ("c", "quiz", 0.0), ("d", "recall", 0.5),
        ("e", "recall", 0.0), ("f", "recall", 1.0)]
    assert all(o.concept == "c" and o.cell == "c.term" and o.domain == "d1" for o in out)
    assert all(0.02 <= o.p <= 0.98 for o in out)


def test_weak_tests_and_declared_cells_are_not_predicted():
    ev = _defs() + [
        _obs("weak", 1, "c.term", "check", "+", strength=0.2),
        Declare(event_id="dk", ts=T0 + timedelta(hours=2), cell="c.purpose", value="know"),
        _obs("decl", 3, "c.purpose", "check", "-"),
        _obs("ok", 4, "c.notation", "quiz", "+")]
    assert [o.event_id for o in _run(ev)] == ["ok"]
    # targets are θ-free (R3): a candidate that weights checks down still scores the check
    low = ip.set_(D, "evidence_weight.check", 0.2)
    ev2 = _defs() + [_obs("x", 1, "c.term", "check", "+"), _obs("y", 2, "c.term", "quiz", "+")]
    assert [o.event_id for o in _run(ev2, low)] == ["x", "y"]


THETAS = (
    D,
    InferParams(slip=0.3),
    InferParams(guess_scale=1.5),
    InferParams(guess_scale=0.5, slip=0.0),
    ip.set_(D, "evidence_weight.check", 0.3),
    ip.set_(D, "evidence_weight.quiz", 0.0),
    ip.set_(ip.set_(D, "evidence_weight.recall", 2.0), "forgetting_scale", 0.5),
    InferParams(inference_strength=0.0, forgetting_scale=2.0,
                evidence_weight={**D.evidence_weight, "self": 0.0, "feedback": 2.0}),
)


def test_low_strength_targets_do_not_depend_on_theta_or_label():
    """R3: borderline tests (strength 0.26–0.3) are scored as the same set under every θ, for
    `+`, `-` and `~` alike; strength 0.24 is never a target."""
    ev = _defs()
    expected = []
    for i, (st, pol) in enumerate([(0.26, "+"), (0.26, "-"), (0.28, "~"), (0.3, "-"),
                                   (0.3, "+"), (0.27, "-"), (0.24, "+"), (0.24, "-")]):
        kind = ("check", "quiz", "recall")[i % 3]
        eid = f"t{i}"
        ev.append(_obs(eid, 1 + i * 30, "c.term" if i % 2 else "c.purpose", kind, pol, st))
        if st >= 0.25:
            expected.append(eid)
    for th in THETAS:
        assert [o.event_id for o in _run(ev, th)] == expected, th


@pytest.mark.parametrize("build", BUILDERS)
def test_target_set_is_theta_independent(build):
    events, dom = build(seed=2)
    ids = [o.event_id for o in _run(events, D, dom)]
    for th in THETAS[1:]:
        assert [o.event_id for o in _run(events, th, dom)] == ids, th


def test_domain_default_and_unknown():
    ev = _defs() + [_obs("a", 1, "c.term", "check", "+"), _obs("b", 2, "p.term", "check", "+")]
    out = _run(ev, domain_of={"c": "math"})
    assert [o.domain for o in out] == ["math", OTHER_DOMAIN]


def test_prior_kind_days_since_test_and_direct():
    ev = _defs() + [
        _obs("a", 1, "c.term", "check", "+"),         # nothing before: none
        _obs("s1", 2, "c.purpose", "self", "+"),
        _obs("s2", 3, "c.purpose", "self", "+"),
        _obs("q", 4, "c.purpose", "quiz", "+"),        # self dominates (1.6 vs 0)
        _obs("b", 30, "c.term", "recall", "-"),        # check before
        _obs("pp", 31, "c.procedure", "check", "+"),   # procedure + → notation/term inferred
        _obs("n", 32, "c.notation", "check", "+"),     # only inferred evidence
    ]
    out = {o.event_id: o for o in _run(ev)}
    assert out["a"].prior_kind == "none" and out["a"].days_since_test is None
    assert out["a"].direct == 0.0
    assert out["q"].prior_kind == "self" and out["q"].days_since_test is None
    assert out["q"].direct == pytest.approx(1.6)
    assert out["b"].prior_kind == "check"
    assert out["b"].days_since_test == pytest.approx(29 / 24)
    assert out["n"].prior_kind == "inferred"


def test_prior_kind_tracks_the_candidate_weights():
    ev = _defs() + [_obs("s", 1, "c.term", "self", "+"), _obs("k", 2, "c.term", "check", "+"),
                    _obs("k2", 3, "c.term", "check", "+")]
    by = {o.event_id: o.prior_kind for o in _run(ev)}
    assert by["k2"] == "check"                       # check 1.0 > self 0.8
    by2 = {o.event_id: o.prior_kind for o in _run(ev, ip.set_(D, "evidence_weight.self", 1.2))}
    assert by2["k2"] == "self"                       # self 1.6 > check 1.0


def test_rejections_fail_closed_except_noop_revoke():
    base = _defs() + [_obs("pp", 1, "c.procedure", "check", "+")]
    revoke = RevokeInference(event_id="rv", ts=T0 + timedelta(hours=2), source_event_id="pp")
    after = _obs("t", 3, "c.term", "check", "+")
    no_inf = InferParams(inference_strength=0.0)
    assert [o.event_id for o in _run(base + [revoke, after], no_inf)] == ["pp", "t"]
    assert [o.event_id for o in _run(base + [revoke, after])] == ["pp", "t"]
    bad = RevokeInference(event_id="rv2", ts=T0 + timedelta(hours=2), source_event_id="nope")
    with pytest.raises(AspectRejected):
        _run(base + [bad])
    revoke2 = RevokeInference(event_id="rv3", ts=T0 + timedelta(hours=3), source_event_id="pp")
    for th in (D, no_inf):                       # a second revoke fails under every θ
        with pytest.raises(AspectRejected):
            _run(base + [revoke, revoke2], th)
    with pytest.raises(AspectRejected):
        _run(base + [_obs("pp", 4, "c.term", "check", "+")])          # repeated id
    with pytest.raises(AspectRejected):
        _run(base + [_obs("u", 4, "zz.term", "check", "+")])          # unknown concept


# ---- prequential guarantees ----

@pytest.mark.parametrize("build", BUILDERS)
def test_deterministic(build):
    e1, d1 = build(seed=3)
    e2, d2 = build(seed=3)
    assert e1 == e2 and d1 == d2
    assert _run(e1, D, d1) == _run(e2, D, d2)
    assert build(seed=4)[0] != e1


@pytest.mark.parametrize("build", BUILDERS)
def test_no_lookahead(build):
    events, dom = build(seed=1)
    cand = InferParams(slip=0.2, forgetting_scale=0.7,
                       evidence_weight={**D.evidence_weight, "self": 0.3})
    full = _run(events, cand, dom)
    idx = {e.event_id: i for i, e in enumerate(events)}
    for o in full[:: max(1, len(full) // 25)] + [full[-1]]:
        i = idx[o.event_id]
        cut = _run(events[: i + 1], cand, dom)          # later events dropped
        assert cut[-1] == o
        assert _run(events[:i], cand, dom) == cut[:-1]  # the event itself is not seen either


def test_default_invariance_link():
    """With InferParams() the replayed K is exactly the pinned ASPECT_CONFIG_V1 K."""
    assert ASPECT_CONFIG_V1.with_infer(InferParams()) == ASPECT_CONFIG_V1
    events, dom = scenarios.calibrated(seed=0)
    out = {o.event_id: o for o in predict_outcomes(events, ASPECT_CONFIG_V1, D, dom)}
    k = AspectK(ASPECT_CONFIG_V1)
    seen = 0
    for ev in events:
        if ev.event_id in out:
            o = out[ev.event_id]
            assert o.p == p_correct(k.belief(o.cell, ev.ts), o.kind, ASPECT_CONFIG_V1.infer)
            seen += 1
        k.apply(ev)
    assert seen == len(out) > 0


def test_outcome_is_frozen():
    o = _run(_defs() + [_obs("a", 1, "c.term", "check", "+")])[0]
    assert isinstance(o, Outcome)
    with pytest.raises(AttributeError):
        o.p = 0.5


# ---- scenario properties (Tasks 5–7 rely on these) ----

@pytest.mark.parametrize("build", BUILDERS)
def test_scenarios_shape(build):
    events, dom = build(seed=0)
    out = _run(events, D, dom)
    assert len(out) >= 120
    assert {o.domain for o in out} == {"d1", "d2"}
    assert set(dom.values()) == {"d1", "d2"}
    assert {type(e).__name__ for e in events} <= {"DefineConcept", "Observe"}


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_overconfident_self_property(seed):
    events, dom = scenarios.overconfident_self(seed=seed)
    out = _run(events, D, dom)
    selfish = [o for o in out if o.prior_kind == "self"]
    assert len(selfish) >= 60
    assert sum(o.y for o in selfish) / len(selfish) < 0.45          # ≈70% fuzzy
    assert sum(o.p for o in selfish) / len(selfish) > 0.6           # the default believes them
    gains = {v: _gain(events, dom, ip.set_(D, "evidence_weight.self", v))
             for v in (0.0, 0.3, 1.2)}
    assert gains[0.0] > 0.06 and gains[0.3] > 0.03 and gains[1.2] < -0.03
    # self down is the best single-path move, clearly ahead of the next one
    others = [_gain(events, dom, ip.set_(D, p, v)) for p in ip.PATHS
              if p != "evidence_weight.self" for v in ip.bounds(p)]
    assert gains[0.0] > 1.4 * max(others)
    base = _run(events, D, dom)
    out0 = {o.event_id: o for o in _run(events, ip.set_(D, "evidence_weight.self", 0.0), dom)}
    diffs = [log_loss(o.p, o.y) - log_loss(out0[o.event_id].p, o.y) for o in base]
    assert paired_bootstrap(diffs, seed=seed_of(f"oc{seed}"), n=500)[1] > 0.0


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_forgetting_fast_property(seed):
    events, dom = scenarios.forgetting_fast(seed=seed)
    out = _run(events, D, dom)
    recalls = [o for o in out if o.kind == "recall"]
    checks = [o for o in out if o.kind == "check"]
    assert len(recalls) >= 120
    assert sum(o.y == 0.0 for o in recalls) / len(recalls) > 2 / 3     # mostly `-`
    assert sum(o.y for o in checks) / len(checks) > 0.5                # checks mostly `+`
    assert all(10.0 <= o.days_since_test <= 40.0 for o in recalls)
    gains = {v: _gain(events, dom, ip.set_(D, "forgetting_scale", v)) for v in (0.5, 0.75, 1.5)}
    # FSRS memory (scenarios doc): the right direction helps, monotonically, the wrong one hurts;
    # forgetting_scale is no longer the single best path (slip / start_known explain misses too)
    assert gains[0.5] > gains[0.75] > 0.001 and gains[1.5] < -0.002
    assert gains[0.5] > 0.003


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_calibrated_control(seed):
    events, dom = scenarios.calibrated(seed=seed)
    out = _run(events, D, dom)
    ybar, pbar = (sum(o.y for o in out) / len(out), sum(o.p for o in out) / len(out))
    assert abs(ybar - pbar) < 0.04     # truth = the predicting K: pure sampling noise, SD ≈0.017
    for p in ip.PATHS:
        lo, hi = ip.bounds(p)
        d0 = ip.get(D, p)
        for v in (lo, (lo + d0) / 2, (d0 + hi) / 2, hi):
            assert _gain(events, dom, ip.set_(D, p, v)) < 0.01, (p, v)
