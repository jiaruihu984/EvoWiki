"""Deterministic aspect-event scenarios for channel A (used by Tasks 2, 5, 6, 7).

Each builder returns `(events, domain_of)`: aspect events only (DefineConcept + Observe) and a
concept → domain map over two domains, "d1" and "d2". Seeded random only; no simulator imports.

- `overconfident_self`: the user self-reports `+` on many cells (several times each), then the
  later checks there are mostly fuzzy (≈70% `~`). Fix: `evidence_weight.self` down.
- `forgetting_fast`: a correct check, then recalls 10–40 days apart that are mostly `-`.
  Fix: `forgetting_scale` down.
- `calibrated`: every test outcome is drawn from the default model's own prediction, so no
  θ_infer direction should help (control).

Outcomes that are not the scenario's point are drawn from a "truth" aspect K (`_Truth`), so the
default is calibrated on them and a global move (slip, guess, a test weight) pays for itself
there. Measured margins (seeds 0–4, mean log-loss gain vs the default, single-path moves to the
bounds and half-way): overconfident_self best = self → 0 (≈0.10–0.13; next slip → 0.3 ≈0.06–0.08);
forgetting_fast: forgetting_scale → 0.5 gains ≈0.006–0.015 (seeds 0–2), monotone in the scale,
but it is no longer the best single path: K's memory is FSRS (belief/memory.py, 2026-10-24), whose
power-law curve is flat over 10–40 days and whose per-cell stability already drops on every miss,
so a 0.5–2× stretch moves predicted recall little and slip → 0.3 / start_known → 0.05 explain the
misses as well (≈0.02). Under the old exponential model it was ≈0.06–0.08. calibrated: every
move < 0.01."""

import random
from datetime import UTC, datetime, timedelta

from evowiki.belief.aspects import AspectConfig, AspectK, DefineConcept, Observe
from evowiki.goals.contracts import Aspect, Observation, cell_id
from evowiki.teach.contracts import InferParams
from evowiki.teach.predict import p_correct
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
DOMAINS = ("d1", "d2")
TRUE_FORGETTING_SCALE = 0.05
N_RECALLS = 3


def _obs(eid: str, ts: datetime, cell: str, kind: str, polarity: str,
         strength: float = 1.0) -> Observe:
    return Observe(event_id=eid, ts=ts, observation=Observation(
        cell=cell, polarity=polarity, strength=strength, kind=kind, evidence_ref=f"ref-{eid}",
        quote="我会这个" if kind == "self" else None))


def _define(prefix: str, n: int, aspects: tuple[Aspect, ...], difficulty: int,
            rng: random.Random) -> tuple[list, dict[str, str], list[str]]:
    events, domain_of, concepts = [], {}, []
    for i in range(n):
        c = f"{prefix}{i}"
        concepts.append(c)
        domain_of[c] = DOMAINS[i % 2]
        events.append(DefineConcept(event_id=f"{prefix}-def{i}", ts=T0, concept_id=c,
                                    aspects=aspects, difficulty=difficulty))
    rng.shuffle(concepts)
    return events, domain_of, concepts


class _Truth:
    """A running aspect K under a "true" θ_infer: draws each test outcome from its own p at that
    moment, then applies it (so later draws see earlier outcomes)."""

    def __init__(self, defs: list, config: AspectConfig, rng: random.Random):
        self.k = AspectK(config)
        self.infer, self.rng = config.infer, rng
        for e in defs:
            self.k.apply(e)

    def p(self, cell: str, kind: str, ts: datetime) -> float:
        return p_correct(self.k.belief(cell, ts), kind, self.infer)

    def draw(self, eid: str, ts: datetime, cell: str, kind: str,
             fails: tuple[str, ...] = ("-",)) -> Observe:
        """`+` with the truth's p, else one of `fails` (all label 0 for quiz / check)."""
        pol = "+" if self.rng.random() < self.p(cell, kind, ts) else self.rng.choice(fails)
        return self.add(_obs(eid, ts, cell, kind, pol))

    def add(self, ev: Observe) -> Observe:
        self.k.apply(ev)
        return ev


