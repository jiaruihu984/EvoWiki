"""The bands K gives on the paths the user ruled on (2026-11-04), and reading a page starting its memory
without touching K. Default config: what the learner reads is a function of the band (BAND_WORD)."""

from datetime import UTC, datetime, timedelta
from math import ceil

import pytest

from evowiki.belief.aspects import AspectK, Declare, DefineConcept, Observe, PageRead
from evowiki.goals.contracts import Band, Observation
from evowiki.goals.guardrails import BAND_WORD
from evowiki.goals.review import fading
from evowiki.goals.selfreport import LEVEL_CLAIM, MEANS_CLAIM, SELF_CLAIM, self_observations
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

T = datetime(2026, 1, 1, tzinfo=UTC)
C = "c.term"


@pytest.fixture
def k():
    out = AspectK(ASPECT_CONFIG_V1)
    out.apply(DefineConcept(event_id="def", ts=T, concept_id="c", aspects=("term",), prereqs=(), difficulty=3))
    return out


def rough(k, mark):
    for o in self_observations(MEANS_CLAIM[mark], [C], quote="摸底", evidence_ref="rough:d:x:b"):
        k.apply(Observe(event_id=f"rough-{mark}", ts=T, observation=o))


def answer(k, polarity, days=0.0, name="a"):
    k.apply(Observe(event_id=name, ts=T + timedelta(days=days), observation=Observation(
        cell=C, polarity=polarity, strength=1.0, kind="check", evidence_ref="check:p:s")))


def band(k, days=0.0):
    return k.belief(C, T + timedelta(days=days)).band


@pytest.mark.parametrize("mark", ["know", "partly"])
def test_said_he_knows_it_then_right_is_mastered(k, mark):
    rough(k, mark)
    assert BAND_WORD[band(k)] == "熟悉"
    answer(k, "+")
    assert band(k) is Band.KNOWN and BAND_WORD[band(k)] == "掌握"


def test_said_familiar_is_weaker_than_said_mastered():
    a, b = AspectK(ASPECT_CONFIG_V1), AspectK(ASPECT_CONFIG_V1)
    for x, mark in ((a, "partly"), (b, "know")):
        x.apply(DefineConcept(event_id="def", ts=T, concept_id="c", aspects=("term",), prereqs=(), difficulty=3))
        rough(x, mark)
    assert a.belief(C, T).m < b.belief(C, T).m


def test_one_right_answer_from_nothing_is_familiar_and_the_second_mastered(k):
    assert BAND_WORD[band(k)] == "还没学"
    answer(k, "+", name="a1")
    assert band(k) is Band.LIKELY and BAND_WORD[band(k)] == "熟悉"
    answer(k, "+", days=3, name="a2")
    assert band(k, 3) is Band.KNOWN


def test_said_he_does_not_know_then_right_is_familiar(k):
    rough(k, "dont")
    assert BAND_WORD[band(k)] == "不会"
    answer(k, "+")
    assert band(k) is Band.SHAKY and BAND_WORD[band(k)] == "熟悉"


def test_marked_dont_then_right_is_familiar(k):
    k.apply(Declare(event_id="d", ts=T, cell=C, value="dont"))
    assert BAND_WORD[band(k)] == "不会"
    answer(k, "+")
    assert band(k) is Band.SHAKY


@pytest.mark.parametrize("mark", ["know", "partly"])
def test_a_wrong_answer_does_not_keep_known(k, mark):
    rough(k, mark)
    answer(k, "+", name="a1")
    answer(k, "-", days=1, name="a2")
    assert band(k, 1) is Band.SHAKY


def test_after_many_right_answers_a_wrong_one_still_does_not_keep_known(k):
    for i in range(5):
        answer(k, "+", days=i * 3, name=f"r{i}")
    assert band(k, 12) is Band.KNOWN
    answer(k, "-", days=15, name="w")
    assert band(k, 15) is Band.LIKELY and BAND_WORD[band(k, 15)] == "熟悉"
    answer(k, "+", days=18, name="r5")
    assert band(k, 18) is Band.KNOWN


def test_forgetting_takes_known_down_and_a_right_answer_brings_it_back(k):
    rough(k, "partly")
    answer(k, "+", name="a1")
    assert band(k, 500) is Band.SHAKY         # recall below recall_min
    answer(k, "+", days=500, name="a2")
    assert band(k, 500) is Band.KNOWN


