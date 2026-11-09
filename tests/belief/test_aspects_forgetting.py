import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from evowiki.belief.aspects import AspectConfig, AspectK, Declare, DefineConcept, Observe
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.contracts import Aspect, Band, BandParams, Observation

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
ALL = tuple(Aspect)
# FSRS-6 (belief/memory.py): a first correct answer (Good) -> S = 2.3065 days;
# R = (1 + F t/S)^-0.1542 < 0.6  <=>  t > 62.25 days.


def k_with(*concepts, bands=None):
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default(), bands=bands))
    for i, (c, prereqs) in enumerate(concepts):
        k.apply(DefineConcept(event_id=f"d{i}", ts=T0, concept_id=c, aspects=ALL, prereqs=prereqs))
    return k


def obs(cell, pol, strength=1.0, kind="recall", quote=None):
    return Observation(cell=cell, polarity=pol, strength=strength, kind=kind,
                       evidence_ref="ev1", quote=quote)


def see(k, n, o, ts=T0):
    k.apply(Observe(event_id=f"o{n}", ts=ts, observation=o))


def test_recall_min_default_and_bounds():
    assert BandParams().recall_min == 0.6
    with pytest.raises(ValidationError):
        BandParams(recall_min=0.2)
    with pytest.raises(ValidationError):
        BandParams(recall_min=0.95)


def test_tested_cell_drops_to_shaky_when_recall_falls_below_threshold():
    k = k_with(("z", ()))
    see(k, 1, obs("z.procedure", "+"))
    assert k.belief("z.procedure", T0 + timedelta(days=62)).band is Band.LIKELY
    b = k.belief("z.procedure", T0 + timedelta(days=63))
    assert b.recall < 0.6 and b.band is Band.SHAKY
    assert k.belief("z.procedure", T0 + timedelta(days=63), decay=False).band is Band.LIKELY


def test_known_also_drops_and_numbers_do_not_change():
    k = k_with(("z", ()))
    for n in range(4):
        see(k, n, obs("z.term", "+"))
    fresh = k.belief("z.term", T0)
    assert fresh.band is Band.KNOWN
    late = k.belief("z.term", T0 + timedelta(days=3650))
    assert late.band is Band.SHAKY
    assert (late.m, late.u, late.direct) == (fresh.m, fresh.u, fresh.direct)


def test_recall_resets_after_review_and_band_returns():
    k = k_with(("z", ()))
    see(k, 1, obs("z.procedure", "+"))
    t = T0 + timedelta(days=70)
    assert k.belief("z.procedure", t).band is Band.SHAKY
    see(k, 2, obs("z.procedure", "+", kind="recall"), ts=t)
    assert k.belief("z.procedure", t).band in (Band.LIKELY, Band.KNOWN)


def test_gap_is_not_changed_by_forgetting():
    k = k_with(("z", ()))
    see(k, 1, obs("z.procedure", "-"))
    assert k.belief("z.procedure", T0 + timedelta(days=200)).band is Band.GAP


def test_declaration_never_decays():
    k = k_with(("z", ()))
    see(k, 1, obs("z.procedure", "+"))
    k.apply(Declare(event_id="dec", ts=T0, cell="z.procedure", value="know"))
    assert k.belief("z.procedure", T0 + timedelta(days=400)).band is Band.KNOWN


def test_self_report_only_never_decays():
    k = k_with(("z", ()))
    see(k, 1, obs("z.term", "+", kind="self", quote="我会这个"))
    assert k.memory("z.term").last_tested is None
    fresh, late = k.belief("z.term", T0), k.belief("z.term", T0 + timedelta(days=400))
    assert late.recall == 1.0 and late == fresh


def test_threshold_is_a_band_param():
    k = k_with(("z", ()), bands=BandParams(recall_min=0.3))
    see(k, 1, obs("z.procedure", "+"))
    assert k.belief("z.procedure", T0 + timedelta(days=70)).band is Band.LIKELY


# a first review starts from FSRS's initial stability of the grade: Good / Hard / Again
@pytest.mark.parametrize(("pol", "stability"), [("+", 2.3065), ("~", 1.2931), ("-", 0.212)])
def test_recall_three_grades_update_stability_like_quiz(pol, stability):
    k = k_with(("z", ()))
    t = T0 + timedelta(days=2)
    see(k, 1, obs("z.term", pol, kind="recall"), ts=t)
    mem = k.memory("z.term")
    assert mem.stability == pytest.approx(stability) and mem.last_tested == t


def test_recall_matches_quiz_numbers():
    a, b = k_with(("z", ())), k_with(("z", ()))
    see(a, 1, obs("z.notation", "~", kind="recall"))
    see(b, 1, obs("z.notation", "~", kind="quiz"))
    later = T0 + timedelta(days=3)
    assert a.belief("z.notation", later) == b.belief("z.notation", later)