def overconfident_self(n_concepts: int = 40, seed: int = 0) -> tuple[list, dict[str, str]]:
    """The `term` and `interpret` cells get three self-report `+` each, then two checks that are
    ≈70% `~`. The `purpose` and `mechanism` cells get three `+` feedbacks and checks drawn from
    the default K, so moves that lower p everywhere (slip up, guess down) are not free."""
    rng = random.Random(f"overconfident_self/{seed}")
    told, quiet = (Aspect.TERM, Aspect.INTERPRET), (Aspect.PURPOSE, Aspect.MECHANISM)
    events, domain_of, concepts = _define("oc", n_concepts, (*told, *quiet), 3, rng)
    truth = _Truth(events, ASPECT_CONFIG_V1, rng)
    ts = T0
    n = 0
    for c in concepts:                     # first: confident self-reports / positive feedback
        for a, kind in [(a, "self") for a in told] + [(a, "feedback") for a in quiet]:
            for _ in range(3):
                ts += timedelta(hours=rng.uniform(0.5, 3.0))
                events.append(truth.add(_obs(f"oc-e{n}", ts, cell_id(c, a), kind, "+")))
                n += 1
    ts += timedelta(days=1)
    for c in concepts:                     # later: the checks
        for _ in range(2):
            for a in (*told, *quiet):
                ts += timedelta(hours=rng.uniform(0.5, 3.0))
                if a in told:              # ≈70% fuzzy, the rest correct
                    pol = "~" if rng.random() < 0.7 else "+"
                    events.append(truth.add(_obs(f"oc-c{n}", ts, cell_id(c, a), "check", pol)))
                else:
                    events.append(truth.draw(f"oc-c{n}", ts, cell_id(c, a), "check"))
                n += 1
    return events, domain_of


def _forgets_faster(scale: float) -> AspectConfig:
    """The default config with `forgetting_scale = scale`, which may go below the contract's 0.5
    floor (built without validation: test only). K's FSRS state evolves at the default
    parameters and the scale only stretches the recall curve read from it (belief/memory.py), so
    this is exactly the K a fitted forgetting_scale of `scale` would be."""
    infer = InferParams.model_construct(**{**dict(InferParams()), "forgetting_scale": scale})
    return ASPECT_CONFIG_V1.with_infer(infer)


def forgetting_fast(seed: int = 0, n_concepts: int = 40,
                    true_scale: float = TRUE_FORGETTING_SCALE) -> tuple[list, dict[str, str]]:
    """Per cell: two `+` feedbacks and a check the same day (mostly `+`), then N_RECALLS recalls
    10–40 days apart. Every test is drawn from a K with forgetting_scale `true_scale`
    (it forgets faster than the contract's floor 0.5 allows); a first check does not
    depend on forgetting, so it is calibrated, while the recalls are mostly `-` and the default
    over-predicts them."""
    rng = random.Random(f"forgetting_fast/{seed}")
    aspects = (Aspect.TERM, Aspect.PURPOSE)
    defs, domain_of, concepts = _define("ff", n_concepts, aspects, 2, rng)
    truth = _Truth(defs, _forgets_faster(true_scale), rng)
    plan: list[tuple[datetime, str, str]] = []      # (ts, cell, kind) in time order
    for j, c in enumerate(concepts):
        for a in aspects:
            ts = T0 + timedelta(days=j * 0.5, hours=rng.uniform(0.0, 12.0))
            for kind in ("feedback", "feedback", "check"):
                ts += timedelta(hours=rng.uniform(0.2, 1.0))
                plan.append((ts, cell_id(c, a), kind))
            for _ in range(N_RECALLS):
                ts += timedelta(days=rng.uniform(10.0, 40.0))
                plan.append((ts, cell_id(c, a), "recall"))
    plan.sort()
    events: list = list(defs)
    for n, (ts, cell, kind) in enumerate(plan):
        if kind == "feedback":
            events.append(truth.add(_obs(f"ff-f{n}", ts, cell, "feedback", "+")))
        else:
            events.append(truth.draw(f"ff-t{n}", ts, cell, kind))
    return events, domain_of


def calibrated(seed: int = 0, n_concepts: int = 40, n_events: int = 900
               ) -> tuple[list, dict[str, str]]:
    """Mixed evidence at random times; each test's outcome is drawn from the default K's own p
    at that moment (recall: else `-`; quiz / check: else `-` or `~`)."""
    rng = random.Random(f"calibrated/{seed}")
    aspects = (Aspect.TERM, Aspect.PURPOSE, Aspect.PROCEDURE)
    events, domain_of, concepts = _define("ca", n_concepts, aspects, 3, rng)
    truth = _Truth(events, ASPECT_CONFIG_V1, rng)
    cells = [cell_id(c, a) for c in concepts for a in aspects]
    ts = T0
    for n in range(n_events):
        ts += timedelta(hours=rng.uniform(1.0, 30.0))
        cell = rng.choice(cells)
        r = rng.random()
        kind = ("self" if r < 0.15 else "feedback" if r < 0.2 else "quiz" if r < 0.4
                else "check" if r < 0.7 else "recall")
        eid = f"ca-e{n}"
        if kind == "recall":
            events.append(truth.draw(eid, ts, cell, kind))
        elif kind in ("quiz", "check"):
            events.append(truth.draw(eid, ts, cell, kind, fails=("-", "~")))
        else:
            events.append(truth.add(_obs(eid, ts, cell, kind, rng.choice(("+", "+", "~", "-")))))
    return events, domain_of
