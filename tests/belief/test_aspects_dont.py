"""「不会」 is evidence in the one K calculation (user decision 2026-11-04): it reads gap at once, it comes
back in review, one right answer is not mastery, several are, and it can be taken back exactly."""

from datetime import UTC, datetime, timedelta

from evowiki.belief.aspects import (
    AspectK,
    Claim,
    Declare,
    DefineConcept,
    Observe,
    RevokeInference,
)
from evowiki.goals.contracts import Aspect, Band, Observation
from evowiki.goals.review import fading
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
CELL = "z.term"


class Run:
    def __init__(self):
        self.k = AspectK(ASPECT_CONFIG_V1)
        self.n = 0
        self.now = T0
        self.k.apply(DefineConcept(event_id="dz", ts=T0, concept_id="z", aspects=(Aspect.TERM,)))
        self.k.apply(DefineConcept(event_id="dd", ts=T0, concept_id="d", aspects=(Aspect.PURPOSE,),
                                   prereqs=("z",)))

    def _next(self, days: float) -> str:
        self.n += 1
        self.now += timedelta(days=days)
        return f"e{self.n}"

    def answer(self, polarity: str = "+", *, days: float = 3.0, kind: str = "check",
               ref: str = "check:p:s", cell: str = CELL) -> str:
        eid = self._next(days)
        self.k.apply(Observe(event_id=eid, ts=self.now, observation=Observation(
            cell=cell, polarity=polarity, strength=1.0, kind=kind, evidence_ref=ref)))
        return eid

    def declare(self, value, *, days: float = 1.0) -> None:
        self.k.apply(Declare(event_id=self._next(days), ts=self.now, cell=CELL, value=value))

    @property
    def band(self) -> Band:
        return self.k.belief(CELL, self.now).band

    @property
    def declared(self):
        return self.k.belief(CELL, self.now).declared

    def due(self) -> list[str]:
        return [f.cell for f in fading(self.k, self.now)]


def test_it_reads_gap_at_once_whatever_the_cell_had():
    r = Run()
    for _ in range(4):
        r.answer("+")
    assert r.band is Band.KNOWN
    r.declare("dont")
    assert r.band is Band.GAP and r.declared == "dont"


def test_it_comes_back_in_review():
    r = Run()
    r.declare("dont")
    assert r.due() == [CELL]


def test_one_right_answer_is_not_mastery_and_several_raise_it():
    r = Run()
    r.declare("dont")
    bands = []
    for _ in range(7):
        r.answer("+")
        bands.append(r.band)
    assert bands[:4] == [Band.SHAKY, Band.SHAKY, Band.LIKELY, Band.KNOWN] and bands[-1] is Band.KNOWN


def test_the_mark_stands_until_likely_then_it_is_history():
    r = Run()
    r.declare("dont")
    r.answer("+")
    r.answer("+")
    assert r.declared == "dont"
    r.answer("+")
    assert r.declared is None and r.band is Band.LIKELY
    r.declare(None)                     # nothing to take back any more
    assert r.band is Band.LIKELY


def test_a_wrong_answer_after_it_keeps_it_low():
    r = Run()
    r.declare("dont")
    r.answer("+")
    high = r.k.belief(CELL, r.now).m
    r.answer("-")
    assert r.k.belief(CELL, r.now).m < high and r.band is Band.SHAKY and r.declared == "dont"


def test_taking_it_back_restores_evidence_and_memory():
    r = Run()
    r.answer("+")
    r.answer("+")
    before = (r.k.belief(CELL, r.now).m, r.k.memory(CELL))
    r.declare("dont")
    r.declare(None, days=0.0)
    assert (r.k.belief(CELL, r.now).m, r.k.memory(CELL)) == before
    assert r.declared is None


def test_taking_it_back_after_a_test_keeps_the_test():
    r = Run()
    r.answer("+")
    r.declare("dont")
    r.answer("+")
    last = r.k.memory(CELL).last_tested
    r.declare(None)
    assert r.k.memory(CELL).last_tested == last
    assert r.k.belief(CELL, r.now).direct == 2.0


def test_know_replaces_it():
    r = Run()
    r.declare("dont")
    r.declare("know")
    assert r.band is Band.KNOWN and r.declared == "know" and r.k.belief(CELL, r.now).direct == 0.0


def test_a_second_mark_restarts_and_undo_returns_all_of_it():
    r = Run()
    r.answer("+")
    r.declare("dont")
    r.answer("+")
    r.declare("dont")
    assert r.band is Band.GAP and r.k.belief(CELL, r.now).direct == 1.5
    r.declare(None)
    assert r.k.belief(CELL, r.now).direct == 2.0


def test_inference_set_aside_can_still_be_revoked():
    r = Run()
    src = r.answer("+", cell="d.purpose")
    assert r.k.belief(CELL, r.now).inferred_pos > 0
    r.declare("dont")
    assert r.k.belief(CELL, r.now).inferred_pos == 0
    r.k.apply(RevokeInference(event_id="rv", ts=r.now, source_event_id=src))
    r.declare(None)
    assert r.k.belief(CELL, r.now).inferred_pos == 0


def test_replay_equals_the_run():
    r = Run()
    r.answer("+")
    r.declare("dont")
    c = r.k.copy()
    assert c.to_json() == r.k.to_json()


# ---- the wrong answer that still stands (decision 3) ----

def test_open_miss_only_a_page_answer_that_is_still_wrong():
    r = Run()
    assert r.k.open_miss(CELL) is None
    wrong = r.answer("-")
    assert r.k.open_miss(CELL) == wrong
    r.answer("+")
    assert r.k.open_miss(CELL) is None


def test_open_miss_counts_a_block_question_but_not_the_opening_probe():
    r = Run()
    r.answer("-", kind="quiz", ref="probe:1")
    assert r.k.open_miss(CELL) is None
    wrong = r.answer("-", kind="quiz", ref="block:b1:q1")
    assert r.k.open_miss(CELL) == wrong


def test_open_miss_leaves_out_review_and_confirmation_answers():
    r = Run()
    r.answer("-", kind="recall", ref="review:p:s:recall")
    r.answer("-", kind="check", ref="review:p:s:check")
    r.answer("-", kind="check", ref="confirm:f1")
    assert r.k.open_miss(CELL) is None


def test_open_miss_a_fuzzy_answer_is_not_wrong():
    r = Run()
    r.answer("~")
    assert r.k.open_miss(CELL) is None


def test_open_miss_a_claim_closes_it():
    r = Run()
    wrong = r.answer("-")
    r.k.apply(Claim(event_id="cl", ts=r.now, claim="slip", cell=CELL, source_event_id=wrong))
    assert r.k.open_miss(CELL) is None
