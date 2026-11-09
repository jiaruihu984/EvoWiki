"""eval.page.judge (page-eval@v1, src/evowiki/evaluation/page.py), offline with a scripted judge: the page evaluator's core kept from RSI-B's Gate judge (both orders, the
missing-prerequisite veto, the length rule, the static floor, the anchor veto)."""

from evowiki.evaluation import page as pe
from evowiki.evaluation.page import EvalCase, SectionText

OLD = SectionText("回想旧", "旧的讲法：先给定义，再说用途，最后给一个小例子。" * 2)
NEW = SectionText("回想新", "新的讲法：先给一个能算出结果的小例子，再给定义和用途。" * 2)
LONG = SectionText("回想新", NEW.body * 3)


class Scripted:
    """Answers in order; records each request."""

    def __init__(self, *outs: dict):
        self.outs = list(outs)
        self.requests = []

    def call(self, role, req, schema):
        self.requests.append((role, req))
        return schema.model_validate(self.outs.pop(0)), None


def _r(better: int, p1: int = 0, p2: int = 0) -> dict:
    return {"better": better, "missing_prereq_1": p1, "missing_prereq_2": p2, "reason": "r"}


def test_new_wins_in_both_orders_passes():
    llm = Scripted(_r(2), _r(1))
    out = pe.evaluate([EvalCase("c1", OLD, NEW)], llm=llm, subject="u")
    assert out.passed and out.utility == 1.0 and out.vetoes == ()
    assert out.cases[0].verdict == "better"
    role, req = llm.requests[0]
    assert role == "verifier" and req.purpose == "eval.page.judge"
    assert "⟦g:" not in req.prompt                              # no guidance marker reaches it


def test_flip_is_a_tie_and_does_not_pass():
    out = pe.evaluate([EvalCase("c1", OLD, NEW)], llm=Scripted(_r(2), _r(2)), subject="u")
    assert not out.passed and out.cases[0].verdict == "same"
    assert pe.FLIP_NOTE in out.cases[0].note


def test_more_unexplained_concepts_in_both_orders_is_a_veto():
    # old-new order: _1 = old, _2 = new; new-old order: _1 = new, _2 = old
    out = pe.evaluate([EvalCase("c1", OLD, NEW)], llm=Scripted(_r(2, 0, 2), _r(1, 3, 1)),
                      subject="u")
    assert out.vetoes == (pe.VETO_PREREQ,) and not out.passed
    assert out.prereq == (1, 5)
    # only one order says more: no veto
    out = pe.evaluate([EvalCase("c1", OLD, NEW)], llm=Scripted(_r(2, 0, 2), _r(1, 0, 1)),
                      subject="u")
    assert out.vetoes == () and out.passed


def test_longer_win_is_a_tie_unless_the_learner_asked_for_more():
    out = pe.evaluate([EvalCase("c1", OLD, LONG)], llm=Scripted(_r(2), _r(1)), subject="u")
    assert out.cases[0].verdict == "same" and pe.LONGER_NOTE in out.cases[0].note
    more = EvalCase("c1", OLD, LONG, quotes=("讲得太浅",))
    out = pe.evaluate([more], llm=Scripted(_r(2), _r(1)), subject="u")
    assert out.cases[0].verdict == "better"


def test_static_floor_and_anchor_loss_are_vetoes():
    bad = SectionText("", "## 标题\n太短")
    llm = Scripted()
    out = pe.evaluate([EvalCase("c1", OLD, bad)], llm=llm, subject="u")
    assert out.vetoes == (pe.VETO_BASELINE,) and llm.requests == []
    assert "缺少回想提示" in out.cases[0].note
    out = pe.evaluate([EvalCase("a1", OLD, NEW, anchor=True), EvalCase("c1", OLD, NEW)],
                      llm=Scripted(_r(1), _r(2)), subject="u")
    assert out.vetoes == (pe.VETO_ANCHOR,) and len(out.cases) == 1


def test_a_failed_call_is_an_error_not_a_verdict():
    class Broken:
        def call(self, role, req, schema):
            raise RuntimeError("down")

    out = pe.evaluate([EvalCase("c1", OLD, NEW)], llm=Broken(), subject="u")
    assert out.vetoes == (pe.VETO_ERROR,) and "down" in out.error and out.cases == ()
