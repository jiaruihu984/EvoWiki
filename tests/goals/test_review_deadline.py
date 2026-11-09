"""A goal's exam date stretches the review horizon of its concepts (`goals.review`, assistant-actions
goal_deadline); the forgetting model and everything without a deadline are unchanged."""

from datetime import timedelta

from evowiki.goals.review import ReviewSettings, fading, schedule
from tests.goals.test_review import T0, cue_src, k_with, quiz


def test_a_deadline_pulls_reviews_that_would_lapse_before_it():
    k = k_with(("z", ()), ("y", ()))
    quiz(k, "z.term")
    quiz(k, "y.term")
    day5 = T0 + timedelta(days=1)
    assert fading(k, day5) == ()                                  # FSRS S = 2.31 d: not due yet
    due = fading(k, day5, deadline_days={"z": 20.0})
    assert [f.cell for f in due] == ["z.term"]                    # only the goal's concept
    plan = schedule(k, day5, (cue_src("z.term"), cue_src("y.term")),
                    deadline_days={"z": 20.0})
    assert [i.cell for i in plan.items] == ["z.term"]


def test_a_nearer_deadline_than_the_horizon_or_none_changes_nothing():
    k = k_with(("z", ()))
    quiz(k, "z.term")
    day12 = T0 + timedelta(days=1)
    three = ReviewSettings(horizon_days=3.0)
    assert fading(k, day12, three, deadline_days={"z": 1.0}) == fading(k, day12, three)
    assert fading(k, day12, three) != ()                          # due within the 3-day horizon
    assert fading(k, day12, deadline_days={}) == fading(k, day12) == ()
    assert fading(k, day12, deadline_days={"other": 30.0}) == fading(k, day12)
