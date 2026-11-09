import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from evowiki.content.io import load_graph
from evowiki.content.models import (
    PUBLISHABLE,
    Concept,
    Connection,
    ContentVersion,
    Edge,
    Lang,
    Passage,
    Perspective,
    QuizItem,
    Segment,
    SegmentKind,
    Source,
)
from evowiki.content.relations import relation_id
from evowiki.pipeline.covers import apply_covers_review
from evowiki.pipeline.edges import cycle_closers, edge_key
from evowiki.pipeline.edgeview import EdgeView
from evowiki.pipeline.perspectives import apply_phase_review
from evowiki.pipeline.presumes import apply_presumes_correction, apply_presumes_revision
from evowiki.pipeline.quiz import apply_quiz_review
from evowiki.pipeline.review import ReviewQueue, ReviewTarget, review_fingerprint


class SnapshotValidationError(ValueError):
    pass


class ContentBundle(BaseModel):
    sources: list[Source] = Field(default_factory=list)
    passages: list[Passage] = Field(default_factory=list)
    concepts: list[Concept] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    perspectives: list[Perspective] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    quiz_items: list[QuizItem] = Field(default_factory=list)


def review_target_for(seg: Segment) -> ReviewTarget:
    if seg.lang is Lang.ZH:
        return ReviewTarget.TRANSLATION
    if seg.kind is SegmentKind.CONNECTION:
        return ReviewTarget.CONNECTION
    return ReviewTarget.SEGMENT


def _resolved(seg: Segment, queue: ReviewQueue) -> Segment | None:
    status = queue.resolve(review_target_for(seg), seg.id, seg.review_status,
                           fingerprint=review_fingerprint(seg))
    if status not in PUBLISHABLE:
        return None
    return seg.model_copy(update={"review_status": status})


def _aligned(zh: Segment, en: Segment) -> bool:
    """A zh segment is a sentence-aligned translation of its en: same sentence count and
    identical per-sentence citations. Anything else translates superseded en content."""
    return [s.citations for s in zh.sentences] == [s.citations for s in en.sentences]


def publishable_bundle(bundle: ContentBundle, queue: ReviewQueue) -> ContentBundle:
    cids = {c.id for c in bundle.concepts}
    edges = []
    for e in bundle.edges:
        status = queue.resolve(ReviewTarget.EDGE, edge_key(e.src, e.dst, e.relation),
                               e.review_status, fingerprint=review_fingerprint(e))
        if status in PUBLISHABLE and e.src in cids and e.dst in cids:
            edges.append(e.model_copy(update={"review_status": status}))
    closing = set(cycle_closers(edges))
    edges = [e for i, e in enumerate(edges) if i not in closing]
    view = EdgeView(edges)

    en: dict[str, Segment] = {}
    for s in bundle.segments:
        if s.lang is Lang.EN and s.concept_id in cids and (r := _resolved(s, queue)):
            en[s.id] = r
    connections = [c for c in bundle.connections if c.segment_id in en
                   and set(c.path) <= cids and view.path_valid(c.path)]
    published_rel = {relation_id(c.path[0], c.path[-1]) for c in connections}
    perspectives = []
    for original in bundle.perspectives:
        p = original
        if (p.segment_id not in en
                or not set(p.presumes_required) | set(p.presumes_helpful) <= cids):
            continue
        p = apply_covers_review(queue, p, en[p.segment_id])
        kept_covers = ([r for r in p.covers_relation_ids if r in published_rel]
                       if p.covers_review_status in PUBLISHABLE else [])
        p = p.model_copy(update={"covers_relation_ids": kept_covers,
                                 "covers_review_status": (p.covers_review_status
                                                          if kept_covers else None)})
        p = apply_phase_review(queue, p, en[p.segment_id])
        if p.framing_phase_status not in PUBLISHABLE:
            p = p.model_copy(update={"framing_phase": None, "framing_phase_status": None})
        # last: a human-approved presumption move (Plan 4b-1d); covers/phase decisions bind to
        # the pipeline's lists, and a move keeps their union, so neither is affected
        p = apply_presumes_revision(queue, original, p, en[p.segment_id])
        p = apply_presumes_correction(queue, original, p, en[p.segment_id], frozenset(cids))
        perspectives.append(p)
    owned = {p.segment_id for p in perspectives} | {c.segment_id for c in connections}
    en = {sid: s for sid, s in en.items()
          if s.kind not in (SegmentKind.PERSPECTIVE, SegmentKind.CONNECTION) or sid in owned}
    zh = {}
    for s in bundle.segments:
        if (s.lang is Lang.ZH and s.translated_from in en
                and _aligned(s, en[s.translated_from]) and (r := _resolved(s, queue))):
            zh[s.id] = r
    segments = [*en.values(), *zh.values()]

    conn_ids = {c.segment_id for c in connections}
    quiz = []
    for q in bundle.quiz_items:
        r = apply_quiz_review(queue, q)
        if (r is None or r.review_status not in PUBLISHABLE
                or (r.core and not r.answer_verified_by_human)
                or not set(r.concept_ids) <= cids
                or (r.tests_connection_id is not None and r.tests_connection_id not in conn_ids)):
            continue
        quiz.append(r)

    cited = ({c for e in edges for c in e.citations} | {c for s in segments for c in s.citations}
             | {c for q in quiz for c in q.citations})
    passages = [p for p in bundle.passages if p.id in cited]
    source_ids = {p.source_id for p in passages}
    sources = [s for s in bundle.sources if s.id in source_ids]
    return ContentBundle(sources=sources, passages=passages, concepts=list(bundle.concepts),
                         edges=edges, segments=segments, perspectives=perspectives,
                         connections=connections, quiz_items=quiz)


def _document(pub: ContentBundle) -> dict[str, Any]:
    def dump(items, key):
        return [i.model_dump(mode="json") for i in sorted(items, key=key)]

    return {
        "sources": dump(pub.sources, lambda s: s.id),
        "passages": dump(pub.passages, lambda p: p.id),
        "concepts": dump(pub.concepts, lambda c: c.id),
        "edges": dump(pub.edges, lambda e: edge_key(e.src, e.dst, e.relation)),
        "segments": dump(pub.segments, lambda s: s.id),
        "perspectives": dump(pub.perspectives, lambda p: p.segment_id),
        "connections": dump(pub.connections, lambda c: c.segment_id),
        "quiz_items": dump(pub.quiz_items, lambda q: q.id),
    }


def snapshot(bundle: ContentBundle, queue: ReviewQueue, versions_dir: Path,
             now: datetime) -> ContentVersion:
    doc = _document(publishable_bundle(bundle, queue))
    digest = hashlib.sha256(
        json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    cv_id = f"cv_{digest}"
    target = Path(versions_dir) / cv_id / "graph.json"
    if target.exists():
        stored = json.loads(target.read_text(encoding="utf-8"))["content_version"]
        return ContentVersion.model_validate(stored)
    cv = ContentVersion(id=cv_id, created_at=now, snapshot_ref=f"versions/{cv_id}/graph.json")
    for key in ("concepts", "segments", "quiz_items"):
        for item in doc[key]:
            item["content_version"] = cv_id
    doc["content_version"] = cv.model_dump(mode="json")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(target)
    try:
        load_graph(target)
    except Exception as exc:
        target.unlink(missing_ok=True)
        if not any(target.parent.iterdir()):
            target.parent.rmdir()
        raise SnapshotValidationError(f"snapshot {cv_id} failed validation: {exc}") from exc
    return cv
