from datetime import UTC, datetime, timedelta
from math import log1p

import pytest
from pydantic import ValidationError

from evowiki.belief.aspects import AspectConfig, AspectK, Declare, DefineConcept, Observe
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.contracts import (
    Aspect,
    Band,
    CheckItem,
    Observation,
    OutlineSection,
    PageOutline,
    SectionRole,
    SectionText,
)
from evowiki.goals.review import (
    ReviewSettings,
    ReviewSource,
    dependents,
    digest,
    fading,
    last_answered,
    recall_observation,
    reviews_between,
    reviews_done,
    schedule,
    sources_from_page,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
ALL = tuple(Aspect)
# FSRS: one quiz '+' -> S = 2.3065 days = the time recall takes to fall to 0.9 (the default desired
# retention), so it is due after 2.3065 days (horizon 0); its band drops (recall < 0.6) after 62.25.


def k_with(*concepts):
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default()))
    for i, (c, prereqs) in enumerate(concepts):
        k.apply(DefineConcept(event_id=f"d{i}", ts=T0, concept_id=c, aspects=ALL, prereqs=prereqs))
    return k


_n = iter(range(10_000))


def quiz(k, cell, pol="+", ts=T0):
    k.apply(Observe(event_id=f"q{next(_n)}", ts=ts, observation=Observation(
        cell=cell, polarity=pol, strength=1.0, kind="recall" if pol == "+" else "quiz", evidence_ref="ev")))


def cue_src(cell, page="p", sec=None):
    return ReviewSource(page_id=page, section_id=sec or f"s-{cell}", heading="h",
                        cells=(cell,), cue="一句回想提示")


@pytest.mark.parametrize(("grade", "pol"), [("recalled", "+"), ("fuzzy", "~"), ("forgot", "-")])
def test_recall_observation_grades(grade, pol):
    o = recall_observation("z.term", grade, "rev-1")
    assert (o.kind, o.polarity, o.strength, o.evidence_ref) == ("recall", pol, 1.0, "rev-1")


def test_sources_from_page_uses_cue_and_verified_check_only():
    chk = CheckItem(cell="z.procedure", stem="?", options=("a", "b", "c"), answer=0, verified=True)
    raw = CheckItem(cell="z.term", stem="?", options=("a", "b", "c"), answer=0, verified=False)
    outline = PageOutline(page_id="p", goal="g", concepts=("z",), targets=("z",), sections=(
        OutlineSection(id="s1", heading="讲透", role=SectionRole.EXPLAIN, cells=("z.term",)),
        OutlineSection(id="s2", heading="无提示", role=SectionRole.EXPLAIN, cells=("z.notation",)),
        OutlineSection(id="s3", heading="确认", role=SectionRole.CHECK, check=chk),
        OutlineSection(id="s4", heading="未核", role=SectionRole.CHECK, check=raw),
    ))
    texts = {"s1": SectionText(section_id="s1", cue="定义是…", answer="a", body="b", cache_key="c"),
             "s2": SectionText(section_id="s2", cue="  ", body="b", cache_key="c")}
    got = sources_from_page(outline, texts)
    assert [(s.section_id, s.cells, s.cue, s.check) for s in got] == [
        ("s1", ("z.term",), "定义是…", None), ("s3", ("z.procedure",), "", chk)]


def test_a_cue_card_needs_the_section_text_and_an_answer():
    outline = PageOutline(page_id="p", goal="g", concepts=("z",), targets=("z",), sections=(
        OutlineSection(id="s1", heading="有正文", role=SectionRole.EXPLAIN, cells=("z.term",)),
        OutlineSection(id="s2", heading="没正文", role=SectionRole.EXPLAIN, cells=("z.notation",)),
        OutlineSection(id="s3", heading="没答案", role=SectionRole.EXPLAIN, cells=("z.purpose",)),
    ))
    texts = {"s1": SectionText(section_id="s1", cue="z 是什么？", answer="标准分。", body="z 是**标准分**。",
                               cache_key="c"),
             "s2": SectionText(section_id="s2", cue="记号？", answer="z。", body=" ", cache_key="c"),
             "s3": SectionText(section_id="s3", cue="只是折叠标题", body="正文", cache_key="c")}
    got = sources_from_page(outline, texts)
    assert [(s.section_id, s.cue, s.answer) for s in got] == [("s1", "z 是什么？", "标准分。")]


