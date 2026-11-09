"""Seeded random aspect-K event streams for the golden (bit-identical default) check.

`golden_streams()` is shared by scripts/rsi/make_aspect_golden.py (which records the outputs) and
tests/belief/test_aspects_infer.py (which replays them). Changing anything here invalidates
tests/belief/data/aspect_golden.json."""

import random
from datetime import UTC, datetime, timedelta

from evowiki.belief.aspects import (
    AspectConfig,
    AspectK,
    AspectRejected,
    Declare,
    DefineConcept,
    Observe,
    RevokeInference,
)
from evowiki.goals.contracts import Aspect, Observation, cell_id

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
N_STREAMS = 40
KINDS = ("quiz", "check", "self", "feedback", "ask", "recall")
POLARITIES = ("+", "-", "~")
DECLARE_VALUES = ("know", "dont", None)


def _stream(seed: int) -> list:
    rng = random.Random(seed)
    aspects = list(Aspect)
    concepts = [f"c{i}" for i in range(rng.randint(3, 6))]
    ts = T0
    events: list = []
    cells: list[str] = []
    for i, c in enumerate(concepts):
        asp = tuple(rng.sample(aspects, rng.randint(1, len(aspects))))
        others = [x for x in concepts if x != c]
        prereqs = tuple(rng.sample(others, rng.randint(0, min(2, len(others)))))
        events.append(DefineConcept(event_id=f"s{seed}-d{i}", ts=ts, concept_id=c, aspects=asp,
                                    prereqs=prereqs, difficulty=rng.randint(1, 5)))
        cells += [cell_id(c, a) for a in asp]
    observes: list[str] = []
    for j in range(rng.randint(30, 80)):
        ts = ts + timedelta(days=rng.uniform(0.0, 20.0))
        eid = f"s{seed}-e{j}"
        r = rng.random()
        if r < 0.80 or not observes:
            kind = rng.choice(KINDS)
            strength = rng.choice((0.0, 0.2, 1.0, round(rng.random(), 3)))
            o = Observation(cell=rng.choice(cells), polarity=rng.choice(POLARITIES),
                            strength=strength, kind=kind, evidence_ref=f"ref{j}",
                            quote="I think I get it" if kind == "self" else None)
            events.append(Observe(event_id=eid, ts=ts, observation=o))
            observes.append(eid)
        elif r < 0.90:
            events.append(Declare(event_id=eid, ts=ts, cell=rng.choice(cells),
                                  value=rng.choice(DECLARE_VALUES)))
        else:
            events.append(RevokeInference(event_id=eid, ts=ts,
                                          source_event_id=rng.choice(observes)))
    return events


def golden_streams() -> list[list]:
    return [_stream(seed) for seed in range(N_STREAMS)]


def replay(config: AspectConfig, events: list) -> tuple[AspectK, list[str]]:
    """Apply every event; rejected ones (e.g. a revoke of a source with no inferred entry, or a
    second revoke of the same source) are skipped and their ids returned."""
    k = AspectK(config)
    rejected: list[str] = []
    for ev in events:
        try:
            k.apply(ev)
        except AspectRejected:
            rejected.append(ev.event_id)
    return k, rejected


def snapshot(k: AspectK, events: list, rejected: list[str]) -> dict:
    last = events[-1].ts
    later = last + timedelta(days=30)
    cells = [cell_id(c, a) for c in k.concept_ids() for a in k.aspects(c)]
    return {
        "rejected": rejected,
        "state": k.to_json(),
        "belief_last": {x: k.belief(x, last).model_dump(mode="json") for x in cells},
        "belief_plus30": {x: k.belief(x, later).model_dump(mode="json") for x in cells},
        "memory": {x: k.memory(x).model_dump(mode="json") for x in cells
                   if k.memory(x).last_tested is not None},
    }
