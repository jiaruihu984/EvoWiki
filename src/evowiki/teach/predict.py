"""Prequential (one-step-ahead) prediction of test outcomes under a θ_infer,u candidate.

`predict_outcomes` replays the event log through an aspect K built from `base.with_infer(infer)`.
Before it applies each target it records the K's prediction from the belief at that event's
time; only then does it apply the event. A prediction therefore never sees the event it predicts
or anything after it.

Targets (controller ruling R3): an `Observe` of kind quiz / check / recall whose θ-free weight
`effective_weight(o)` (no θ_infer factor, no slip / guess factor) is >= MIN_TEST_WEIGHT, on a cell
that is not declared at that moment. The set depends on neither the candidate θ nor the event's
own outcome, so every candidate predicts exactly the same event ids and comparisons pair 1:1.
K's own counted-test rule (`_observe`, which does use θ and polarity) is unchanged.

A test on a cell with an open wrong-answer claim (`Claim`, not yet resolved) gets prior_kind
"slip_claim" / "learned_claim" (the latest open one): that is the confirmation question, and its
outcome is what calibrates the claim's trust level.

The base `AspectConfig` is a parameter (the pinned one lives in wikiapp, which teach must not
import)."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import get_args

from evowiki.belief.aspects import (
    BASE_GUESS,
    MAX_GUESS,
    MIN_TEST_WEIGHT,
    RECALL_KINDS,
    AspectConfig,
    AspectEvent,
    AspectK,
    AspectRejected,
    Claim,
    Observe,
    RevokeInference,
    effective_weight,
)
from evowiki.goals.contracts import CellBelief, split_cell
from evowiki.schema import days_between
from evowiki.teach.contracts import EvidenceKind, InferParams

OTHER_DOMAIN = "other"
P_MIN, P_MAX = 0.02, 0.98
_KIND_ORDER = get_args(EvidenceKind)     # tie-break order for prior_kind


@dataclass(frozen=True)
class Outcome:
    event_id: str
    ts: datetime
    cell: str
    concept: str
    kind: str              # quiz | check | recall
    y: float               # + → 1; quiz/check ~ or - → 0; recall ~ → 0.5, - → 0
    p: float               # predicted P(correct), clipped to [P_MIN, P_MAX]
    domain: str
    prior_kind: str        # open claim kind, else dominant direct-evidence kind, else inferred / none
    days_since_test: float | None
    direct: float


def label(kind: str, polarity: str) -> float:
    if polarity == "+":
        return 1.0
    if kind == "recall" and polarity == "~":
        return 0.5
    return 0.0


def p_correct(b: CellBelief, kind: str, infer: InferParams) -> float:
    """P(correct) for a test of `kind` on a cell with belief `b`, clipped to [P_MIN, P_MAX]."""
    if kind not in RECALL_KINDS:
        raise ValueError(f"{kind!r} is not a test kind")
    q = b.m * b.recall
    if kind == "recall":
        p = (1.0 - infer.slip) * q
    else:
        g = min(MAX_GUESS, infer.guess_scale * BASE_GUESS)
        p = (1.0 - infer.slip) * q + g * (1.0 - q)
    return min(P_MAX, max(P_MIN, p))


def _prior_kind(b: CellBelief, sums: Mapping[str, float]) -> str:
    if b.direct > 0.0:
        best = max(_KIND_ORDER, key=lambda k: (sums.get(k, 0.0), -_KIND_ORDER.index(k)))
        if sums.get(best, 0.0) > 0.0:
            return best
    if b.inferred_pos + b.inferred_neg > 0.0:
        return "inferred"
    return "none"


def predict_outcomes(events: Sequence[AspectEvent], base: AspectConfig, infer: InferParams,
                     domain_of: Mapping[str, str]) -> list[Outcome]:
    """One Outcome per counted test, in log order. Events are applied in the given order.

    The first RevokeInference of an earlier Observe that the candidate rejects (its source added
    no inferred evidence under this θ_infer, e.g. inference_strength 0) is a no-op and skipped;
    every other rejection raises AspectRejected (a second revoke of the same source, a revoke of
    a non-Observe, a repeated event id, an Observe on an unknown cell): the log must replay
    cleanly."""
    k = AspectK(base.with_infer(infer))
    sums: dict[str, dict[str, float]] = defaultdict(dict)    # cell -> kind -> direct weight
    seen: set[str] = set()
    observes: set[str] = set()
    revoked: set[str] = set()
    claim_kind: dict[str, str] = {}                           # claim event id -> slip_claim / …
    out: list[Outcome] = []
    for ev in events:
        if ev.event_id in seen:
            raise AspectRejected(f"event {ev.event_id} already applied")
        seen.add(ev.event_id)
        if isinstance(ev, Observe):
            o = ev.observation
            try:
                k.memory(o.cell)
            except KeyError:
                raise AspectRejected(f"cell {o.cell} is not defined") from None
            if o.kind in RECALL_KINDS and effective_weight(o) >= MIN_TEST_WEIGHT:
                b = k.belief(o.cell, ev.ts)
                if b.declared is None:
                    last = k.memory(o.cell).last_tested
                    out.append(Outcome(
                        event_id=ev.event_id, ts=ev.ts, cell=o.cell,
                        concept=split_cell(o.cell)[0], kind=o.kind,
                        y=label(o.kind, o.polarity), p=p_correct(b, o.kind, infer),
                        domain=domain_of.get(split_cell(o.cell)[0], OTHER_DOMAIN),
                        prior_kind=next(
                            (claim_kind[c] for c in reversed(k.open_claims(o.cell))),
                            _prior_kind(b, sums[o.cell])),
                        days_since_test=None if last is None else days_between(last, ev.ts),
                        direct=b.direct))
            k.apply(ev)
            observes.add(ev.event_id)
            w = effective_weight(o, infer)
            sums[o.cell][o.kind] = sums[o.cell].get(o.kind, 0.0) + w
            continue
        if isinstance(ev, Claim):
            claim_kind[ev.event_id] = f"{ev.claim}_claim"
        first_revoke = (isinstance(ev, RevokeInference) and ev.source_event_id in observes
                        and ev.source_event_id not in revoked)
        if isinstance(ev, RevokeInference):
            revoked.add(ev.source_event_id)
        try:
            k.apply(ev)
        except AspectRejected:
            if not first_revoke:
                raise
    return out
