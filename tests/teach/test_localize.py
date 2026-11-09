from evowiki.teach.contracts import SpecItem
from evowiki.teach.localize import MIN_SIGNALS, NEW_ITEM, localize
from tests.teach.spec_helpers import ITEMS, log, sig, spec, unclear_feedback


def _logs():
    return [log("p1", "s-a"), log("p1", "s-b"), log("p1", "s-c", role="recap")]


def test_unclear_feedback_is_traced_to_the_item_that_caused_it():
    out = localize(unclear_feedback(), _logs(), spec())
    [f] = out.b
    loc = f.localization
    assert (loc.track, loc.failure, loc.target, loc.ref) == ("B", "unclear", "spec_item", "s2")
    assert len(loc.signals) == 5 and set(f.sections) == {("p1", "s-a"), ("p1", "s-b")}
    assert "s2" in loc.reason and "不像人话" in loc.reason
    assert out.a == ()


def test_one_complaint_alone_starts_nothing():
    assert MIN_SIGNALS == 2
    out = localize(unclear_feedback()[:1], _logs(), spec())
    assert out.b == ()


def test_item_only_in_force_on_other_sections_is_not_blamed():
    logs = [log("p1", "s-a", items=("s1", "s3")), log("p1", "s-b", items=("s1", "s3")),
            log("p2", "x", items=("s2",))]
    [f] = localize(unclear_feedback(), logs, spec()).b
    assert f.localization.ref == NEW_ITEM                  # no unclear-related item in force


def test_item_in_force_where_the_user_said_it_was_fine_scores_lower():
    a = SpecItem(item_id="a", text="术语直接用，不解释。", origin="default", authority="rsi")
    b = SpecItem(item_id="b", text="术语用专业书面写法。", origin="default", authority="rsi")
    logs = [log("p1", "s-a", items=("a", "b")), log("p1", "s-b", items=("a", "b")),
            log("p2", "k1", items=("b",)), log("p2", "k2", items=("b",)),
            log("p2", "k3", items=("b",))]
    good = [sig("p2", s, kind="section_know") for s in ("k1", "k2", "k3")]
    [f] = localize(unclear_feedback() + good, logs, spec(a, b)).b
    assert f.localization.ref == "a"


def test_difficulty_on_sections_k_thought_known_goes_to_rsi_a():
    known = {"p1.c:term": "known"}
    logs = [log("p1", "s-a", bands=known), log("p1", "s-b", bands=known)]
    sigs = [sig("p1", "s-a", chips=("too_hard",)), sig("p1", "s-b", chips=("too_hard",)),
            sig("p1", "s-a", kind="section_deeper")]
    out = localize(sigs, logs, spec())
    assert out.b == ()
    [a] = out.a
    assert (a.localization.track, a.localization.failure, a.localization.target) == \
        ("A", "overconfident", "k_estimate")
    assert a.localization.ref == "p1.c:term"


def test_style_complaints_stay_with_the_teaching_spec_even_on_known_sections():
    known = {"p1.c:term": "known"}
    logs = [log("p1", "s-a", bands=known), log("p1", "s-b", bands=known)]
    sigs = [sig("p1", "s-a", chips=("too_long",)), sig("p1", "s-b", chips=("too_long",))]
    [f] = localize(sigs, logs, spec()).b
    assert (f.localization.failure, f.localization.ref) == ("too_long", "s4")


def test_expand_and_know_signals_hand_off_to_rsi_a():
    logs = [log("p1", "s-a", bands={"p1.c:term": "known"}),
            log("p1", "s-b", bands={"p1.c:term": "gap"})]
    out = localize([sig("p1", "s-a", kind="expand"), sig("p1", "s-b", kind="section_know")],
                   logs, spec())
    assert {f.localization.failure for f in out.a} == {"overconfident", "underconfident"}
    assert all(f.localization.target == "k_estimate" for f in out.a)


def test_no_related_item_falls_back_to_a_param_then_to_a_new_item():
    no_examples = [i for i in ITEMS if i.item_id != "s3"]
    sigs = [sig("p1", "s-a", chips=("no_example",)), sig("p1", "s-b", chips=("no_example",))]
    [f] = localize(sigs, _logs(), spec(*no_examples)).b
    assert (f.localization.target, f.localization.ref, f.param_direction) == \
        ("rep_param", "fading_level", -1)
    sigs = [sig("p1", "s-a", kind="section_other"), sig("p1", "s-b", kind="section_other")]
    s = spec(ITEMS[3]).model_copy(update={"params": spec().params.model_copy(
        update={"analogy_level": 2})})
    [f] = localize(sigs, _logs(), s).b
    assert (f.localization.failure, f.localization.ref) == ("misframed", NEW_ITEM)


