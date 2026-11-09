"""Synthetic quiz logs for the A1 fitter: outcomes drawn from a known θ_infer (slip = 0.25).

Reuses the aspect-event builders of `tests/teach/scenarios.py` (`_define`, `_Truth`, `_obs`).

Slip is only identifiable where a cell is well known (p ≈ (1 - slip)·q + guess·(1 - q) moves
with slip in proportion to q), so each cell first gets six rounds of `+` self-report and feedback
on an easy concept, and quizzes follow at short gaps (little forgetting). Design check (not used
to pick the seed, which stays 7): with this design the grid MLE of slip over seeds 0–9 at 300
outcomes had sd ≈ 0.014 around 0.25; with one feedback per cell, three aspects and 1–20 h gaps it
was ≈ 0.05 (seed 7 there: 0.13)."""

import random
from datetime import timedelta

from evowiki.goals.contracts import Aspect, cell_id
from evowiki.teach.contracts import InferParams
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.teach.scenarios import T0, _define, _obs, _Truth

TRUE_SLIP = 0.25
ASPECTS = (Aspect.TERM,)
WARMUP_ROUNDS = 6


def quiz_log(n_outcomes: int = 300, *, n_concepts: int = 20, seed: int = 7,
             true: InferParams | None = None):
    """(events, domain_of): per cell WARMUP_ROUNDS × (`+` self, `+` feedback), then quizzes on
    random cells 0.2–2 h apart whose outcomes are drawn from the truth K under `true` (default
    slip = TRUE_SLIP). Exactly `n_outcomes` quiz Observes, in time order."""
    rng = random.Random(seed)
    defs, domain_of, concepts = _define("fq", n_concepts, ASPECTS, 1, rng)
    truth = _Truth(defs, ASPECT_CONFIG_V1.with_infer(true or InferParams(slip=TRUE_SLIP)), rng)
    cells = [cell_id(c, a) for c in concepts for a in ASPECTS]
    events: list = list(defs)
    ts = T0
    for i, cell in enumerate(cells):
        for j in range(WARMUP_ROUNDS):
            for kind in ("self", "feedback"):
                ts += timedelta(minutes=10)
                events.append(truth.add(_obs(f"fq-{kind}{i}-{j}", ts, cell, kind, "+")))
    for n in range(n_outcomes):
        ts += timedelta(hours=rng.uniform(0.2, 2.0))
        events.append(truth.draw(f"fq-q{n}", ts, rng.choice(cells), "quiz", fails=("-", "~")))
    return events, domain_of


def ask_log(n_outcomes: int, *, true: InferParams, n_asks: int = 3, seed: int = 7):
    """(events, domain_of) for A2: cells one after another; each gets `n_asks` questions the user
    asked (`ask`, all `+` or all `-`, at random), then one recall test 0.2–2 h later drawn from the
    truth K under `true`. Every test comes right after its cell's asks, so what the asks say
    matters on the training and on the held-out side alike (all asks up front with several tests
    per cell left the held-out tests mostly on cells the earlier tests had already settled).
    Recall tests have no guess floor, so a wrong reading of the asks costs more log-loss.
    Exactly `n_outcomes` tests, in time order."""
    rng = random.Random(seed)
    defs, domain_of, concepts = _define("aq", n_outcomes, ASPECTS, 1, rng)
    truth = _Truth(defs, ASPECT_CONFIG_V1.with_infer(true), rng)
    events: list = list(defs)
    ts = T0
    for i, cell in enumerate(cell_id(c, a) for c in concepts for a in ASPECTS):
        pol = rng.choice("+-")
        for j in range(n_asks):
            ts += timedelta(minutes=10)
            events.append(truth.add(_obs(f"aq-ask{i}-{j}", ts, cell, "ask", pol)))
        ts += timedelta(hours=rng.uniform(0.2, 2.0))
        events.append(truth.draw(f"aq-r{i}", ts, cell, "recall"))
    return events, domain_of


def fresh_log(n_cells: int = 60, per_cell: int = 4, *, true: InferParams, seed: int = 0):
    """(events, domain_of): each cell is quizzed `per_cell` times 5–60 min apart right after it
    appears (no warm-up), outcomes drawn from the truth K under `true`. The first test of every
    cell is decided by the K's belief before any evidence, so start_known shows."""
    rng = random.Random(f"fresh/{seed}")
    defs, domain_of, concepts = _define("fr", n_cells, ASPECTS, 1, rng)
    truth = _Truth(defs, ASPECT_CONFIG_V1.with_infer(true), rng)
    events: list = list(defs)
    ts = T0
    n = 0
    for c in concepts:
        for _ in range(per_cell):
            ts += timedelta(minutes=rng.uniform(5, 60))
            events.append(truth.draw(f"fr-q{n}", ts, cell_id(c, ASPECTS[0]), "quiz"))
            n += 1
    return events, domain_of
