import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from evowiki.belief.beta import Belief, predict_correct
from evowiki.belief.coldstart import prior_beliefs
from evowiki.belief.theta import ThetaInfer
from evowiki.belief.update import apply_evidence
from evowiki.content.graph import ContentGraph
from evowiki.evidence.models import RECALL_TYPES, Evidence


@dataclass
class BeliefState:
    beliefs: dict[str, Belief]
    ambiguous: set[str] = field(default_factory=set)
    applied: int = 0

    def to_json(self) -> str:
        return json.dumps({
            "beliefs": {k: v.model_dump(mode="json") for k, v in sorted(self.beliefs.items())},
            "ambiguous": sorted(self.ambiguous),
            "applied": self.applied,
        })

    @classmethod
    def from_json(cls, s: str) -> "BeliefState":
        d = json.loads(s)
        return cls(
            beliefs={k: Belief.model_validate(v) for k, v in d["beliefs"].items()},
            ambiguous=set(d["ambiguous"]),
            applied=d["applied"],
        )


class Tracer:
    def __init__(self, graph: ContentGraph, theta: ThetaInfer, state: BeliefState):
        self._graph = graph
        self._theta = theta
        self._state = BeliefState(dict(state.beliefs), set(state.ambiguous), state.applied)

    @classmethod
    def from_prior(cls, graph: ContentGraph, theta: ThetaInfer, background: str) -> "Tracer":
        return cls(graph, theta, BeliefState(prior_beliefs(graph, theta, background)))

    @property
    def state(self) -> BeliefState:
        """A copy of the current state; mutating it never affects the tracer."""
        s = self._state
        return BeliefState(dict(s.beliefs), set(s.ambiguous), s.applied)

    def apply(self, ev: Evidence) -> None:
        res = apply_evidence(self._state.beliefs, ev, self._graph, self._theta)
        ambiguous = set(self._state.ambiguous)
        if ev.evidence_type in RECALL_TYPES:
            ambiguous.discard(ev.concept_id)
        ambiguous |= res.ambiguous
        self._state = BeliefState(res.updated, ambiguous, self._state.applied + 1)

    def predict_item(self, item_id: str, now: datetime) -> float:
        item = self._graph.item(item_id)
        return predict_correct([self._state.beliefs[c] for c in item.concept_ids], now, self._theta)


def replay(
    graph: ContentGraph, theta: ThetaInfer, initial: BeliefState, evidence: Iterable[Evidence]
) -> BeliefState:
    tr = Tracer(graph, theta, initial)
    for ev in sorted(evidence, key=lambda e: (e.ts, e.id)):
        tr.apply(ev)
    return tr.state


def replay_in_order(
    graph: ContentGraph, theta: ThetaInfer, initial: BeliefState, evidence: Iterable[Evidence]
) -> BeliefState:
    """Apply evidence in the given (ledger) order. Persisted K uses this: (event seq, ordinal)."""
    tr = Tracer(graph, theta, initial)
    for ev in evidence:
        tr.apply(ev)
    return tr.state
