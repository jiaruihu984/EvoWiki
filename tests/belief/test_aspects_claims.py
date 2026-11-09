"""Wrong-answer claims in K: slip sets aside part of a wrong answer, learned adds a weak positive;
a refuted resolution takes the claim back exactly, a confirmed one keeps it."""

from datetime import UTC, datetime, timedelta

import pytest

from evowiki.belief.aspects import (
    AspectK,
    AspectRejected,
    Claim,
    DefineConcept,
    Observe,
    ResolveClaim,
)
from evowiki.goals.contracts import Aspect, Observation, cell_id
from evowiki.teach.contracts import InferParams
from evowiki.teach.infer_paths import set_
from evowiki.teach.predict import predict_outcomes
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
A = next(iter(Aspect))
CELL = cell_id("c", A)


def _obs(eid, hours, pol, kind="check"):
    return Observe(event_id=eid, ts=T0 + timedelta(hours=hours), observation=Observation(
        cell=CELL, polarity=pol, strength=1.0, kind=kind, evidence_ref="r",
        quote="会" if kind == "self" else None))


def _claim(eid, claim, source="w1", hours=1):
    return Claim(event_id=eid, ts=T0 + timedelta(hours=hours), claim=claim, cell=CELL,
                 source_event_id=source)


def _resolve(eid, claim_id, outcome, hours=2):
    return ResolveClaim(event_id=eid, ts=T0 + timedelta(hours=hours), claim_event_id=claim_id,
                        outcome=outcome)


def _k(*events, infer=None):
    cfg = ASPECT_CONFIG_V1 if infer is None else ASPECT_CONFIG_V1.with_infer(infer)
    k = AspectK(cfg)
    k.apply(DefineConcept(event_id="dc", ts=T0, concept_id="c", aspects=tuple(Aspect)))
    for ev in events:
        k.apply(ev)
    return k


def _ab(k):
    c = k._cells[CELL]
    return c.a, c.b


def test_slip_sets_aside_its_share_of_the_wrong_answer_and_refuted_takes_it_back():
    before = _ab(_k(_obs("w1", 0, "~")))
    k = _k(_obs("w1", 0, "~"), _claim("cl", "slip"))
    a, b = _ab(k)
    assert a == pytest.approx(before[0] * 0.7) and b == pytest.approx(before[1] * 0.7)
    assert k.open_claims(CELL) == ("cl",)
    refuted = _k(_obs("w1", 0, "~"), _claim("cl", "slip"), _resolve("r", "cl", "refuted"))
    assert _ab(refuted) == pytest.approx(before) and refuted.open_claims(CELL) == ()
    confirmed = _k(_obs("w1", 0, "~"), _claim("cl", "slip"), _resolve("r", "cl", "confirmed"))
    assert _ab(confirmed) == pytest.approx((a, b)) and confirmed.open_claims(CELL) == ()


def test_learned_adds_a_weak_positive_until_refuted():
    base = _ab(_k(_obs("w1", 0, "-")))
    k = _k(_obs("w1", 0, "-"), _claim("cl", "learned"))
    assert _ab(k) == pytest.approx((base[0] + 0.3, base[1]))
    k2 = _k(_obs("w1", 0, "-"), _claim("cl", "learned"), _resolve("r", "cl", "refuted"))
    assert _ab(k2) == pytest.approx(base)


def test_the_trust_levels_come_from_theta_infer():
    inf = set_(set_(InferParams(), "evidence_weight.slip_claim", 1.0),
               "evidence_weight.learned_claim", 0.0)
    before = _ab(_k(_obs("w1", 0, "~"), infer=inf))
    assert _ab(_k(_obs("w1", 0, "~"), _claim("cl", "slip"), infer=inf)) == pytest.approx((0, 0))
    assert _ab(_k(_obs("w1", 0, "~"), _claim("cl", "learned"), infer=inf)) == pytest.approx(before)


@pytest.mark.parametrize("events, why", [
    ((_obs("w1", 0, "+"), _claim("cl", "slip")), "not a wrong test answer"),
    ((_obs("w1", 0, "-", kind="self"), _claim("cl", "slip")), "not a wrong test answer"),
    ((_obs("w1", 0, "~"), _claim("cl", "slip", source="nope")), "not a wrong test answer"),
    ((_obs("w1", 0, "~"), _claim("cl", "slip"), _claim("cl2", "learned")), "already claimed"),
    ((_obs("w1", 0, "~"), _claim("cl", "slip"), _resolve("r", "cl", "confirmed"),
      _resolve("r2", "cl", "refuted")), "no open claim"),
])
def test_claims_fail_closed(events, why):
    with pytest.raises(AspectRejected, match=why):
        _k(*events)


def test_copy_and_fingerprint_carry_open_claims():
    k = _k(_obs("w1", 0, "~"), _claim("cl", "slip"))
    c = k.copy()
    assert c.to_json() == k.to_json() and c.open_claims(CELL) == ("cl",)
    assert '"claimed"' in k.to_json() and '"claimed"' not in _k(_obs("w1", 0, "~")).to_json()


def test_a_confirmation_question_is_predicted_under_its_claim_kind():
    events = [DefineConcept(event_id="dc", ts=T0, concept_id="c", aspects=tuple(Aspect))]
    for i in range(8):                                   # 8 slips, each "confirmed" by a new miss
        events += [_obs(f"w{i}", 3 * i, "~"), _claim(f"cl{i}", "slip", f"w{i}", 3 * i + 1),
                   _obs(f"c{i}", 3 * i + 2, "~"), _resolve(f"r{i}", f"cl{i}", "refuted", 3 * i + 2)]
    out = predict_outcomes(events, ASPECT_CONFIG_V1, InferParams(), {"c": "d1"})
    kinds = {o.event_id: o.prior_kind for o in out}
    assert all(kinds[f"c{i}"] == "slip_claim" for i in range(8))
    assert all(kinds[f"w{i}"] != "slip_claim" for i in range(8))
