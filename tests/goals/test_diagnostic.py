"""goals/diagnostic: the shared single-answer-question helpers (deterministic shuffle, answers ->
observations). The per-cell generator was deleted (unreachable from the product)."""

from datetime import UTC, datetime, timedelta

import pytest

from evowiki.belief.aspects import AspectConfig, AspectK, DefineConcept, Observe
from evowiki.belief.memory import days_to
from evowiki.belief.theta import ThetaInfer
from evowiki.goals import diagnostic
from evowiki.goals.contracts import Aspect, CheckItem
from evowiki.goals.diagnostic import IDK, _options, _shuffled, answer_observation
from evowiki.goals.review import DESIRED_RETENTION

RAW = [CheckItem(cell="z.notation", stem="z 的分母是？", options=("σ", "μ", "n", "σ²"), answer=0),
       CheckItem(cell="z.interpret", stem="z=1 表示？",
                 options=("高一个标准差", "等于均值", "高 1 分", "前 1%"), answer=0),
       CheckItem(cell="mean.procedure", stem="1,2,3 的均值？", options=("2", "6", "3", "1.5"),
                 answer=0)]


def test_options_are_shuffled_deterministically_and_the_key_follows():
    a = [_shuffled(i) for i in RAW]
    assert a == [_shuffled(i) for i in RAW]
    assert any(i.answer != 0 for i in a)                   # every raw key is first, not after
    for raw, i in zip(RAW, a, strict=True):
        assert sorted(i.options) == sorted(raw.options)
        assert i.options[i.answer] == raw.options[raw.answer]


def test_options_are_strings_and_odd_types_are_refused():
    assert _options(["2", 6, 1.5]) == ("2", "6", "1.5")
    assert _options(["a", True]) is None and _options(["a", {"x": 1}]) is None


ITEM = CheckItem(cell="z.notation", stem="s", options=("a", "b", "c", "d"), answer=1,
                 verified=True)


@pytest.mark.parametrize(("answer", "want"), [(1, ("+", 1.0)), (2, ("-", 1.0)),
                                              (IDK, ("-", 0.8))])
@pytest.mark.parametrize("kind", ["quiz", "check"])
def test_answer_observation(answer, want, kind):
    o = answer_observation(ITEM, answer, kind=kind, evidence_ref="ev:1")
    assert (o.polarity, o.strength, o.kind, o.cell, o.evidence_ref) == (*want, kind,
                                                                         "z.notation", "ev:1")


def test_unverified_items_never_count_and_out_of_range_is_an_error():
    assert answer_observation(ITEM.model_copy(update={"verified": False}), 1, kind="quiz",
                              evidence_ref="e") is None
    with pytest.raises(ValueError):
        answer_observation(ITEM, 4, kind="quiz", evidence_ref="e")


def test_no_generator_and_no_verifier_left():
    for name in ("generate_items", "GENERATE_PURPOSE", "GENERATE_PROMPT", "verify_items",
                 "VERIFY_PURPOSE", "accept_items"):
        assert not hasattr(diagnostic, name), name


@pytest.mark.parametrize("text", [
    "x^2 + y_i = 3", "z = (x − μ) / σ", "$5 和 $10", "$5-$10", 'print("a\nb")',
    r're.match(r"\d+", s)', "Σ x 和 ∂f/∂x", "A 的转置 Aᵀ", "f(x) = 2x，[1, 2]"])
def test_plain_text_math_is_not_latex(text):
    assert diagnostic.latex_in(text) is None


@pytest.mark.parametrize("text, found", [
    ("$x^2$ 等于？", "$x^2$"), (r"等于 $\mu$ 吗", r"$\mu$"), ("$$ a $$", "$$"),
    (r"\frac{1}{2}", r"\frac"), (r"\(x\)", r"\(x\)"), (r"\[a\]", r"\[a\]"), ("x^{2}", "^{"),
    ("a_{ij}", "_{"), (r"\alpha 是", r"\alpha"), (r"\sum_i x_i", r"\sum")])
def test_latex_markup_is_found(text, found):
    assert diagnostic.latex_in("题干", text) == found
    item = CheckItem(cell="z.term", stem="哪个对？", options=("甲", text, "丙"), answer=0)
    assert diagnostic.item_latex(item) == found


def test_a_wrong_pick_is_a_miss_in_k_and_brings_the_card_back_soon():
    """Live 2026-11-04: a wrong pick read as Hard took S 13.8 -> 36.3 days, a month before the card returned."""
    t0 = datetime(2026, 10, 1, 9, tzinfo=UTC)
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default()))
    k.apply(DefineConcept(event_id="d", ts=t0, concept_id="z", aspects=tuple(Aspect), prereqs=()))
    for i, days in enumerate((0, 5)):
        k.apply(Observe(event_id=f"o{i}", ts=t0 + timedelta(days=days),
                        observation=answer_observation(ITEM, ITEM.answer, kind="check", evidence_ref=f"e{i}")))
    before = k.memory("z.notation").stability
    wrong = answer_observation(ITEM, 2, kind="check", evidence_ref="w")
    k.apply(Observe(event_id="w", ts=t0 + timedelta(days=30), observation=wrong))
    after = k.memory("z.notation").stability
    assert before > 9 and after < 3 and days_to(after, DESIRED_RETENTION) < 3   # two right choices: Good x 0.6