def test_a_cell_graded_fuzzy_or_forgot_comes_back_when_fsrs_says_so():
    """Live 2026-11-04: 模糊 took the band to shaky and the card never came back, 60 days on."""
    k = k_with(("z", ()))
    quiz(k, "z.term")
    for cell, grade in (("z.term", "~"), ("z.notation", "-")):
        k.apply(Observe(event_id=f"g-{cell}", ts=T0 + timedelta(days=5), observation=Observation(
            cell=cell, polarity=grade, strength=1.0, kind="recall", evidence_ref="review:p:s:rv")))
    assert k.belief("z.notation", T0, decay=False).band not in (Band.KNOWN, Band.LIKELY)
    interval = k.memory("z.notation").stability
    assert [f.cell for f in fading(k, T0 + timedelta(days=5 + interval / 2))] == []
    assert "z.notation" in [f.cell for f in fading(k, T0 + timedelta(days=5 + interval + 0.01))]
    assert "z.term" in [f.cell for f in fading(k, T0 + timedelta(days=90))]


def test_fading_lists_only_tested_learned_undeclared_cells():
    k = k_with(("z", ()))
    quiz(k, "z.term")
    quiz(k, "z.notation", "-")                       # gap: not a review target
    quiz(k, "z.purpose")
    k.apply(Declare(event_id="dec", ts=T0, cell="z.purpose", value="know"))
    assert fading(k, T0 + timedelta(days=2)) == ()
    got = fading(k, T0 + timedelta(days=3))
    assert [f.cell for f in got] == ["z.term"]
    f = got[0]
    assert f.band is Band.LIKELY and f.band_undecayed is Band.LIKELY
    assert f.days_to_threshold == pytest.approx(2.3065 - 3, abs=1e-4)
    assert f.recall_at_horizon == f.recall_now < 0.9
    late = fading(k, T0 + timedelta(days=70))[0]
    assert late.band is Band.SHAKY and late.band_undecayed is Band.LIKELY
    assert late.days_to_threshold < 0


def test_dependents_counts_direct_prereq_users():
    k = k_with(("a", ()), ("b", ("a",)), ("c", ("a", "b")))
    assert dependents(k) == {"a": 2, "b": 1, "c": 0}


def test_schedule_orders_by_importance_then_urgency():
    k = k_with(("a", ()), ("b", ("a",)), ("c", ("a",)), ("d", ()))
    quiz(k, "a.term")
    quiz(k, "d.term")
    srcs = (cue_src("a.term"), cue_src("d.term"))
    now = T0 + timedelta(days=12)
    plan = schedule(k, now, srcs)
    assert [i.cell for i in plan.items] == ["a.term", "d.term"]   # a is a prereq of 2 concepts
    assert plan.items[0].dependents == 2 and plan.due == 2
    plan = schedule(k, now, srcs, goal_concepts=frozenset({"d"}))
    assert plan.items[1].in_goal and plan.items[1].score == pytest.approx(
        2 * (1 - plan.items[1].recall_at_horizon))


def test_schedule_more_urgent_first_when_equally_important():
    k = k_with(("x", ()), ("y", ()))
    quiz(k, "x.term")
    quiz(k, "y.term", ts=T0 + timedelta(days=1))
    plan = schedule(k, T0 + timedelta(days=13), (cue_src("x.term"), cue_src("y.term")))
    assert [i.cell for i in plan.items] == ["x.term", "y.term"]


