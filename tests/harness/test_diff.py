import pytest

from evowiki.harness.anchors import (
    H1_CLOSED_STEPS,
    H1_EVIDENCE_STEPS,
    H1_JUDGE_STEPS,
    H4_MAX_RAISE,
)
from evowiki.harness.diff import (
    RSI_STEP,
    KeyOp,
    PackDiff,
    apply_diff,
    closed_h1,
    complexity,
    diff_from_json,
    diff_to_json,
    refuse,
)
from evowiki.harness.pack import EMPTY, HarnessPack
from evowiki.llm.prompt_steps import STEPS
from evowiki.teach.contracts import SpecPatchOp

H4 = "H4.deep.max_tool_calls"
H1 = "H1.write"


def test_apply_round_trip_and_default_reset():
    p = HarnessPack(4, {H4: 80, H1: "旧"})
    out = apply_diff(p, PackDiff((KeyOp(H4, 90), KeyOp(H1, None))))
    assert out.version == 4 and dict(out.values) == {H4: 90}
    assert dict(p.values) == {H4: 80, H1: "旧"}


def test_apply_validates_values():
    with pytest.raises(ValueError):
        apply_diff(EMPTY, PackDiff((KeyOp(H4, 5000),)))


def test_refuse_empty():
    assert refuse(PackDiff(), EMPTY, {}, defaults={}) is not None


def test_refuse_dimension_and_agent():
    assert refuse(PackDiff((KeyOp("H6.keep_turns", 5),)), EMPTY, {}, defaults={}) is not None
    assert refuse(PackDiff((KeyOp("H4.bot.max_tool_calls", 3),)), EMPTY, {}, defaults={}) is not None
    assert refuse(PackDiff((KeyOp("H4.deep.max_rewrites", 2),)), EMPTY, {},
                  defaults={"H4.deep.max_rewrites": 2}) is None
    assert refuse(PackDiff((KeyOp(H1, "先举例"),)), EMPTY, {}, defaults={}) is None


def test_refuse_user_source():
    assert refuse(PackDiff((KeyOp(H1, "x"),)), EMPTY, {H1: "user"}, defaults={}) is not None
    assert refuse(PackDiff((KeyOp(H1, "x"),)), EMPTY, {H1: "improver"}, defaults={}) is None


def test_h4_raise_limit():
    p = HarnessPack(1, {H4: 100})
    assert refuse(PackDiff((KeyOp(H4, int(100 * H4_MAX_RAISE)),)), p, {}, defaults={}) is None
    assert refuse(PackDiff((KeyOp(H4, 151),)), p, {}, defaults={}) is not None
    assert refuse(PackDiff((KeyOp(H4, 50),)), p, {}, defaults={}) is None


def test_h4_raise_uses_default_when_absent():
    # no pack value: the effective value is the default -> 1.5 x default
    assert refuse(PackDiff((KeyOp(H4, 200),)), EMPTY, {}, defaults={H4: 80}) is not None
    assert refuse(PackDiff((KeyOp(H4, 120),)), EMPTY, {}, defaults={H4: 80}) is None


def test_h4_without_pack_value_or_default_is_refused():
    assert refuse(PackDiff((KeyOp(H4, 10),)), EMPTY, {}, defaults={}) is not None


def test_refuse_invalid_value():
    assert refuse(PackDiff((KeyOp(H4, 5000),)), EMPTY, {}, defaults={}) is not None
    assert refuse(PackDiff((KeyOp("H4.deep.nope", 5),)), EMPTY, {}, defaults={}) is not None


def test_spec_ops_allowed():
    d = PackDiff(spec_ops=(SpecPatchOp(op="add_item", text="先给例子"),))
    assert refuse(d, EMPTY, {}, defaults={}) is None


