import json
from collections import defaultdict
from collections.abc import Iterable

import networkx as nx
from pydantic import BaseModel, Field, model_validator

from evowiki.content.models import (
    PUBLISHABLE,
    Concept,
    Edge,
    PrereqKind,
    Relation,
    ReviewStatus,
)
from evowiki.llm.models import LLMRequest, Role
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.linking import mentions
from evowiki.pipeline.report import LenientBatch, StageReport
from evowiki.pipeline.review import ReviewTarget, review_fingerprint, spot_check_sample
from evowiki.pipeline.skeleton import DOMAIN_ROOTS

PURPOSE = "edges_propose"
PROMPT_VERSION = "edges.propose.v1"

SYSTEM = (
    "You propose prerequisite and relatedness edges between concepts of a technical wiki. "
    "A 'formal' prerequisite A->B means A is used to define, derive or compute B and must cite "
    "a passage showing that use. A 'pedagogical' prerequisite means A is normally taught "
    "before B (e.g. textbook order, 'recall that ...'). 'related_to' links concepts that "
    "illuminate each other without dependency. Cite passage ids that mention both concepts. "
    "Output JSON only."
)


class ProposedEdge(BaseModel):
    src: str
    dst: str
    relation: Relation
    kind: PrereqKind | None = None
    strength: float = Field(ge=0.0, le=1.0)
    citations: list[str] = Field(default_factory=list)
    rationale: str

    @model_validator(mode="after")
    def _shape(self) -> "ProposedEdge":
        if self.relation is Relation.PART_OF:
            raise ValueError("proposers may not emit part_of edges")
        if (self.relation is Relation.PREREQUISITE_OF) != (self.kind is not None):
            raise ValueError("kind is required for prerequisite_of and forbidden otherwise")
        return self


class EdgeProposalBatch(LenientBatch):
    edges: list[ProposedEdge]


class EdgeProposalRecord(BaseModel):
    subject: str
    provider_role: str
    edge: ProposedEdge
    llm_call_id: str


def edge_key(src: str, dst: str, relation: Relation) -> str:
    if relation is Relation.RELATED_TO:
        src, dst = sorted((src, dst))
    return f"{src}|{relation.value}|{dst}"


def _prompt(concept: Concept, candidates: list[Concept], passages: list) -> str:
    return (
        f"Target concept: {concept.id} ({concept.name_en}).\n"
        "Propose edges that touch the target: its prerequisites (src=X, dst=target) and "
        "related_to links. Only use these concept ids:\n"
        + "\n".join(f"- {c.id}: {c.name_en}" for c in candidates)
        + "\nFor each edge give src, dst, relation, kind (formal|pedagogical for "
        "prerequisite_of, null otherwise), strength 0-1, citations (passage ids), rationale.\n"
        "Passages:\n" + "\n".join(f"[{p.id}] {p.text}" for p in passages)
    )


def propose_edges(ctx: StageContext, role: Role,
                  concept: Concept) -> tuple[list[EdgeProposalRecord], StageReport]:
    report = ctx.report("edges")
    candidates = sorted(ctx.concepts.values(), key=lambda c: c.id)
    passages = ctx.retrieve([concept])
    request = LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=concept.id,
                         system=SYSTEM, prompt=_prompt(concept, candidates, passages))
    out, call = ctx.gateway.call(role, request, EdgeProposalBatch)
    report.llm_call_ids.append(call.id)
    report.dropped.extend(out.invalid_notes("edges"))
    records = [EdgeProposalRecord(subject=concept.id, provider_role=role, edge=e,
                                  llm_call_id=call.id)
               for e in out.edges if concept.id in (e.src, e.dst)]
    return records, report


def _valid_citations(ctx: StageContext, e: ProposedEdge) -> set[str]:
    a, b = ctx.concepts[e.src], ctx.concepts[e.dst]
    return {pid for pid in e.citations
            if pid in ctx.passages and mentions(ctx.passages[pid].text, a)
            and mentions(ctx.passages[pid].text, b)}


