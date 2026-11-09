from datetime import UTC, datetime

import pytest

from evowiki.belief.aspects import (
    AspectConfig,
    AspectK,
    AspectRejected,
    DefineConcept,
    Observe,
    RevokeInference,
)
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.contracts import Aspect, Band, Observation

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def chain() -> AspectK:
    """z needs var, var needs mean, mean needs base (3 hops away from z)."""
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default()))
    for i, (c, p) in enumerate([("base", ()), ("mean", ("base",)), ("var", ("mean",)),
                                ("z", ("var",))]):
        k.apply(DefineConcept(event_id=f"d{i}", ts=T0, concept_id=c, aspects=tuple(Aspect),
                              prereqs=p))
    return k


def see(k, eid, cell, pol, strength=1.0):
    k.apply(Observe(event_id=eid, ts=T0, observation=Observation(
        cell=cell, polarity=pol, strength=strength, kind="quiz", evidence_ref="ev")))


def test_within_concept_coupling():
    k = chain()
    see(k, "o", "z.procedure", "+")
    assert k.belief("z.notation", T0).inferred_pos == pytest.approx(0.4)
    assert k.belief("z.term", T0).inferred_pos == pytest.approx(0.3)
    assert k.belief("z.mechanism", T0).inferred_pos == 0
    assert k.belief("z.notation", T0).direct == 0


def test_prereq_hops_halve_and_stop_after_two():
    k = chain()
    see(k, "o", "z.procedure", "+")
    for a in ("term", "purpose", "notation"):
        assert k.belief(f"var.{a}", T0).inferred_pos == pytest.approx(0.5)
        assert k.belief(f"mean.{a}", T0).inferred_pos == pytest.approx(0.25)
        assert k.belief(f"base.{a}", T0).inferred_pos == 0
    assert k.belief("var.procedure", T0).inferred_pos == 0


def test_inferred_only_caps_at_likely():
    k = chain()
    for n in range(10):
        see(k, f"o{n}", "z.procedure", "+")
    b = k.belief("var.term", T0)
    assert b.inferred_pos == pytest.approx(5.0) and b.m > 0.7
    assert b.band is Band.LIKELY
    assert k.belief("mean.term", T0).band is Band.LIKELY  # 2.5 >= 0.5


def test_half_inference_is_still_none():
    k = chain()
    see(k, "o", "z.procedure", "+")
    assert k.belief("var.term", T0).band is Band.LIKELY
    assert k.belief("mean.term", T0).band is Band.NONE


def test_term_observation_does_not_hop():
    k = chain()
    see(k, "o", "z.term", "+")
    assert k.belief("var.term", T0).inferred_pos == 0


def test_not_knowing_lower_layer_lowers_dependants_weakly():
    k = chain()
    see(k, "o", "var.procedure", "-")
    for a in ("procedure", "notation"):
        assert k.belief(f"z.{a}", T0).inferred_neg == pytest.approx(0.15)
    assert k.belief("z.term", T0).inferred_neg == 0
    assert k.belief("mean.procedure", T0).inferred_neg == 0
    see(k, "f", "var.notation", "~")
    assert k.belief("z.procedure", T0).inferred_neg == pytest.approx(0.30)
    see(k, "t", "var.term", "-")
    assert k.belief("z.procedure", T0).inferred_neg == pytest.approx(0.30)


def test_revoke_removes_only_that_sources_inference():
    k = chain()
    see(k, "o1", "z.procedure", "+")
    see(k, "o2", "z.procedure", "+")
    k.apply(RevokeInference(event_id="r", ts=T0, source_event_id="o1"))
    assert k.belief("var.term", T0).inferred_pos == pytest.approx(0.5)
    assert k.belief("z.procedure", T0).direct == pytest.approx(2.0)   # direct untouched
    k.apply(RevokeInference(event_id="r2", ts=T0, source_event_id="o2"))
    assert k.belief("var.term", T0).band is Band.NONE
    with pytest.raises(AspectRejected):
        k.apply(RevokeInference(event_id="r3", ts=T0, source_event_id="o2"))
    with pytest.raises(AspectRejected):
        k.apply(RevokeInference(event_id="r4", ts=T0, source_event_id="nope"))


def test_hops_skip_undefined_prereqs_and_cycles_stay_bounded():
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default()))
    k.apply(DefineConcept(event_id="a", ts=T0, concept_id="x", aspects=tuple(Aspect),
                          prereqs=("y", "ghost")))
    k.apply(DefineConcept(event_id="b", ts=T0, concept_id="y", aspects=tuple(Aspect),
                          prereqs=("x",)))
    see(k, "o", "x.procedure", "+")
    assert k.belief("y.term", T0).inferred_pos == pytest.approx(0.5)
    assert k.belief("x.term", T0).inferred_pos == pytest.approx(0.3 + 0.25)  # within + hop 2