def test_memory_unknown_cell_raises():
    with pytest.raises(KeyError):
        k_with(("z", ())).memory("nope.term")


# ---- fix wave 1 (user ruling a): relearning cap; weak tests don't count as a test ----

def forget_then_miss(kind="recall", pol="-", n_plus=4, days=3650):
    k = k_with(("z", ()))
    for n in range(n_plus):
        see(k, n, obs("z.term", "+"))
    t = T0 + timedelta(days=days)
    assert k.belief("z.term", t).recall < 0.6          # forgotten before the miss
    s_before = k.memory("z.term").stability
    see(k, 99, obs("z.term", pol, kind=kind), ts=t)
    return k, t, s_before


def test_forgotten_then_missed_stays_shaky_until_recalled():
    k, t, s_before = forget_then_miss()
    assert k.belief("z.term", t).band is Band.SHAKY
    mem = k.memory("z.term")
    assert mem.relearning is True and mem.last_tested == t
    assert mem.stability < s_before                      # Again lowers FSRS stability
    assert k.belief("z.term", t + timedelta(days=5)).band is Band.SHAKY
    see(k, 100, obs("z.term", "+", kind="recall"), ts=t + timedelta(days=5))
    assert k.belief("z.term", t + timedelta(days=5)).band in (Band.LIKELY, Band.KNOWN)
    assert k.memory("z.term").relearning is False


def test_forgotten_then_fuzzy_stays_shaky():
    k, t, _ = forget_then_miss(pol="~", n_plus=2, days=90)
    assert k.belief("z.term", t, decay=False).band is Band.LIKELY   # evidence alone says likely
    assert k.belief("z.term", t).band is Band.SHAKY
    assert k.memory("z.term").relearning is True


@pytest.mark.parametrize("kind", ["quiz", "check", "recall"])
def test_every_recall_kind_sets_relearning(kind):
    k, t, _ = forget_then_miss(kind=kind)
    assert k.memory("z.term").relearning is True
    assert k.belief("z.term", t).band is Band.SHAKY


def test_non_forgotten_miss_does_not_set_relearning():
    k = k_with(("z", ()))
    see(k, 1, obs("z.term", "+"))
    see(k, 2, obs("z.term", "-"), ts=T0 + timedelta(days=1))
    assert k.memory("z.term").relearning is False


def test_self_plus_does_not_clear_relearning():
    k, t, _ = forget_then_miss()
    see(k, 100, obs("z.term", "+", kind="self", quote="我会"), ts=t)
    assert k.memory("z.term").relearning is True
    assert k.belief("z.term", t).band is Band.SHAKY


def test_declaration_wins_over_relearning():
    k, t, _ = forget_then_miss()
    k.apply(Declare(event_id="dec", ts=t, cell="z.term", value="know"))
    assert k.belief("z.term", t).band is Band.KNOWN


def test_decay_false_ignores_relearning_cap():
    k, t, _ = forget_then_miss()
    assert k.belief("z.term", t, decay=False).band in (Band.LIKELY, Band.KNOWN)


@pytest.mark.parametrize("strength", [0.0, 0.2])
def test_weak_test_does_not_count_as_a_test(strength):
    k = k_with(("z", ()))
    see(k, 1, obs("z.term", "+", strength=strength))
    mem = k.memory("z.term")
    assert mem.last_tested is None and mem.stability is None


def test_copy_and_json_carry_relearning():
    k, _, _ = forget_then_miss()
    assert k.copy().memory("z.term").relearning is True
    assert k.copy().to_json() == k.to_json()
    cells = json.loads(k.to_json())["cells"]
    assert cells["z.term"]["relearning"] is True
    assert cells["z.procedure"]["relearning"] is False


# ---- FSRS memory (user ruling 2026-10-24, belief/memory.py) ----

def _after(*answers):
    """Stability of z.term after (polarity, ts) answers, each a full-weight quiz."""
    k = k_with(("z", ()))
    for n, (pol, ts) in enumerate(answers):
        see(k, n, obs("z.term", pol), ts=ts)
    return k.memory("z.term").stability


def test_massed_answers_do_not_inflate_stability():
    one = _after(("+", T0))
    massed = _after(*[("+", T0 + timedelta(minutes=i)) for i in range(4)])
    assert massed == pytest.approx(one)          # the old model: ×2 per answer, 16× here


def test_spaced_success_grows_more_than_massed_success():
    massed = _after(("+", T0), ("+", T0 + timedelta(minutes=1)))
    spaced = _after(("+", T0), ("+", T0 + timedelta(days=5)))
    assert spaced > 3 * massed