def test_schedule_cap_and_already_reviewed():
    k = k_with(("a", ()), ("b", ()), ("c", ()))
    for c in "abc":
        quiz(k, f"{c}.term")
    srcs = tuple(cue_src(f"{c}.term") for c in "abc")
    now = T0 + timedelta(days=12)
    assert len(schedule(k, now, srcs).items) == 3
    assert len(schedule(k, now, srcs, settings=ReviewSettings(max_per_day=2)).items) == 2
    assert schedule(k, now, srcs, settings=ReviewSettings(max_per_day=2),
                    already_reviewed=2).items == ()
    assert schedule(k, now, srcs).due == 3


def test_schedule_takes_the_first_card_of_a_cell_in_page_order_else_no_material():
    k = k_with(("z", ()))
    for a in ("term", "procedure", "notation"):
        quiz(k, f"z.{a}")
    chk = CheckItem(cell="z.procedure", stem="?", options=("a", "b", "c"), answer=1, verified=True)
    chk_term = CheckItem(cell="z.term", stem="?", options=("a", "b", "c"), answer=1, verified=True)
    srcs = (ReviewSource(page_id="p", section_id="c1", heading="h", cells=("z.term",),
                         check=chk_term),
            cue_src("z.term", sec="s1"),
            ReviewSource(page_id="p", section_id="c2", heading="h", cells=("z.procedure",),
                         check=chk))
    plan = schedule(k, T0 + timedelta(days=12), srcs)
    modes = {i.cell: (i.mode, i.section_id) for i in plan.items}
    assert modes == {"z.term": ("check", "c1"), "z.procedure": ("check", "c2")}
    assert plan.due == 2      # cards, not cells: z.notation has no card


def test_the_cards_of_a_cell_take_turns_longest_ago_first():
    k = k_with(("z", ()))
    quiz(k, "z.term")
    chk = CheckItem(cell="z.term", stem="?", options=("a", "b", "c"), answer=1, verified=True)
    srcs = (cue_src("z.term", sec="s1"), cue_src("z.term", sec="s2"),
            ReviewSource(page_id="p", section_id="c", heading="h", cells=("z.term",), check=chk))

    def card(**answered):
        at = {("p", s, "check" if s == "c" else "cue"): T0 + timedelta(days=d) for s, d in answered.items()}
        item = schedule(k, T0 + timedelta(days=12), srcs, answered=at).items[0]
        return item.mode, item.section_id

    assert card() == ("cue", "s1")
    assert card(s1=1) == ("cue", "s2")
    assert card(s1=1, s2=2) == ("check", "c")
    assert card(s1=3, s2=2, c=1) == ("check", "c")
    assert card(s1=1, s2=2, c=3) == ("cue", "s1")


def test_last_answered_reads_review_refs_before_today():
    def ob(i, kind, ref, ts):
        return Observe(event_id=f"la{i}", ts=ts, observation=Observation(
            cell="z.term", polarity="+", strength=1.0, kind=kind, evidence_ref=ref))

    today = T0 + timedelta(days=2)
    events = [ob(1, "recall", "review:p:s1:rv1", T0),
              ob(2, "recall", "review:p:s1:rv2", T0 + timedelta(days=1)),
              ob(3, "check", "review:p:s1:rv3", T0),                     # the same section's check card
              ob(4, "check", "check:p:c1", T0),                          # a page's own check: no review
              ob(5, "recall", "review:p:s2:rv4", today)]                 # today's answers do not move the turn
    assert last_answered(events, today) == {("p", "s1", "cue"): T0 + timedelta(days=1),
                                            ("p", "s1", "check"): T0}


def test_settings_bounds():
    with pytest.raises(ValidationError):
        ReviewSettings(max_per_day=-1)
    with pytest.raises(ValidationError):
        ReviewSettings(horizon_days=31)