def reconcile_edges(ctx: StageContext,
                    records: Iterable[EdgeProposalRecord]) -> tuple[list[Edge], StageReport]:
    report = ctx.report("edges")
    by_key: dict[str, dict[str, ProposedEdge]] = defaultdict(dict)
    for r in records:
        e = r.edge
        key = edge_key(e.src, e.dst, e.relation)
        if e.src == e.dst:
            report.dropped.append(f"{key}: self-loop")
            continue
        if e.src not in ctx.concepts or e.dst not in ctx.concepts:
            report.dropped.append(f"{key}: unknown concept")
            continue
        by_key[key].setdefault(r.provider_role, e)

    edges: list[Edge] = []
    auto_fps: dict[str, str] = {}
    for key in sorted(by_key):
        props = by_key[key]
        first = next(iter(props.values()))
        cites = sorted(set().union(*(_valid_citations(ctx, p) for p in props.values())))
        kinds = {p.kind for p in props.values()}
        relations = {p.relation for p in props.values()}
        if PrereqKind.FORMAL in kinds and not cites:
            report.dropped.append(f"{key}: formal edge without valid citation")
            continue
        both = len(props) == 2
        reason: str | None = None
        kind = first.kind
        if both and len(relations) == 1 and len(kinds) == 1 and cites:
            status = ReviewStatus.AUTO_VERIFIED
        elif both and len(relations) == 1 and kinds == {PrereqKind.PEDAGOGICAL}:
            status, reason = ReviewStatus.FLAGGED, "pedagogical_agreement_needs_human"
        elif both and relations == {Relation.PREREQUISITE_OF} and len(kinds) == 2:
            status, reason, kind = ReviewStatus.FLAGGED, "kind_disagreement", PrereqKind.PEDAGOGICAL
        else:
            status, reason = ReviewStatus.FLAGGED, "single_provider"
        strength = round(sum(p.strength for p in props.values()) / len(props), 6)
        provenance = "agree:generator+verifier" if both else f"single:{next(iter(props))}"
        edge = Edge(src=first.src, dst=first.dst, relation=first.relation,
                    kind=kind if first.relation is Relation.PREREQUISITE_OF else None,
                    strength=strength, citations=cites, provenance=provenance,
                    review_status=status)
        fp = review_fingerprint(edge)
        if reason is not None:
            ctx.queue.enqueue(ReviewTarget.EDGE, key, reason, payload={
                role: json.loads(p.model_dump_json()) for role, p in props.items()},
                fingerprint=fp)
        resolved = ctx.queue.resolve(ReviewTarget.EDGE, key, status, fingerprint=fp)
        if resolved is None:
            report.dropped.append(f"{key}: rejected by reviewer")
            continue
        edges.append(edge.model_copy(update={"review_status": resolved}))
        (report.flagged if resolved is ReviewStatus.FLAGGED else report.created).append(key)
        if status is ReviewStatus.AUTO_VERIFIED:
            auto_fps[key] = fp
    for key in sorted(spot_check_sample(auto_fps, ctx.spot_check_fraction)):
        ctx.queue.enqueue(ReviewTarget.EDGE, key, "spot_check", spot_check=True,
                          fingerprint=auto_fps[key])
    return edges, report


def cycle_closers(edges: list[Edge]) -> list[int]:
    """Deterministic DAG admission over publishable prerequisite_of edges: admit
    human_approved first, then by descending strength, then edge_key; return the indices
    (in admission order) of the edges that would close a cycle."""
    rank = {ReviewStatus.HUMAN_APPROVED: 0, ReviewStatus.AUTO_VERIFIED: 1}
    order = sorted(
        (i for i, e in enumerate(edges)
         if e.relation is Relation.PREREQUISITE_OF and e.review_status in PUBLISHABLE),
        key=lambda i: (rank[edges[i].review_status], -edges[i].strength,
                       edge_key(edges[i].src, edges[i].dst, edges[i].relation)))
    dag = nx.DiGraph()
    closers = []
    for i in order:
        e = edges[i]
        if e.dst in dag and e.src in dag and nx.has_path(dag, e.dst, e.src):
            closers.append(i)
        else:
            dag.add_edge(e.src, e.dst)
    return closers


def enforce_dag(ctx: StageContext, edges: list[Edge]) -> tuple[list[Edge], StageReport]:
    report = ctx.report("edges")
    out = list(edges)
    for i in cycle_closers(edges):
        e = out[i]
        key = edge_key(e.src, e.dst, e.relation)
        ctx.queue.enqueue(ReviewTarget.EDGE, key, "cycle", fingerprint=review_fingerprint(e))
        out[i] = e.model_copy(update={"review_status": ReviewStatus.FLAGGED})
        report.flagged.append(key)
    return out, report


def domain_part_of_edges(concepts: Iterable[Concept]) -> list[Edge]:
    roots = set(DOMAIN_ROOTS.values())
    return [Edge(src=c.id, dst=DOMAIN_ROOTS[c.domain], relation=Relation.PART_OF,
                 strength=1.0, provenance="rule:domain", review_status=ReviewStatus.AUTO_VERIFIED)
            for c in sorted(concepts, key=lambda c: c.id)
            if c.id not in roots and c.domain in DOMAIN_ROOTS]
