"""Channel A gate on the deterministic scenarios and small crafted streams."""

from datetime import timedelta

import pytest

from evowiki.belief.aspects import DefineConcept
from evowiki.goals.contracts import Aspect, cell_id
from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import InferParams
from evowiki.teach.gate_a import MIN_OUTCOMES, gate_a
from evowiki.teach.predict import predict_outcomes
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach import scenarios
from tests.teach.scenarios import T0, _obs

D = InferParams()
SELF = "evidence_weight.self"


def _mid_cutoff(events, domain_of):
    """The ts of the median test outcome under the default θ: about half the outcomes are test."""
    outs = predict_outcomes(events, ASPECT_CONFIG_V1, D, domain_of)
    return outs[len(outs) // 2].ts


def _run(events, domain_of, new, *, cutoff=None, pid="p1"):
    cutoff = cutoff or _mid_cutoff(events, domain_of)
    return gate_a(pid, events, ASPECT_CONFIG_V1, D, new, cutoff=cutoff, domain_of=domain_of)


def test_overconfident_self_down_passes_and_up_fails():
    events, dom = scenarios.overconfident_self()
    good = _run(events, dom, ip.set_(D, SELF, 0.3))
    assert good.passed, good
    assert good.vetoes == ()
    assert good.channel == "A" and good.cost_usd == 0.0
    assert good.n_outcomes >= MIN_OUTCOMES
    assert good.ci_low > 0 and good.ci_high >= good.ci_low
    assert good.utility_after > good.utility_before
    assert good.complexity_before == 0 and good.complexity_after == 1

    bad = _run(events, dom, ip.set_(D, SELF, 1.2))
    assert not bad.passed
    assert "no_ci_improvement" in bad.vetoes
    assert bad.utility_after < bad.utility_before


def test_forgetting_fast_scale_down_passes_and_up_fails():
    events, dom = scenarios.forgetting_fast()
    good = _run(events, dom, ip.set_(D, "forgetting_scale", 0.6))
    assert good.passed, good
    bad = _run(events, dom, ip.set_(D, "forgetting_scale", 1.8))
    assert not bad.passed
    assert "no_ci_improvement" in bad.vetoes


def _small_stream(n_tests: int):
    """One concept per test: a self-report `+`, then (a day later) one check `~`."""
    events, dom, tests = [], {}, []
    ts = T0
    for i in range(n_tests):
        c = f"s{i}"
        dom[c] = "d1"
        events.append(DefineConcept(event_id=f"s-def{i}", ts=T0, concept_id=c,
                                    aspects=(Aspect.TERM,), difficulty=3))
    for i in range(n_tests):
        ts += timedelta(hours=1)
        events.append(_obs(f"s-self{i}", ts, cell_id(f"s{i}", Aspect.TERM), "self", "+"))
    ts += timedelta(days=1)
    cutoff = ts
    for i in range(n_tests):
        ts += timedelta(hours=1)
        tests.append(_obs(f"s-chk{i}", ts, cell_id(f"s{i}", Aspect.TERM), "check", "~"))
    return events + tests, dom, cutoff


def test_twenty_outcomes_is_insufficient():
    events, dom, cutoff = _small_stream(20)
    r = _run(events, dom, ip.set_(D, SELF, 0.0), cutoff=cutoff)
    assert not r.passed
    assert r.vetoes == ("insufficient_outcomes",)
    assert r.n_outcomes == 20
    assert r.ci_low is None and r.utility_before is None


def test_domain_regression_vetoes():
    """d1: self-reports overstate (checks `-`); d2: self-reports are right (checks `+`). Dropping
    the self weight helps d1 a lot and hurts d2."""
    events, dom = [], {}
    n = 30
    for i in range(n):
        c = f"x{i}"
        dom[c] = "d1" if i < 20 else "d2"
        events.append(DefineConcept(event_id=f"x-def{i}", ts=T0, concept_id=c,
                                    aspects=(Aspect.TERM,), difficulty=3))
    ts = T0
    for i in range(n):
        for j in range(3):
            ts += timedelta(hours=1)
            events.append(_obs(f"x-self{i}-{j}", ts, cell_id(f"x{i}", Aspect.TERM), "self", "+"))
    ts += timedelta(days=1)
    cutoff = ts
    for i in range(n):
        ts += timedelta(hours=1)
        pol = "-" if i < 20 else "+"
        events.append(_obs(f"x-chk{i}", ts, cell_id(f"x{i}", Aspect.TERM), "check", pol))
    r = _run(events, dom, ip.set_(D, SELF, 0.0), cutoff=cutoff)
    assert r.n_outcomes == n
    assert "domain_regressed:d2" in r.vetoes
    assert "domain_regressed:d1" not in r.vetoes
    assert not r.passed


def test_deterministic():
    events, dom = scenarios.overconfident_self(n_concepts=20, seed=1)
    new = ip.set_(D, SELF, 0.3)
    assert _run(events, dom, new) == _run(events, dom, new)


def test_mismatched_outcome_sets_raise(monkeypatch):
    events, dom = scenarios.overconfident_self(n_concepts=10)
    from evowiki.teach import gate_a as mod

    real = mod.predict_outcomes
    calls = []

    def fake(evs, base, infer, domain_of):
        out = real(evs, base, infer, domain_of)
        calls.append(1)
        return out[:-1] if len(calls) == 2 else out

    monkeypatch.setattr(mod, "predict_outcomes", fake)
    with pytest.raises(ValueError, match="outcome sets differ"):
        _run(events, dom, ip.set_(D, SELF, 0.3))
