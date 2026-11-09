import json
import math
from datetime import timedelta

import pytest
from pydantic import ValidationError

from evowiki.belief.theta import ThetaInfer
from evowiki.belief.tracer import BeliefState, Tracer, replay, replay_in_order
from evowiki.evidence.models import Evidence, EvidenceType, Polarity


def _theta_at_upper_bounds() -> ThetaInfer:
    d = ThetaInfer.default().model_dump()
    d["evidence_weights"]["quiz_correct"] = 3.0
    d["forgetting"]["growth"] = 5.0
    return ThetaInfer.model_validate(d)


def _correct(i: int, t0, cid="vector") -> Evidence:
    return Evidence(id=f"e{i:05d}", source_event_id=f"ev{i}", extractor_version="rule-v1",
                    concept_id=cid, evidence_type=EvidenceType.QUIZ_CORRECT,
                    polarity=Polarity.POSITIVE, extraction_confidence=1.0,
                    ts=t0 + timedelta(minutes=i))


def test_stability_is_capped_and_json_safe(graph, t0):
    theta = _theta_at_upper_bounds()
    tr = Tracer.from_prior(graph, theta, "none")
    for i in range(400):
        tr.apply(_correct(i, t0))
    s = tr.state.beliefs["vector"].stability
    assert s == theta.forgetting.S_max
    assert math.isfinite(s)
    json.loads(tr.state.to_json())  # strict JSON: no Infinity


def test_smax_bounds_and_consistency():
    d = ThetaInfer.default().model_dump()
    d["forgetting"]["S_max"] = 10.0
    with pytest.raises(ValidationError):
        ThetaInfer.model_validate(d)  # below 30
    d["forgetting"]["S_max"] = 40.0
    d["forgetting"]["S0_by_difficulty"][1] = 60.0
    with pytest.raises(ValidationError, match="S_max"):
        ThetaInfer.model_validate(d)  # below an S0


def test_replay_in_order_keeps_given_order(graph, t0):
    theta = ThetaInfer.default()
    init = Tracer.from_prior(graph, theta, "none").state
    # A correct/wrong pair at equal ts commutes (x growth, x shrink) unless a clamp bites;
    # starting at S_max makes order observable: correct->wrong = S_max/2, wrong->correct = S_max.
    init.beliefs["vector"] = init.beliefs["vector"].model_copy(
        update={"stability": theta.forgetting.S_max})
    # Equal ts: replay() sorts by id string ("e10" < "e9"); replay_in_order keeps list order.
    same_ts = [_correct(9, t0), _correct(10, t0)]
    same_ts = [e.model_copy(update={"ts": t0, "id": i})
               for e, i in zip(same_ts, ["e9", "e10"], strict=True)]
    wrong = same_ts[1].model_copy(update={"evidence_type": EvidenceType.QUIZ_WRONG,
                                          "polarity": Polarity.NEGATIVE})
    seq = [same_ts[0], wrong]
    a = replay_in_order(graph, theta, init, seq)
    b = replay(graph, theta, init, seq)
    assert a.to_json() != b.to_json()
    manual = Tracer(graph, theta, init)
    for e in seq:
        manual.apply(e)
    assert a.to_json() == manual.state.to_json()


def test_replay_in_order_does_not_mutate_initial(graph, t0):
    theta = ThetaInfer.default()
    init = BeliefState(dict(Tracer.from_prior(graph, theta, "none").state.beliefs))
    before = init.to_json()
    replay_in_order(graph, theta, init, [_correct(0, t0)])
    assert init.to_json() == before