def test_revoke_event_round_trips_through_adapter_and_rejection_leaves_state():
    from evowiki.belief.aspects import AspectEventAdapter

    ev = AspectEventAdapter.validate_python(
        {"type": "revoke_inference", "event_id": "r", "ts": T0.isoformat(),
         "source_event_id": "o"})
    assert isinstance(ev, RevokeInference)
    k = chain()
    before = k.to_json()
    with pytest.raises(AspectRejected):
        k.apply(ev)
    assert k.to_json() == before


def test_zero_weight_observation_records_no_inference_so_revoke_is_rejected():
    k = chain()
    see(k, "o", "z.procedure", "+", strength=0.0)
    assert k.belief("var.term", T0).inferred_pos == 0
    with pytest.raises(AspectRejected):
        k.apply(RevokeInference(event_id="r", ts=T0, source_event_id="o"))


def _chain(trusted) -> AspectK:
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default()), trusted=trusted)
    for i, (c, p) in enumerate([("base", ()), ("mean", ("base",)), ("var", ("mean",)),
                                ("z", ("var",))]):
        k.apply(DefineConcept(event_id=f"d{i}", ts=T0, concept_id=c, aspects=tuple(Aspect),
                              prereqs=p))
    return k


def test_trusted_none_follows_every_stored_prereq_as_before():
    a, b = chain(), _chain(None)
    for k in (a, b):
        see(k, "o", "z.procedure", "+")
        see(k, "x", "var.notation", "-")
    assert a.to_json() == b.to_json()


def test_untrusted_prereq_is_kept_but_never_followed():
    """Relation layers: K keeps every stored prereq; inference follows only the trusted pairs
    (the graph's verified prerequisites), up (hop) and down (dependants)."""
    k = _chain(frozenset({("mean", "var")}))       # var→z and base→mean are not verified
    assert k.prereqs("z") == ("var",) and k.inference_prereqs("z") == ()
    assert k.inference_prereqs("var") == ("mean",)
    see(k, "o", "z.procedure", "+")
    assert all(k.belief(f"{c}.term", T0).inferred_pos == 0 for c in ("var", "mean", "base"))
    see(k, "p", "var.procedure", "+")               # var → mean is trusted, mean → base is not
    assert k.belief("mean.term", T0).inferred_pos == pytest.approx(0.5)
    assert k.belief("base.term", T0).inferred_pos == 0
    see(k, "q", "var.notation", "-")                # down: z is not a trusted dependant of var
    assert k.belief("z.procedure", T0).inferred_neg == 0
    see(k, "r", "mean.notation", "-")               # var is a trusted dependant of mean
    assert k.belief("var.procedure", T0).inferred_neg > 0


def test_copy_keeps_the_trusted_set():
    k = _chain(frozenset())
    assert k.copy().trusted == frozenset()


def test_a_prereq_verified_later_carries_earlier_evidence():
    """Retroactive prerequisites (user ruling 2026-10-24): replayed with the current trusted set,
    evidence recorded before A→B was verified (and before K stored it as a prereq) propagates."""
    cfg = AspectConfig.from_theta(ThetaInfer.default())
    log = [DefineConcept(event_id="dA", ts=T0, concept_id="A", aspects=tuple(Aspect)),
           DefineConcept(event_id="dB", ts=T0, concept_id="B", aspects=tuple(Aspect)),
           Observe(event_id="miss", ts=T0, observation=Observation(
               cell="A.notation", polarity="-", strength=1.0, kind="quiz", evidence_ref="ev")),
           Observe(event_id="hit", ts=T0, observation=Observation(
               cell="B.procedure", polarity="+", strength=1.0, kind="quiz", evidence_ref="ev"))]

    def replay(trusted, extra=()):
        k = AspectK(cfg, trusted=trusted)
        for ev in [*log, *extra]:
            k.apply(ev)
        return k

    before = replay(frozenset())
    assert before.belief("B.procedure", T0).inferred_neg == 0
    assert before.belief("A.term", T0).inferred_pos == 0
    # A→B verified now: ksync appends the prereq to the log only at verification time
    mirror = DefineConcept(event_id="dB2", ts=T0, concept_id="B", aspects=tuple(Aspect),
                           prereqs=("A",))
    after = replay(frozenset({("A", "B")}), [mirror])
    assert after.inference_prereqs("B") == ("A",)
    assert after.belief("B.procedure", T0).inferred_neg == pytest.approx(0.15)   # down from A
    assert after.belief("A.term", T0).inferred_pos == pytest.approx(0.5)        # hop up from B
    # the mirror itself changes nothing: the current trusted set decides
    same = replay(frozenset({("A", "B")}))
    assert same.belief("B.procedure", T0).inferred_neg == pytest.approx(0.15)
