from datetime import datetime

from evowiki.content.models import (
    PUBLISHABLE,
    Concept,
    Connection,
    ContentVersion,
    Edge,
    Lang,
    Perspective,
    QuizItem,
    Segment,
)
from evowiki.llm.gateway import Gateway
from evowiki.pipeline.config import PipelineConfig
from evowiki.pipeline.connections import connection_targets, generate_connection
from evowiki.pipeline.context import StageContext
from evowiki.pipeline.covers import review_covers
from evowiki.pipeline.edges import (
    EdgeProposalRecord,
    domain_part_of_edges,
    enforce_dag,
    propose_edges,
    reconcile_edges,
)
from evowiki.pipeline.edgeview import EdgeView
from evowiki.pipeline.perspectives import generate_perspectives
from evowiki.pipeline.quiz import generate_quiz
from evowiki.pipeline.report import StageReport
from evowiki.pipeline.review import review_fingerprint
from evowiki.pipeline.segments import generate_base_segments
from evowiki.pipeline.skeleton import DOMAIN_ROOTS, approved_concepts
from evowiki.pipeline.snapshot import ContentBundle, snapshot
from evowiki.pipeline.sources import eligible_sources
from evowiki.pipeline.support import zh_id
from evowiki.pipeline.translate import Glossary, GlossaryEntry, translate_segment
from evowiki.pipeline.workspace import Workspace


class UnknownConceptError(ValueError):
    pass


def _eligible(ws: Workspace):
    sources = eligible_sources(ws.registry())
    ids = {s.id for s in sources}
    return sources, [p for p in ws.load_passages() if p.source_id in ids]


def load_context(ws: Workspace, gateway: Gateway, config: PipelineConfig) -> StageContext:
    sources, passages = _eligible(ws)
    queue = ws.queue()
    concepts = approved_concepts(ws.load("core", Concept), queue)
    return StageContext.build(gateway, queue, concepts, passages, sources,
                              spot_check_fraction=config.spot_check_fraction,
                              retrieval_k=config.retrieval_k)


def run_pipeline(ws: Workspace, gateway: Gateway, config: PipelineConfig, now: datetime, *,
                 concept_ids: list[str] | None = None) -> list[StageReport]:
    ctx = load_context(ws, gateway, config)
    roots = set(DOMAIN_ROOTS.values())
    if concept_ids is None:
        targets = [c for cid, c in sorted(ctx.concepts.items()) if cid not in roots]
    else:
        unknown = [c for c in concept_ids if c not in ctx.concepts]
        if unknown:
            raise UnknownConceptError(f"unknown concept ids: {unknown}")
        targets = [ctx.concepts[c] for c in concept_ids]
    touched = {c.id for c in targets}
    reports: list[StageReport] = []

    # S2 relations
    old = [r for r in ws.load("edge_proposals", EdgeProposalRecord) if r.subject not in touched]
    new: list[EdgeProposalRecord] = []
    for c in targets:
        for role in ("generator", "verifier"):
            recs, rep = propose_edges(ctx, role, c)
            new += recs
            reports.append(rep)
    records = old + new
    ws.save("edge_proposals", records)
    edges, rep = reconcile_edges(ctx, records)
    reports.append(rep)
    edges, rep = enforce_dag(ctx, edges + domain_part_of_edges(ctx.concepts.values()))
    reports.append(rep)
    ws.save("edges", edges)
    view = EdgeView(edges)

    # S3 base segments
    segments = {s.id: s for s in ws.load("segments", Segment)}
    loaded_en = {sid: review_fingerprint(s) for sid, s in segments.items() if s.lang is Lang.EN}
    for c in targets:
        segs, rep = generate_base_segments(ctx, c)
        segments.update({s.id: s for s in segs})
        reports.append(rep)

    # S4 perspectives
    perspectives = {p.segment_id: p for p in ws.load("perspectives", Perspective)}
    for c in targets:
        if c.id in config.perspective_concepts:
            segs, persps, rep = generate_perspectives(ctx, c, view)
            segments.update({s.id: s for s in segs})
            perspectives.update({p.segment_id: p for p in persps})
            reports.append(rep)

    # S5 connections
    connections = {c.segment_id: c for c in ws.load("connections", Connection)}
    paths, invalid = connection_targets(view, config.spine_paths + ws.connection_request_paths())
    reports.append(StageReport(stage="connections", dropped=invalid))
    for path in paths:
        if touched & set(path):
            seg, conn, rep = generate_connection(ctx, path)
            reports.append(rep)
            if seg is not None and conn is not None:
                segments[seg.id] = seg
                connections[conn.segment_id] = conn

    # S5b perspective covers (Stage I-B §4.1): reviewed relation coverage
    for sid, p in list(perspectives.items()):
        if p.concept_id not in touched or sid not in segments:
            continue
        reviewed, rep = review_covers(ctx, p, segments[sid], connections.values(), segments)
        perspectives[sid] = reviewed
        reports.append(rep)

    # S6 quiz
    quiz = {q.id: q for q in ws.load("quiz_items", QuizItem)}
    spine = set(config.demo_spine)
    for c in targets:
        items, rep = generate_quiz(ctx, c, core=c.id in spine)
        quiz.update({q.id: q for q in items})
        reports.append(rep)
    for conn in connections.values():
        if touched & set(conn.path):
            items, rep = generate_quiz(ctx, ctx.concepts[conn.path[0]], core=False,
                                       connection=conn)
            quiz.update({q.id: q for q in items})
            reports.append(rep)

    # zh translation
    glossary = Glossary.from_concepts(ctx.concepts.values(),
                                      extra=ws.load("glossary", GlossaryEntry))
    path_of = {c.segment_id: set(c.path) for c in connections.values()}
    fresh_zh: set[str] = set()
    for seg in sorted(segments.values(), key=lambda s: s.id):
        concerned = path_of.get(seg.id, {seg.concept_id})
        if seg.lang is Lang.EN and seg.review_status in PUBLISHABLE and touched & concerned:
            zh, rep = translate_segment(ctx, seg, glossary)
            reports.append(rep)
            if zh is not None:
                segments[zh.id] = zh
                fresh_zh.add(zh.id)
    for sid, fp in loaded_en.items():
        if (sid in segments and review_fingerprint(segments[sid]) != fp
                and zh_id(sid) not in fresh_zh):
            segments.pop(zh_id(sid), None)  # translation of superseded en content

    ws.save("segments", sorted(segments.values(), key=lambda s: s.id))
    ws.save("perspectives", sorted(perspectives.values(), key=lambda p: p.segment_id))
    ws.save("connections", sorted(connections.values(), key=lambda c: c.segment_id))
    ws.save("quiz_items", sorted(quiz.values(), key=lambda q: q.id))
    return reports


def build_bundle(ws: Workspace, config: PipelineConfig) -> ContentBundle:
    sources, passages = _eligible(ws)
    return ContentBundle(
        sources=sources, passages=passages,
        concepts=approved_concepts(ws.load("core", Concept), ws.queue()),
        edges=ws.load("edges", Edge), segments=ws.load("segments", Segment),
        perspectives=ws.load("perspectives", Perspective),
        connections=ws.load("connections", Connection),
        quiz_items=ws.load("quiz_items", QuizItem))


def snapshot_workspace(ws: Workspace, config: PipelineConfig, now: datetime) -> ContentVersion:
    return snapshot(build_bundle(ws, config), ws.queue(), ws.versions_dir, now)
