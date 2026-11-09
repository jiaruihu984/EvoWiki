from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from evowiki.belief.aspects import (
    AddNote,
    AspectConfig,
    AspectEventAdapter,
    AspectK,
    AspectRejected,
    DefineConcept,
    Observe,
    effective_weight,
)
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.contracts import Aspect, Band, Observation

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
ALL = tuple(Aspect)


def k_with(*concepts: tuple[str, tuple[str, ...]]) -> AspectK:
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default()))
    for i, (c, prereqs) in enumerate(concepts):
        k.apply(DefineConcept(event_id=f"d{i}", ts=T0, concept_id=c, aspects=ALL, prereqs=prereqs))
    return k


def obs(cell, pol, strength=1.0, kind="quiz", quote=None):
    return Observation(cell=cell, polarity=pol, strength=strength, kind=kind,
                       evidence_ref="ev1", quote=quote)


def see(k, n, o, ts=T0):
    k.apply(Observe(event_id=f"o{n}", ts=ts, observation=o))


def test_prior_is_tracer_default_beta_06_14():
    k = k_with(("z", ()))
    b = k.belief("z.procedure", T0)
    assert b.m == pytest.approx(0.3) and b.direct == 0 and b.band is Band.NONE


def test_one_correct_answer_is_likely():
    k = k_with(("z", ()))
    see(k, 1, obs("z.procedure", "+"))
    b = k.belief("z.procedure", T0)
    assert b.m == pytest.approx(1.6 / 3) and b.u == pytest.approx(0.24944, abs=1e-4)
    assert b.band is Band.LIKELY


def test_wrong_answer_is_shaky():
    k = k_with(("z", ()))
    see(k, 1, obs("z.procedure", "~"))
    b = k.belief("z.procedure", T0)
    assert b.m == pytest.approx(1.0 / 3.2) and b.direct == pytest.approx(1.2)
    assert b.band is Band.SHAKY


def test_dont_know_is_gap():
    k = k_with(("z", ()))
    see(k, 1, obs("z.procedure", "-", 0.8))
    b = k.belief("z.procedure", T0)
    assert b.m == pytest.approx(0.6 / 2.8) and b.band is Band.GAP


def test_three_correct_is_known():
    k = k_with(("z", ()))
    for n in range(3):
        see(k, n, obs("z.procedure", "+"))
    b = k.belief("z.procedure", T0)
    assert b.m == pytest.approx(0.72) and b.u == pytest.approx(0.18330, abs=1e-4)
    assert b.band is Band.KNOWN


def test_self_report_discounts_and_mechanism_discounts_more():
    assert effective_weight(obs("z.procedure", "+", 1.0, "self", "我会")) == pytest.approx(0.8)
    assert effective_weight(obs("z.mechanism", "+", 1.0, "self", "我会")) == pytest.approx(0.4)
    assert effective_weight(obs("z.procedure", "+", 1.0, "ask")) == pytest.approx(0.6)
    assert effective_weight(obs("z.procedure", "+", 0.5, "quiz")) == pytest.approx(0.5)


def test_aspect_outside_concept_is_rejected_and_state_unchanged():
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default()))
    k.apply(DefineConcept(event_id="d", ts=T0, concept_id="z",
                          aspects=(Aspect.TERM, Aspect.NOTATION)))
    before = k.to_json()
    with pytest.raises(AspectRejected):
        see(k, 1, obs("z.mechanism", "+"))
    with pytest.raises(AspectRejected):
        see(k, 2, obs("nope.term", "+"))
    assert k.to_json() == before
    with pytest.raises(ValidationError):
        obs("z.bogus", "+")


def test_duplicate_event_id_is_rejected():
    k = k_with(("z", ()))
    see(k, 1, obs("z.term", "+"))
    with pytest.raises(AspectRejected):
        see(k, 1, obs("z.term", "+"))


def test_redefine_unions_aspects_and_prereqs():
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default()))
    k.apply(DefineConcept(event_id="a", ts=T0, concept_id="z", aspects=(Aspect.TERM,),
                          prereqs=("v",)))
    see(k, 1, obs("z.term", "+"))
    k.apply(DefineConcept(event_id="b", ts=T0, concept_id="z",
                          aspects=(Aspect.NOTATION, Aspect.TERM), prereqs=("m", "v")))
    assert k.aspects("z") == (Aspect.TERM, Aspect.NOTATION)
    assert k.belief("z.term", T0).direct == pytest.approx(1.0)


def test_forgetting_is_fsrs():
    k = k_with(("z", ()))
    see(k, 1, obs("z.procedure", "+", kind="recall"))        # FSRS Good: S = 2.3065 d, last = T0
    assert k.belief("z.procedure", T0 + timedelta(days=28)).recall == pytest.approx(0.67413, 1e-4)
    see(k, 2, obs("z.procedure", "-"), ts=T0 + timedelta(days=28))
    b = k.belief("z.procedure", T0 + timedelta(days=28))
    assert b.direct == pytest.approx(1.0 + 0.67413, 1e-4) and b.recall == pytest.approx(1.0)
    see(k, 3, obs("z.term", "+", kind="ask"))                # not a recall event
    assert k.belief("z.term", T0 + timedelta(days=100)).recall == 1.0


def test_copy_is_independent():
    k = k_with(("z", ()))
    c = k.copy()
    see(c, 1, obs("z.term", "+"))
    assert k.belief("z.term", T0).direct == 0


def test_note_label_is_optional_and_old_payloads_replay_with_none():
    k = k_with(("z", ()))
    old = {"type": "add_note", "event_id": "n1", "ts": T0.isoformat(), "cell": "z.term",
           "text": "低一个标准差", "evidence_ref": "quiz:d:0"}            # stored before labels
    k.apply(AspectEventAdapter.validate_python(old))
    k.apply(AddNote(event_id="n2", ts=T0, cell="z.term", text="a", evidence_ref="quiz:d:1",
                    label="题目：z？；选了「a」"))
    assert [n.label for n in k.notes("z")] == [None, "题目：z？；选了「a」"]
    with pytest.raises(ValidationError):
        AddNote(event_id="n3", ts=T0, cell="z.term", text="a", evidence_ref="e", label="x" * 601)