def test_logs_of_another_spec_version_or_later_time_do_not_explain_a_signal():
    logs = [log("p1", "s-a", version=2), log("p1", "s-b", minute=500)]
    out = localize(unclear_feedback(), logs, spec())
    assert out.b == () and len(out.unplaced) == 5


def test_page_level_feedback_lands_on_the_teaching_sections():
    sigs = [sig("p1", None, chips=("unclear",)), sig("p1", None, chips=("unclear",))]
    [f] = localize(sigs, _logs(), spec()).b
    assert set(f.sections) == {("p1", "s-a"), ("p1", "s-b")}     # not the recap s-c
    assert f.localization.ref == "s2"


def test_review_card_feedback_maps_unclear_to_b_and_too_easy_to_rsi_a():
    from evowiki.teach.signal_kinds import polarity
    logs = [log("p1", "s-a"), log("p1", "s-b")]
    unclear = [sig("p1", "s-a", kind="card_feedback", text="题目没说清楚：问的是哪个"),
               sig("p1", "s-b", kind="card_feedback", text="题目没说清楚")]
    [f] = localize(unclear, logs, spec()).b
    assert f.localization.failure == "unclear"
    easy = [sig("p1", "s-a", kind="card_feedback", text="太简单了")]
    out = localize(easy, logs, spec())
    assert out.b == () and [a.localization.failure for a in out.a] == ["underconfident"]
    assert localize([sig("p1", "s-a", kind="card_feedback", text="答案不对：应该是 B")],
                    logs, spec()).b == ()
    assert polarity(unclear[0]) == -1 and polarity(easy[0]) == 0


# ---- signal weights and threshold ----

def test_zero_weight_makes_a_page_feedback_only_cluster_disappear():
    assert localize(unclear_feedback(), _logs(), spec()).b
    out = localize(unclear_feedback(), _logs(), spec(), signal_weights={"page_feedback": 0.0})
    assert out.b == ()


def test_weights_and_min_signals_set_the_threshold():
    two = unclear_feedback()[:2]                      # two page-feedback complaints
    assert localize(two, _logs(), spec()).b
    assert localize(two, _logs(), spec(), min_signals=3).b == ()
    assert localize(two, _logs(), spec(), min_signals=3, signal_weights={"page_feedback": 1.5}).b
    assert localize(two, _logs(), spec(), signal_weights={"page_feedback": 0.5}).b == ()
    # other kinds keep weight 1: a 「再讲深一点」 pair still forms its cluster
    deeper = [sig("p1", "s-a", kind="section_deeper"), sig("p1", "s-b", kind="section_deeper")]
    [f] = localize(deeper, _logs(), spec(), signal_weights={"page_feedback": 0.0}).b
    assert f.localization.failure == "too_shallow"


def test_findings_rank_by_weighted_votes():
    unclear = unclear_feedback()                                       # 5 page_feedback
    deeper = [sig("p1", "s-a", kind="section_deeper") for _ in range(3)]
    plain = localize(unclear + deeper, _logs(), spec()).b
    assert plain[0].localization.failure == "unclear"
    weighted = localize(unclear + deeper, _logs(), spec(),
                        signal_weights={"page_feedback": 0.4, "section_deeper": 2.0}).b
    assert [f.localization.failure for f in weighted] == ["too_shallow", "unclear"]


def test_assistant_rejected_is_not_mapped_to_a_page_section():
    from datetime import UTC, datetime

    from evowiki.teach.contracts import Signal
    rejected = [Signal(signal_id=f"arej:{i}", kind="assistant_rejected",
                       at=datetime(2026, 10, 20, tzinfo=UTC), page_id="p1", text="拒绝了助手的修改建议")
                for i in range(3)]
    out = localize(rejected, _logs(), spec(), min_signals=1)
    assert out.b == () and out.a == () and out.unplaced == ()
    mixed = localize(unclear_feedback() + rejected, _logs(), spec())
    assert {s for f in mixed.b for s in f.localization.signals}.isdisjoint(
        {s.signal_id for s in rejected})


def test_assistant_rated_is_not_mapped_to_a_page_section():
    from datetime import UTC, datetime

    from evowiki.teach.contracts import Signal
    rated = [Signal(signal_id=f"arat:a{i}", kind="assistant_rated",
                    at=datetime(2026, 10, 22, tzinfo=UTC), page_id="p1", text="觉得这次回答不好：不对")
             for i in range(3)]
    out = localize(rated, _logs(), spec(), min_signals=1)
    assert out.b == () and out.a == () and out.unplaced == ()