def test_complexity_grows_with_text():
    short = PackDiff((KeyOp(H1, "a" * 100),))
    long = PackDiff((KeyOp(H1, "a" * 300),))
    assert complexity(EMPTY, short, {}) == pytest.approx(2.0)
    assert complexity(EMPTY, long, {}) == pytest.approx(4.0)
    assert complexity(EMPTY, long, {H1: "a" * 300}) == pytest.approx(1.0)
    spec = PackDiff(spec_ops=(SpecPatchOp(op="add_item", text="b" * 100),))
    assert complexity(EMPTY, spec, {}) == pytest.approx(2.0)
    assert complexity(EMPTY, PackDiff((KeyOp(H4, 90),)), {}) == pytest.approx(1.0)


def test_json_round_trip_keeps_before_values():
    d = PackDiff((KeyOp(H4, 100), KeyOp(H1, None)),
                 (SpecPatchOp(op="add_item", item_id="i1", text="先给例子"),))
    raw = diff_to_json(d, {H4: 80, H1: "旧"}, 3)
    assert raw["ops"][0] == {"name": H4, "value": 100, "before": 80}
    back, before, ver = diff_from_json(raw)
    assert back == d and before == {H4: 80, H1: "旧"} and ver == 3
    assert diff_from_json(diff_to_json(PackDiff((KeyOp(H4, 90),)), {}))[1] == {H4: None}


def test_h4_back_to_default_follows_the_raise_rule():
    # final review M1: a reset may not jump to a default far above the current value
    low = HarnessPack(1, {H4: 20})
    assert refuse(PackDiff((KeyOp(H4, None),)), low, {}, defaults={H4: 80}) is not None
    assert refuse(PackDiff((KeyOp(H4, None),)), HarnessPack(1, {H4: 60}), {},
                  defaults={H4: 80}) is None
    assert refuse(PackDiff((KeyOp(H4, None),)), HarnessPack(1, {H4: 200}), {},
                  defaults={H4: 80}) is None                  # lowering back is fine
    assert refuse(PackDiff((KeyOp(H4, None),)), low, {}, defaults={}) is not None   # unknown default
    assert refuse(PackDiff((KeyOp(H1, None),)), EMPTY, {}, defaults={}) is None     # text keys: no rule


@pytest.mark.parametrize("step", sorted(H1_CLOSED_STEPS))
def test_refuse_rsi_judge_review_and_method_steps(step):
    # the RSI loops' own judge / reviewer / method: never automatic, back to default included;
    # a value the user set stays theirs (refused as an RSI step before the source rule)
    name = f"H1.{step}"
    for value in ("换一种比较方法", None):
        why = refuse(PackDiff((KeyOp(name, value),)), HarnessPack(1, {name: "旧"}),
                     {name: "improver"}, defaults={})
        assert why is not None and RSI_STEP in why
    # mixed with an allowed op the whole diff is refused
    assert refuse(PackDiff((KeyOp(H1, "先举例"), KeyOp(name, "x"))), EMPTY, {},
                  defaults={}) is not None


def test_closed_steps_are_the_feedback_step_only():
    # RSI-B's gate / reviewer / proposer / B2 steps retired (2026-10-29): no step is an RSI step
    assert not any(p.startswith("rsi.") for s in STEPS for p in (*s.purposes, s.source))
    assert H1_JUDGE_STEPS == frozenset()
    # the evidence: the feedback reader (RSI's signals), rule provenance contract P0
    assert H1_EVIDENCE_STEPS == {"feedback"}
    assert H1_CLOSED_STEPS == H1_EVIDENCE_STEPS | {"language_style"}
    assert closed_h1("H1.feedback") and not closed_h1("H1.write") and not closed_h1("H2.feedback")


def test_evaluation_prompts_carry_no_guidance_marks():
    # the judges' text is the same under every override the improver may write: they carry no
    # marked span at all
    import inspect

    from evowiki.evaluation import page as page_eval
    from evowiki.harness import judge_prompt
    from evowiki.llm.prompt_steps import apply_marks, marked

    texts = (inspect.getsource(judge_prompt), inspect.getsource(page_eval))
    assert not {k for t in texts for k in marked(t)}
    open_overrides = {s.key: "改过" for s in STEPS if s.key not in H1_CLOSED_STEPS}
    for t in texts:
        assert apply_marks(t, open_overrides)[0] == apply_marks(t, {})[0]
