"""Keep K (the aspect log) in step with the node graph (plan §0 "K prereqs"): every node is a K
concept, so any node can hold direct evidence, and every VERIFIED `prerequisite` relation src→dst
is a K prereq of dst (relation layers, user ruling 2026-10-14: only a verified prerequisite feeds
knowledge inference; an unverified one or a `recommended` edge is never mirrored). Prereqs
mirrored before this rule stay in K; K inference filters them at replay (`AspectK(trusted=…)`).
The mirror is not what makes inference follow a prereq: every replay follows the CURRENT verified
set for all events (`AspectK(trusted=…)`), so evidence recorded before a verification propagates
through the newly verified prerequisite too (retroactive, user ruling 2026-10-24).

Both are reached with `DefineConcept` only (`belief.aspects`): defining a concept K does not have
yet creates it (default aspects term + purpose, or the caller's choice per node); redefining one K
already has unions its prereqs, so a node K knows is only ever redefined to add missing prereqs
(with its own current aspects, which the union leaves unchanged).

Documented limit: K can NOT remove a prereq. A prerequisite relation dropped from the graph (or an
undone split) stays in K; the graph stays authoritative for map, diagnosis and path, K only uses it
for inference. Pure: no IO."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol

from evowiki.belief.aspects import DefineConcept
from evowiki.goals.contracts import Aspect
from evowiki.nodes.model import NodeGraph

DEFAULT_ASPECTS: tuple[Aspect, ...] = (Aspect.TERM, Aspect.PURPOSE)


class KSyncError(ValueError):
    """A node that cannot be a K concept (its id contains '.', the cell separator)."""


class KnownConcepts(Protocol):
    """What ksync reads from K (`belief.aspects.AspectK` satisfies it)."""

    def concept_ids(self) -> tuple[str, ...]: ...

    def aspects(self, concept_id: str) -> tuple[Aspect, ...]: ...

    def prereqs(self, concept_id: str) -> tuple[str, ...]: ...


def check_ids(graph: NodeGraph) -> None:
    """KSyncError for a node id K cannot hold (K cell ids are `<concept>.<aspect>`)."""
    bad = [r.id for r in graph.nodes if "." in r.id]
    if bad:
        raise KSyncError(f"node ids may not contain '.' (they are K concept ids): {bad}")


def ksync_events(
    graph: NodeGraph,
    k: KnownConcepts,
    *,
    ts: datetime,
    new_id: Callable[[str], str],
    aspects: Mapping[str, tuple[Aspect, ...]] | None = None,
) -> list[DefineConcept]:
    """The `DefineConcept` events (node id order) that make every node a K concept and every
    verified prerequisite relation a K prereq. `aspects`: a new node's aspects (else DEFAULT_ASPECTS);
    ignored for nodes K already has. Empty when K is already in step."""
    check_ids(graph)
    known = set(k.concept_ids())
    out: list[DefineConcept] = []
    for rec in graph.nodes:
        need = tuple(dict.fromkeys(graph.verified_prereqs(rec.id)))
        if rec.id not in known:
            chosen = (aspects or {}).get(rec.id) or DEFAULT_ASPECTS
            out.append(DefineConcept(event_id=new_id("aev"), ts=ts, concept_id=rec.id,
                                     aspects=chosen, prereqs=need))
            continue
        have = set(k.prereqs(rec.id))
        missing = tuple(p for p in need if p not in have)
        if missing:
            out.append(DefineConcept(event_id=new_id("aev"), ts=ts, concept_id=rec.id,
                                     aspects=k.aspects(rec.id), prereqs=missing))
    return out


def in_step(graph: NodeGraph, k: KnownConcepts) -> bool:
    """Every node is a K concept and every verified prerequisite relation is a K prereq."""
    known = set(k.concept_ids())
    return all(r.id in known for r in graph.nodes) and all(
        r.src in k.prereqs(r.dst) for r in graph.prereq_edges() if r.verified)