@pytest.mark.parametrize("gap", [timedelta(minutes=1), timedelta(days=5)])
def test_hard_never_lowers_stability_a_miss_does(gap):
    s0 = _after(("+", T0))
    hard = _after(("+", T0), ("~", T0 + gap))
    miss = _after(("+", T0), ("-", T0 + gap))
    assert hard >= s0 > miss
    good = _after(("+", T0), ("+", T0 + gap))
    assert good >= hard


def test_a_weak_test_is_no_review_and_a_counted_one_is_a_full_review():
    full = _after(("+", T0))
    k = k_with(("z", ()))
    see(k, 1, obs("z.term", "+", strength=0.25))          # = MIN_TEST_WEIGHT
    assert k.memory("z.term").stability == pytest.approx(full)
    k = k_with(("z", ()))
    see(k, 1, obs("z.term", "+", strength=0.2))
    assert k.memory("z.term").stability is None


def test_forgetting_scale_stretches_the_curve():
    from evowiki.teach.contracts import InferParams
    base = k_with(("z", ()))
    slow = AspectK(AspectConfig.from_theta(ThetaInfer.default(),
                                           infer=InferParams(forgetting_scale=2.0)))
    slow.apply(DefineConcept(event_id="d0", ts=T0, concept_id="z", aspects=ALL))
    for k in (base, slow):
        see(k, 1, obs("z.term", "+"))
    t = timedelta(days=20)
    assert slow.belief("z.term", T0 + 2 * t).recall == pytest.approx(
        base.belief("z.term", T0 + t).recall)
    assert slow.memory("z.term").stability == pytest.approx(2 * base.memory("z.term").stability)



# ---- what moves the schedule: review cards and page questions (user rulings 2026-11-04) ----

PAGE_ANSWERS = ["check:p:s1", "block:p:s1:a:0.0:r"]
MASTERY_ONLY = ["probe:d:0:0", "known:a1", "quiz:asst:s~m~c:0"]


def _cell(ref, kind, polarity, days=30):
    """A cell with a real memory, then one answer `days` after its last formal review."""
    k = AspectK(AspectConfig(prior_a=1.0, prior_b=1.0))
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    k.apply(DefineConcept(event_id="d", ts=t0, concept_id="c", aspects=("term",)))
    ob = lambda e, p, r, kd, ts: k.apply(Observe(event_id=e, ts=ts, observation=Observation(
        cell="c.term", polarity=p, strength=1.0, kind=kd, evidence_ref=r)))
    ob("a", "+", "review:p:s:1", "recall", t0)
    ob("b", "+", "review:p:s:2", "recall", t0 + timedelta(days=5))
    before = k.memory("c.term")
    m_before = k.belief("c.term", t0 + timedelta(days=5)).m
    now = t0 + timedelta(days=5 + days)
    ob("c", polarity, ref, kind, now)
    return before, m_before, k.memory("c.term"), k.belief("c.term", now).m


@pytest.mark.parametrize("ref", MASTERY_ONLY)
@pytest.mark.parametrize("polarity", ["+", "-"])
def test_a_diagnosis_or_claim_answer_moves_mastery_not_the_schedule(ref, polarity):
    before, m0, after, m1 = _cell(ref, "check", polarity)
    assert (after.stability, after.last_tested, after.relearning) == (
        before.stability, before.last_tested, before.relearning)
    assert (m1 > m0) if polarity == "+" else (m1 < m0)


@pytest.mark.parametrize("ref", PAGE_ANSWERS)
@pytest.mark.parametrize("polarity", ["+", "-"])
def test_an_answer_on_a_page_is_a_review_exactly_as_on_a_card(ref, polarity):
    _, _, after, m1 = _cell(ref, "check", polarity)
    _, _, card, m_card = _cell("review:p:s:3", "check", polarity)
    assert (after.stability, after.last_tested, after.relearning, m1) == (
        card.stability, card.last_tested, card.relearning, m_card)


@pytest.mark.parametrize("kind", ["recall", "check"])
def test_a_formal_review_card_moves_the_schedule(kind):
    before, _, after, _ = _cell("review:p:s:3", kind, "+")
    assert after.stability > before.stability and after.last_tested > before.last_tested


def test_a_cells_first_answer_of_any_origin_starts_its_memory_or_it_could_never_come_due():
    k = AspectK(AspectConfig(prior_a=1.0, prior_b=1.0))
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    k.apply(DefineConcept(event_id="d", ts=t0, concept_id="c", aspects=("term",)))
    k.apply(Observe(event_id="a", ts=t0, observation=Observation(
        cell="c.term", polarity="+", strength=1.0, kind="check", evidence_ref="check:p:s")))
    assert k.memory("c.term").stability == pytest.approx(1.38, abs=0.01)    # a choice right: Good × 0.6
