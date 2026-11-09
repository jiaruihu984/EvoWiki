"""Aspect K reads θ_infer,u (`InferParams`); the default is bit-identical to the pre-wiring K."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from evowiki.belief.aspects import AspectConfig, AspectK, DefineConcept, Observe
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.contracts import Aspect, Observation
from evowiki.teach.contracts import InferParams
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.belief.aspect_streams import golden_streams, replay, snapshot

GOLDEN = Path(__file__).parent / "data" / "aspect_golden.json"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _infer(**kw) -> InferParams:
    return InferParams(**kw)


def _weights(**over: float) -> dict:
    w = dict(InferParams().evidence_weight)
    w.update(over)
    return w


def _k(infer: InferParams | None = None, prereqs=()) -> AspectK:
    cfg = ASPECT_CONFIG_V1 if infer is None else ASPECT_CONFIG_V1.with_infer(infer)
    k = AspectK(cfg)
    k.apply(DefineConcept(event_id="dp", ts=T0, concept_id="p", aspects=tuple(Aspect)))
    k.apply(DefineConcept(event_id="dz", ts=T0, concept_id="z", aspects=tuple(Aspect),
                          prereqs=prereqs))
    k.apply(DefineConcept(event_id="dd", ts=T0, concept_id="d", aspects=tuple(Aspect),
                          prereqs=("z",)))
    return k


def _see(k: AspectK, cell: str, pol: str, kind: str = "quiz", ts=T0, eid="o1") -> None:
    k.apply(Observe(event_id=eid, ts=ts, observation=Observation(
        cell=cell, polarity=pol, strength=1.0, kind=kind, evidence_ref="ev",
        quote="I get it" if kind == "self" else None)))


@pytest.mark.parametrize("with_default", [False, True])
def test_default_infer_matches_golden(with_default):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cfg = ASPECT_CONFIG_V1.with_infer(InferParams()) if with_default else ASPECT_CONFIG_V1
    streams = golden_streams()
    assert len(streams) == len(golden) == 40
    for events, want in zip(streams, golden, strict=True):
        k, rejected = replay(cfg, events)
        got = json.loads(json.dumps(snapshot(k, events, rejected), sort_keys=True))
        assert got == want   # exact: floats compared with ==


def test_self_weight_down_lowers_m_after_self_plus():
    base, low = _k(), _k(_infer(evidence_weight=_weights(self=0.3)))
    for k in (base, low):
        _see(k, "z.term", "+", kind="self")
    assert low.belief("z.term", T0).m < base.belief("z.term", T0).m


def test_forgetting_scale_up_raises_recall():
    base, slow = _k(), _k(_infer(forgetting_scale=2.0))
    for k in (base, slow):
        _see(k, "z.term", "+")
    later = T0 + timedelta(days=20)
    assert slow.belief("z.term", later).recall > base.belief("z.term", later).recall
    assert slow.memory("z.term").stability == 2.0 * base.memory("z.term").stability


def test_inference_strength_zero_adds_no_inferred():
    k = _k(_infer(inference_strength=0.0), prereqs=("p",))
    _see(k, "z.procedure", "+")
    for c in ("p", "z", "d"):
        for a in Aspect:
            cell = f"{c}.{a.value}"
            if cell != "z.procedure":
                assert k.belief(cell, T0).inferred_pos == 0
    base = _k(prereqs=("p",))
    _see(base, "z.procedure", "+")
    assert base.belief("z.notation", T0).inferred_pos > 0   # the default does infer


def test_guess_scale_up_weakens_check_plus():
    base, guessy = _k(), _k(_infer(guess_scale=1.5))
    for k in (base, guessy):
        _see(k, "z.term", "+", kind="check")
    assert guessy.belief("z.term", T0).direct < base.belief("z.term", T0).direct
    assert guessy.belief("z.term", T0).m < base.belief("z.term", T0).m


def test_slip_up_weakens_check_miss():
    base, slippy = _k(), _k(_infer(slip=0.3))
    for k in (base, slippy):
        _see(k, "z.term", "-", kind="check")
    assert slippy.belief("z.term", T0).direct < base.belief("z.term", T0).direct
    assert slippy.belief("z.term", T0).m > base.belief("z.term", T0).m


def test_config_with_infer_is_new_object():
    infer = _infer(slip=0.2)
    cfg = ASPECT_CONFIG_V1.with_infer(infer)
    assert cfg is not ASPECT_CONFIG_V1
    assert cfg.infer == infer
    assert ASPECT_CONFIG_V1.infer == InferParams()
    assert cfg.prior_a == ASPECT_CONFIG_V1.prior_a
    with pytest.raises(ValidationError):
        cfg.infer = InferParams()   # frozen


def test_partial_evidence_weight_fails_closed():
    partial = InferParams(evidence_weight={"quiz": 1.0, "check": 1.0})
    with pytest.raises(ValidationError, match="missing"):
        ASPECT_CONFIG_V1.with_infer(partial)
    with pytest.raises(ValidationError, match="missing"):
        AspectConfig(prior_a=1.0, prior_b=1.0, infer=partial)
    with pytest.raises(ValidationError, match="missing"):
        AspectConfig.from_theta(ThetaInfer.default(), infer=partial)


def test_start_known_sets_the_belief_before_evidence():
    """start_known scales the prior odds by its odds ratio to the default and keeps the strength:
    with the default config (prior mean 0.3 = the default start_known) a fresh cell's m is
    start_known itself; the default leaves the configured prior untouched."""
    cell = "p.term"
    d = _k().belief(cell, T0)
    assert d.m == ASPECT_CONFIG_V1.prior_a / (ASPECT_CONFIG_V1.prior_a + ASPECT_CONFIG_V1.prior_b)
    hi = _k(_infer(start_known=0.72)).belief(cell, T0)
    assert hi.m == pytest.approx(0.72) and hi.u < d.u and hi.band == d.band
    lo = _k(_infer(start_known=0.05)).belief(cell, T0)
    assert lo.m == pytest.approx(0.05)
    n = ASPECT_CONFIG_V1.prior_a + ASPECT_CONFIG_V1.prior_b    # strength kept: same evidence weight
    k = _k(_infer(start_known=0.72))
    _see(k, cell, "+")
    assert k.belief(cell, T0).m == pytest.approx((0.72 * n + 1.0) / (n + 1.0))


def test_start_known_is_a_ratio_on_another_configured_prior():
    cfg = AspectConfig(prior_a=1.0, prior_b=1.0)
    k = AspectK(cfg.with_infer(_infer(start_known=0.6)))
    k.apply(DefineConcept(event_id="d", ts=T0, concept_id="c", aspects=(Aspect.TERM,)))
    odds = (0.6 / 0.4) / (0.3 / 0.7)            # 1:1 prior odds × the ratio
    assert k.belief("c.term", T0).m == pytest.approx(odds / (1.0 + odds))
    with pytest.raises(ValidationError):
        InferParams(start_known=0.99)