def test_reading_a_page_starts_the_memory_and_leaves_k_alone(k):
    before = k.belief(C, T)
    k.apply(PageRead(event_id="r", ts=T, cell=C))
    after = k.belief(C, T)
    assert (after.band, after.m, after.u, after.direct) == (before.band, before.m, before.u, before.direct)
    assert round(k.memory(C).stability, 2) == 2.31
    assert fading(k, T + timedelta(days=2)) == ()
    assert [f.cell for f in fading(k, T + timedelta(days=3))] == [C]


def test_reading_again_or_after_an_answer_changes_nothing(k):
    answer(k, "-")
    s = k.memory(C).stability
    k.apply(PageRead(event_id="r", ts=T + timedelta(days=1), cell=C))
    assert k.memory(C).stability == s and k.memory(C).last_tested == T


def _right(k, kind, ref, name, days):
    k.apply(Observe(event_id=name, ts=T + timedelta(days=days), observation=Observation(
        cell=C, polarity="+", strength=1.0, kind=kind, evidence_ref=ref)))


CHOICES = [("check", "check:p:s"), ("check", "review:p:s:rv_1"), ("quiz", "block:p:s:a:0.0:b")]


def _intervals(kind, ref, path):
    """The stored stability after each answer of `path`, each given on the first whole day at or after due."""
    x = AspectK(ASPECT_CONFIG_V1)
    x.apply(DefineConcept(event_id="def", ts=T, concept_id="c", aspects=("term",), prereqs=(), difficulty=3))
    at, out = T, []
    for i, pol in enumerate(path):
        x.apply(Observe(event_id=f"e{i}", ts=at, observation=Observation(
            cell=C, polarity=pol, strength=1.0, kind=kind, evidence_ref=ref)))
        s = x.memory(C).stability
        out.append(round(s, 1))
        at += timedelta(days=max(1, ceil(s)))
    return out, x.memory(C).difficulty


@pytest.mark.parametrize(("kind", "ref"), CHOICES)
@pytest.mark.parametrize(("path", "want", "difficulty"), [
    ("+++++++", [1.4, 5.7, 16.1, 40.0, 86.2, 170.7, 311.6], 2.08),
    ("+++-+++", [1.4, 5.7, 16.1, 1.9, 3.2, 5.5, 8.5], 7.35),
    ("+-+++++", [1.4, 0.4, 1.3, 2.7, 4.5, 7.2, 10.9], 7.33),
])
def test_a_choice_answered_right_keeps_goods_stability_discounted(kind, ref, path, want, difficulty):
    got, d = _intervals(kind, ref, path)
    assert got == want and round(d, 2) == difficulty


def test_a_recalled_card_is_goods_stability_undiscounted():
    assert _intervals("recall", "review:p:s:rv_1", "+++")[0] == [2.3, 13.8, 57.0]


def test_the_discount_is_the_schedule_only_the_evidence_reads_right():
    a, _ = _intervals("check", "check:p:s", "+")
    x, y = AspectK(ASPECT_CONFIG_V1), AspectK(ASPECT_CONFIG_V1)
    for k, kind, ref in ((x, "check", "check:p:s"), (y, "recall", "review:p:s:rv_1")):
        k.apply(DefineConcept(event_id="def", ts=T, concept_id="c", aspects=("term",), prereqs=(), difficulty=3))
        k.apply(Observe(event_id="a", ts=T, observation=Observation(
            cell=C, polarity="+", strength=1.0, kind=kind, evidence_ref=ref)))
    assert a == [1.4] and x.belief(C, T).m == y.belief(C, T).m and x.belief(C, T).band is Band.LIKELY


def said(k, claim):
    for o in self_observations(claim, [C], quote="都忘得差不多了", evidence_ref="goal:m:x"):
        k.apply(Observe(event_id="said", ts=T, observation=o))


@pytest.mark.parametrize("claim", [LEVEL_CLAIM["beginner"], MEANS_CLAIM["forgot"]],
                         ids=["from_his_words", "probe_option"])
def test_said_he_forgot_then_one_right_answer_is_not_known(k, claim):
    said(k, claim)
    answer(k, "+")
    assert band(k) is Band.LIKELY


def test_the_familiar_mark_stays_what_it_was():
    assert SELF_CLAIM[MEANS_CLAIM["partly"]] == ("+", 0.75)
    assert SELF_CLAIM[MEANS_CLAIM["forgot"]] == SELF_CLAIM[LEVEL_CLAIM["beginner"]] == ("+", 0.2)
