"""Memory of one K cell: FSRS-6 through py-fsrs (MIT, Open Spaced Repetition,
https://github.com/open-spaced-repetition/py-fsrs). User ruling 2026-10-24: stability, recall and
review scheduling follow FSRS; Beta mastery, aspects, inference, claims and the relearning cap do
not change. Pure and deterministic: no clock, no randomness.

- State per cell: FSRS stability (days until recall falls to 0.9) and FSRS difficulty (1..10),
  both None until the first counted test, plus the last test time (kept by `AspectK`).
- `review`: one graded test through py-fsrs' `Scheduler.review_card`, default FSRS-6 parameters,
  no learning / relearning steps (a cell is a Review card from its first test on), no fuzzing
  (fuzz only moves the due date, which K does not use). Grades: `+` Good, `~` Hard, `-` Again.
  FSRS itself decides the spacing effect: a second answer the same day goes through the
  short-term formula (Good / Hard never lower stability there and barely raise it, Again does),
  a spaced one through the long-term recall / forget stability.
- Initial state: FSRS's own, by the first grade (Good 2.31 d, Hard 1.29 d, Again 0.21 d). py-fsrs
  has no way to start a card from a custom stability without inventing a previous review, so the
  concept difficulty prior (`Forgetting.S0_by_difficulty`) no longer enters K.
- `retrievability`: FSRS-6's power forgetting curve R(t) = (1 + F·t/S)^-decay, evaluated here on
  fractional days (py-fsrs' own helper floors to whole days; its review update still uses whole
  days, as FSRS is fitted on). `scale` is θ_infer's `forgetting_scale`: it stretches the curve,
  R(t / scale) — the same as an effective stability S·scale, the meaning the knob had before.
  The stored FSRS state is unscaled (FSRS updates at its default parameters)."""

from datetime import datetime

from fsrs import Card, Rating, Scheduler, State

SCHEDULER = Scheduler(learning_steps=(), relearning_steps=(), enable_fuzzing=False)
DECAY = SCHEDULER.parameters[20]                 # w20 > 0; R = (1 + F·t/S) ** -DECAY
FACTOR = 0.9 ** (-1.0 / DECAY) - 1.0             # so that R(S) = 0.9
RATING: dict[str, Rating] = {"+": Rating.Good, "~": Rating.Hard, "-": Rating.Again}


def review(stability: float | None, difficulty: float | None, last: datetime | None,
           polarity: str, ts: datetime) -> tuple[float, float]:
    """(stability, difficulty) after one graded test at `ts` (aware). A never-tested cell
    (`stability` None) starts from FSRS's initial state for the grade."""
    if stability is None:
        card = Card(card_id=0, due=ts)                       # Learning, no steps: one review
    else:
        card = Card(card_id=0, state=State.Review, stability=stability, difficulty=difficulty,
                    due=ts, last_review=last)
    out, _ = SCHEDULER.review_card(card, RATING[polarity], ts)
    return float(out.stability), float(out.difficulty)


def retrievability(stability: float, elapsed_days: float, scale: float = 1.0) -> float:
    """FSRS-6 recall probability `elapsed_days` after the last test (fractional days)."""
    return (1.0 + FACTOR * max(0.0, elapsed_days) / (stability * scale)) ** -DECAY


def days_to(stability: float, target: float, scale: float = 1.0) -> float:
    """Days after the last test at which recall falls to `target` (0 < target < 1)."""
    return stability * scale / FACTOR * (target ** (-1.0 / DECAY) - 1.0)