def test_schedule_is_deterministic():
    k = k_with(("a", ()), ("b", ()))
    quiz(k, "a.term")
    quiz(k, "b.term")
    srcs = (cue_src("b.term"), cue_src("a.term"))
    now = T0 + timedelta(days=12)
    assert schedule(k, now, srcs) == schedule(k, now, srcs)
    assert [i.cell for i in schedule(k, now, srcs).items] == ["a.term", "b.term"]  # tie: by id


def test_naive_as_of_rejected_even_without_tested_cells():
    with pytest.raises(ValueError):
        schedule(k_with(("a", ())), datetime(2026, 10, 16))  # noqa: DTZ001


def test_relearning_cell_is_fading_and_scheduled_at_full_urgency():
    k = k_with(("a", ()), ("b", ("a",)))
    for _ in range(4):
        quiz(k, "a.term")
    t = T0 + timedelta(days=3650)
    k.apply(Observe(event_id="miss", ts=t, observation=recall_observation("a.term", "forgot",
                                                                           "rev-1")))
    now = t + timedelta(days=1)
    got = [f for f in fading(k, now) if f.cell == "a.term"]
    assert len(got) == 1
    f = got[0]
    assert f.relearning is True and f.band is Band.SHAKY
    assert f.recall_at_horizon >= 0.6          # fresh recall: only the flag keeps it here
    plan = schedule(k, now, (cue_src("a.term"),))
    [item] = plan.items
    assert item.cell == "a.term"
    assert item.score == pytest.approx(1.0 * (1.0 + log1p(1)))   # importance: one dependent


def test_schedule_exam_concept_ranks_first_and_default_is_unchanged():
    k = k_with(("x", ()), ("y", ()))
    quiz(k, "x.term")
    quiz(k, "y.term")                 # equally urgent and important
    srcs = (cue_src("x.term"), cue_src("y.term"))
    now = T0 + timedelta(days=13)
    base = schedule(k, now, srcs)
    assert [i.cell for i in base.items] == ["x.term", "y.term"]
    assert not any(i.exam for i in base.items)
    plan = schedule(k, now, srcs, exam_concepts=frozenset({"y"}))
    assert [i.cell for i in plan.items] == ["y.term", "x.term"]
    y, x = plan.items
    assert y.exam and not x.exam
    assert y.score == pytest.approx(x.score * 2)       # 1 + EXAM_BONUS over 1
    assert x.score == pytest.approx(base.items[0].score)


def test_self_grades_are_fsrs_ratings():
    """想起来了 / 有点模糊 / 没想起来 = Good / Hard / Again (FSRS, 2026-10-24): five days after a
    correct answer, Good grows stability most, Hard less (never below the previous value), Again
    lowers it; the next due date moves the same way."""
    out = {}
    for grade in ("recalled", "fuzzy", "forgot"):
        k = k_with(("z", ()))
        quiz(k, "z.term")
        s0 = k.memory("z.term").stability
        t = T0 + timedelta(days=5)
        k.apply(Observe(event_id=f"r-{grade}", ts=t,
                        observation=recall_observation("z.term", grade, "ev")))
        out[grade] = k.memory("z.term").stability
    assert out["recalled"] > out["fuzzy"] >= s0 > out["forgot"]


def test_a_card_is_one_section_cue_and_reviews_only_its_due_cells():
    """D15: two due cells of one cue are one card (the lead = the more important one); a cell
    shared with a later section goes to the first; a section cell that is not due is not on it,
    and a section whose due cells all went elsewhere is no card."""
    k = k_with(("a", ()), ("b", ("a",)), ("z", ()))
    quiz(k, "a.term")
    quiz(k, "a.notation")
    quiz(k, "z.term")
    quiz(k, "a.purpose", ts=T0 + timedelta(days=11))     # tested lately: not due on day 12
    first = ReviewSource(page_id="p", section_id="s1", heading="h", cue="cue 1",
                         cells=("a.notation", "a.term", "a.purpose"))
    later = ReviewSource(page_id="p", section_id="s2", heading="h", cue="cue 2",
                         cells=("a.term",))
    srcs = (first, later, cue_src("z.term"))
    plan = schedule(k, T0 + timedelta(days=12), srcs)
    assert [(i.section_id, i.cells) for i in plan.items] == [
        ("s1", ("a.notation", "a.term")), ("s-z.term", ("z.term",))]
    assert plan.items[0].cell == "a.notation" and plan.due == 2
    capped = schedule(k, T0 + timedelta(days=12), srcs, settings=ReviewSettings(max_per_day=1))
    assert len(capped.items) == 1 and capped.due == 2


