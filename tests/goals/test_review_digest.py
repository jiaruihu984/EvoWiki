from datetime import UTC, datetime, timedelta

import pytest

from evowiki.belief.aspects import AspectConfig, AspectK, DefineConcept, Observe
from evowiki.belief.theta import ThetaInfer
from evowiki.goals.contracts import Aspect, Band, Observation
from evowiki.goals.review import (
    ReviewSource,
    digest,
    fading,
    recall_observation,
    replay,
    reviews_between,
    reviews_done,
    schedule,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
CFG = AspectConfig.from_theta(ThetaInfer.default())
ALL = tuple(Aspect)


def define(i, c, prereqs=()):
    return DefineConcept(event_id=f"d{i}", ts=T0, concept_id=c, aspects=ALL, prereqs=prereqs)


def ob(eid, cell, pol, ts, kind="quiz"):
    o = (recall_observation(cell, {"+": "recalled", "~": "fuzzy", "-": "forgot"}[pol], eid)
         if kind == "recall" else
         Observation(cell=cell, polarity=pol, strength=1.0, kind=kind, evidence_ref=eid))
    return Observe(event_id=eid, ts=ts, observation=o)


def log_():
    return [
        define(0, "a"), define(1, "b", ("a",)),
        ob("q1", "a.term", "+", T0 + timedelta(days=27)),   # a choice: FSRS S 1.38 d, band drops after ~37 d
        ob("q2", "b.procedure", "+", T0 + timedelta(days=62)),     # learned in the last week
        ob("q3", "a.notation", "+", T0 + timedelta(days=63)),
        ob("r1", "a.notation", "~", T0 + timedelta(days=64, hours=1), kind="recall"),
        ob("r2", "b.procedure", "+", T0 + timedelta(days=64, hours=2), kind="recall"),
    ]


SRCS = (ReviewSource(page_id="p", section_id="s1", heading="h", cells=("a.term",), cue="提示"),)
NOW = T0 + timedelta(days=65)


def live(events, until):
    k = AspectK(CFG)
    for ev in events:
        if ev.ts <= until:
            k.apply(ev)
    return k


def test_replay_equals_live_state():
    assert replay(log_(), CFG, NOW).to_json() == live(log_(), NOW).to_json()


def test_reviews_between_counts_recall_only():
    assert reviews_between(log_(), T0 + timedelta(days=64), NOW) == {
        "recalled": 1, "fuzzy": 1, "forgot": 0}
    assert reviews_between(log_(), T0, T0 + timedelta(days=64)) == {
        "recalled": 0, "fuzzy": 0, "forgot": 0}


def test_reviews_done_counts_self_grades_and_check_mode_reviews_only():
    log = [*log_(), ob("review:p:s4:rv1", "a.term", "+", NOW - timedelta(hours=1), kind="check"),
           ob("check:p:s4", "a.term", "+", NOW - timedelta(hours=1), kind="check")]
    assert reviews_done(log, T0 + timedelta(days=64), NOW) == 3


def test_digest_today_follows_deadlines():
    plain = digest(log_(), CFG, NOW, sources=SRCS)
    far = {"a": 400.0}            # an exam far ahead: what fades before it is due now
    d = digest(log_(), CFG, NOW, sources=SRCS, deadline_days=far)
    assert len(d.fading) >= len(plain.fading)
    all_fading = fading(replay(log_(), CFG, NOW), NOW, deadline_days=far)
    assert any(f.cell == "a.notation" for f in all_fading)      # no card covers it: nothing he can act on
    assert d.fading == tuple(f for f in all_fading if f.cell in SRCS[0].cells)


def test_weekly_digest_progress_slip_and_fading():
    d = digest(log_(), CFG, NOW, period="week", sources=SRCS)
    assert (d.start, d.end) == (NOW - timedelta(days=7), NOW)
    # b.procedure was learned in the last week, but from no evidence (NONE is not a tier).
    assert "b.procedure" not in {c.cell for c in d.progressed}
    slip = {c.cell: c for c in d.slipped}
    assert slip["a.term"].before is Band.LIKELY and slip["a.term"].after is Band.SHAKY
    assert slip["a.term"].forgot
    assert "a.term" in [f.cell for f in d.fading]
    assert d.reviews == {"recalled": 1, "fuzzy": 1, "forgot": 0}


def test_progress_and_slip_need_a_real_tier_change():
    """No evidence -> any band is not progress; no evidence on either side is not a slip."""
    start = NOW - timedelta(days=7)
    events = [define(0, "a"), define(1, "b", ("a",)),
              ob("g1", "a.purpose", "-", T0), ob("g2", "a.purpose", "-", T0 + timedelta(hours=1)),
              ob("p1", "a.purpose", "+", NOW - timedelta(days=2)),
              ob("p2", "a.purpose", "+", NOW - timedelta(days=2, hours=-1)),
              ob("p3", "a.purpose", "+", NOW - timedelta(days=1)),
              ob("p4", "a.purpose", "+", NOW - timedelta(hours=5)),
              ob("n1", "b.term", "-", NOW - timedelta(days=1))]
    before, after = replay(events, CFG, start), replay(events, CFG, NOW)
    assert before.belief("a.purpose", start).band is Band.GAP
    assert after.belief("a.purpose", NOW).band in (Band.SHAKY, Band.LIKELY, Band.KNOWN)
    assert before.belief("b.term", start).band is Band.NONE
    assert after.belief("b.term", NOW).band is Band.GAP
    d = digest(events, CFG, NOW, period="week")
    assert [c.cell for c in d.progressed] == ["a.purpose"]
    assert d.progressed[0].before is Band.GAP
    assert all(Band.NONE not in (c.before, c.after) for c in d.progressed + d.slipped)
    assert d.slipped == ()


def test_today_matches_schedule_on_live_state():
    events = log_()
    day_start = datetime(2026, 12, 5, 0, 0, tzinfo=UTC)
    d = digest(events, CFG, NOW, sources=SRCS, day_start=day_start)
    expected = schedule(live(events, NOW), NOW, SRCS, already_reviewed=0)
    assert d.today == expected
    assert [i.cell for i in d.today.items] == ["a.term"]


def test_digest_today_ranks_exam_concepts_like_schedule():
    events = log_()
    day_start = datetime(2026, 12, 5, 0, 0, tzinfo=UTC)
    exam = frozenset({"a"})
    d = digest(events, CFG, NOW, sources=SRCS, day_start=day_start, exam_concepts=exam)
    assert d.today == schedule(live(events, NOW), NOW, SRCS, already_reviewed=0,
                               exam_concepts=exam)
    assert [i.exam for i in d.today.items] == [True]


def test_already_reviewed_today_counts_against_cap():
    from evowiki.goals.review import ReviewSettings
    events = log_()
    s = ReviewSettings(max_per_day=2)
    d = digest(events, CFG, NOW, sources=SRCS, settings=s,
               day_start=T0 + timedelta(days=64))      # both recall events are "today"
    assert d.today.items == () and d.today.due >= 1


def test_digest_is_replay_stable():
    events = log_()
    assert digest(events, CFG, NOW, period="week", sources=SRCS) == digest(
        list(events), CFG, NOW, period="week", sources=SRCS)


def test_digest_ignores_future_events():
    events = log_() + [ob("late", "a.term", "+", NOW + timedelta(days=1))]
    assert digest(events, CFG, NOW, sources=SRCS) == digest(log_(), CFG, NOW, sources=SRCS)


def test_naive_as_of_rejected():
    with pytest.raises(ValueError):
        digest(log_(), CFG, datetime(2026, 10, 16))  # noqa: DTZ001


def test_digest_forget_then_miss_slips_as_forgot():
    # 4x recall '+' at once -> FSRS S = 2.3065 (massed answers add nothing); R = 0.6 at 62.25 days.
    events = [define(0, "z")] + [ob(f"q{i}", "z.term", "+", T0, kind="recall") for i in range(4)]
    miss = T0 + timedelta(days=62, hours=12)
    events.append(ob("r1", "z.term", "-", miss, kind="recall"))
    d = digest(events, CFG, miss + timedelta(hours=1))
    slip = {c.cell: c for c in d.slipped}
    assert slip["z.term"].after is Band.SHAKY and slip["z.term"].forgot is True
