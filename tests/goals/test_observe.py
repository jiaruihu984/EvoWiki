
import pytest

from evowiki.goals.contracts import Aspect
from evowiki.goals.observe import RawObservation, self_observations

from .fakes_intake import FakeK

DESC = "我会算均值，方差公式记不太清，没学过 z-score"
CELLS = {"c1.procedure", "c2.notation", "c3.interpret"}


def _raw(**kw) -> RawObservation:
    return RawObservation(**kw)


def test_self_keeps_verbatim_quotes():
    obs, dropped = self_observations(
        [_raw(cell="c1.procedure", polarity="+", strength=0.8, quote="我会算均值"),
         _raw(cell="c2.notation", polarity="~", strength=0.6, quote="方差公式记不太清")],
        DESC, CELLS, evidence_ref="self:1")
    assert [(o.cell, o.polarity, o.kind, o.quote) for o in obs] == [
        ("c1.procedure", "+", "self", "我会算均值"), ("c2.notation", "~", "self", "方差公式记不太清")]
    assert all(o.evidence_ref == "self:1" for o in obs)
    assert dropped == ()


def test_self_drops_fabricated_or_missing_quotes():
    obs, dropped = self_observations(
        [_raw(cell="c1.procedure", polarity="+", strength=0.8, quote="我精通均值的计算"),
         _raw(cell="c2.notation", polarity="-", strength=0.5, quote=""),
         _raw(cell="c3.interpret", polarity="-", strength=0.5)],
        DESC, CELLS, evidence_ref="self:1")
    assert obs == ()
    assert [d.reason for d in dropped] == ["quote_not_verbatim", "quote_not_verbatim",
                                           "quote_not_verbatim"]


def test_self_drops_unknown_cell_aspect_and_polarity():
    obs, dropped = self_observations(
        [_raw(cell="c1.relation", polarity="+", strength=0.8, quote="我会算均值"),
         _raw(cell="c9.term", polarity="+", strength=0.8, quote="我会算均值"),
         _raw(cell="c1.procedure", polarity="maybe", strength=0.8, quote="我会算均值"),
         _raw(cell=None, polarity="+", strength=0.8, quote="我会算均值")],
        DESC, CELLS, evidence_ref="self:1")
    assert obs == ()
    assert [d.reason for d in dropped] == ["bad_aspect", "unknown_cell", "bad_polarity",
                                           "unknown_cell"]


def test_self_clamps_strength_and_defaults_it():
    obs, _ = self_observations(
        [_raw(cell="c1.procedure", polarity="+", strength=3.0, quote="我会算均值"),
         _raw(cell="c2.notation", polarity="~", quote="方差公式记不太清")],
        DESC, CELLS, evidence_ref="self:1")
    assert [o.strength for o in obs] == [1.0, 0.5]


def _page_k() -> FakeK:
    return FakeK({"c1": [Aspect.PROCEDURE, Aspect.TERM], "c2": [Aspect.NOTATION]})


@pytest.mark.parametrize("strength", [float("nan"), float("inf"), float("-inf")])
def test_self_non_finite_strength_defaults(strength):
    obs, _ = self_observations(
        [_raw(cell="c1.procedure", polarity="+", strength=strength, quote="我会算均值")],
        DESC, CELLS, evidence_ref="self:1")
    assert [o.strength for o in obs] == [0.5]