def test_one_card_answer_counts_once():
    t = T0 + timedelta(days=12)
    evs = [Observe(event_id=f"o{c}", ts=t, observation=recall_observation(c, "recalled", "rv1"))
           for c in ("a.term", "a.notation")]
    evs.append(Observe(event_id="o3", ts=t, observation=recall_observation("z.term", "forgot",
                                                                           "rv2")))
    assert reviews_done(evs, T0, t) == 2
    assert reviews_between(evs, T0, t) == {"recalled": 1, "fuzzy": 0, "forgot": 1}



# ---- 「考考我这页」: the page's cards, due or not ----

def test_page_cards_lists_a_cells_card_whether_due_or_not_and_an_early_grade_gains_little():
    from evowiki.goals.contracts import Aspect, Observation
    from evowiki.goals.review import ReviewSource, fading, page_cards

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    k = AspectK(AspectConfig.from_theta(ThetaInfer.default()))
    k.apply(DefineConcept(event_id="d", ts=t0, concept_id="c", aspects=(Aspect.TERM, Aspect.NOTATION)))
    review = lambda eid, days: k.apply(Observe(
        event_id=eid, ts=t0 + timedelta(days=days), observation=Observation(
            cell="c.term", polarity="+", strength=1.0, kind="recall", evidence_ref=f"review:p:s:{eid}")))
    review("a", 0)
    review("b", 6)                                   # stability well above a day: not due tomorrow
    tomorrow = t0 + timedelta(days=7)
    assert fading(k, tomorrow) == ()
    source = ReviewSource(page_id="p", section_id="s", heading="h", cells=("c.term", "c.notation"),
                          cue="问", answer="答")
    plan = page_cards(k, tomorrow, (source,))
    assert [(i.page_id, i.cells) for i in plan.items] in (
        [("p", ("c.term", "c.notation"))], [("p", ("c.notation", "c.term"))])
    before = k.memory("c.term").stability
    review("c", 7)
    early = k.memory("c.term").stability
    assert before < early < before * 1.5                  # a due review of the same card multiplies it



def test_a_page_rounds_answers_do_not_count_against_the_daily_batch():
    from evowiki.goals.contracts import Observation
    from evowiki.goals.review import PAGE_ROUND_ID, reviews_done

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    answer = lambda eid, ref: Observe(event_id=eid, ts=t0, observation=Observation(
        cell="c.term", polarity="+", strength=1.0, kind="recall", evidence_ref=ref))
    events = [answer("a", "review:p:s:rv_1"), answer("b", f"review:p:s:{PAGE_ROUND_ID}_1")]
    assert reviews_done(events, t0 - timedelta(days=1), t0) == 1


def test_the_digest_counts_as_fading_only_what_he_can_review():
    """The failure: cells no card covers were counted in 快忘了, and nothing could be done about them."""
    k_events = [DefineConcept(event_id="def", ts=T0, concept_id="z", aspects=ALL)]
    for i, a in enumerate(("term", "notation")):
        k_events.append(Observe(event_id=f"q{i}", ts=T0, observation=Observation(
            cell=f"z.{a}", polarity="+", strength=1.0, kind="recall", evidence_ref="ev")))
    as_of = T0 + timedelta(days=12)
    d = digest(k_events, k_with(("z", ())).config, as_of, "week", (cue_src("z.term", sec="s1"),))
    assert [f.cell for f in d.fading] == ["z.term"]
    assert [i.cell for i in d.today.items] == ["z.term"]
