from collections.abc import Callable, Iterable
from itertools import combinations, pairwise

import networkx as nx

from evowiki.content.models import (
    PUBLISHABLE,
    Concept,
    Connection,
    ContentVersion,
    Edge,
    Lang,
    MisconceptionFix,
    Passage,
    Perspective,
    PrereqKind,
    QuizItem,
    Relation,
    Segment,
    SegmentKind,
    Source,
    split_tag,
)

_KIND_ORDER = {k: i for i, k in enumerate(SegmentKind)}


def _index[T](items: Iterable[T], key: Callable[[T], str], label: str) -> dict[str, T]:
    out: dict[str, T] = {}
    for item in items:
        k = key(item)
        if k in out:
            raise ValueError(f"duplicate {label}: {k}")
        out[k] = item
    return out


class ContentGraph:
    """Canonical concept graph G. Read-only after construction; holds publishable content only."""

    def __init__(
        self,
        concepts: list[Concept],
        edges: list[Edge],
        quiz_items: list[QuizItem],
        *,
        sources: Iterable[Source] = (),
        passages: Iterable[Passage] = (),
        segments: Iterable[Segment] = (),
        perspectives: Iterable[Perspective] = (),
        connections: Iterable[Connection] = (),
        misconception_fixes: Iterable[MisconceptionFix] = (),
        content_version: ContentVersion | None = None,
    ):
        self._content_version = content_version
        self._concepts = _index(concepts, lambda c: c.id, "concept id")
        self._sources = _index(sources, lambda s: s.id, "source id")
        self._passages = _index(passages, lambda p: p.id, "passage id")
        for p in self._passages.values():
            src = self._sources.get(p.source_id)
            if src is None or not src.derivative_allowed or src.license_verified_at is None:
                raise ValueError(f"passage {p.id} comes from ineligible source {p.source_id}")

        self._edges = list(edges)
        for e in self._edges:
            for cid in (e.src, e.dst):
                if cid not in self._concepts:
                    raise ValueError(f"edge references unknown concept: {cid}")
            if e.review_status not in PUBLISHABLE:
                raise ValueError(f"edge {e.src}->{e.dst} is not publishable ({e.review_status})")
            self._check_citations(f"edge {e.src}->{e.dst}", e.citations)

        self._prereq = nx.DiGraph()
        self._prereq.add_nodes_from(self._concepts)
        self._assoc = nx.Graph()
        self._assoc.add_nodes_from(self._concepts)
        for e in self._edges:
            if e.relation is Relation.PREREQUISITE_OF:
                if self._prereq.has_edge(e.src, e.dst):
                    raise ValueError(f"duplicate prerequisite_of edge: {e.src} -> {e.dst}")
                self._prereq.add_edge(e.src, e.dst, kind=e.kind)
                self._assoc.add_edge(e.src, e.dst)
            elif e.relation is Relation.RELATED_TO:
                self._assoc.add_edge(e.src, e.dst)
        if not nx.is_directed_acyclic_graph(self._prereq):
            raise ValueError("prerequisite_of subgraph contains a cycle")

        self._segments = _index(segments, lambda s: s.id, "segment id")
        for s in self._segments.values():
            self._check_segment(s)
        self._perspectives = _index(perspectives, lambda p: p.segment_id, "perspective segment")
        for p in self._perspectives.values():
            self._check_perspective(p)
        self._connections = _index(connections, lambda c: c.segment_id, "connection segment")
        for c in self._connections.values():
            self._check_connection(c)

        self._items = _index(quiz_items, lambda q: q.id, "quiz item id")
        for q in self._items.values():
            self._check_item(q)

        known_tags = {t for q in self._items.values() for t in q.misconception_tags}
        self._fixes = _index(misconception_fixes, lambda f: f.segment_id, "misconception fix")
        for f in self._fixes.values():
            self._check_fix(f, known_tags)

    # --- validation helpers -------------------------------------------------
    def _check_citations(self, owner: str, citations: Iterable[str]) -> None:
        for pid in citations:
            if pid not in self._passages:
                raise ValueError(f"{owner} cites unknown passage: {pid}")

    def _check_segment(self, s: Segment) -> None:
        if s.concept_id not in self._concepts:
            raise ValueError(f"segment {s.id} references unknown concept: {s.concept_id}")
        if s.review_status not in PUBLISHABLE:
            raise ValueError(f"segment {s.id} is not publishable ({s.review_status})")
        if not s.sentences:
            raise ValueError(f"segment {s.id} has no cited sentences")
        if s.license_obligations is None:
            raise ValueError(f"segment {s.id} lacks license obligations")
        self._check_citations(f"segment {s.id}", s.citations)
        if s.translated_from is not None:
            src = self._segments.get(s.translated_from)
            if (src is None or src.lang is not Lang.EN or src.concept_id != s.concept_id
                    or src.kind is not s.kind):
                raise ValueError(f"segment {s.id} has invalid translated_from {s.translated_from}")

    def _check_perspective(self, p: Perspective) -> None:
        seg = self._segments.get(p.segment_id)
        if seg is None or seg.kind is not SegmentKind.PERSPECTIVE or seg.concept_id != p.concept_id:
            raise ValueError(f"perspective {p.segment_id} must reference a perspective segment "
                             f"of {p.concept_id}")
        for cid in [*p.presumes_required, *p.presumes_helpful]:
            if cid not in self._concepts:
                raise ValueError(f"perspective {p.segment_id} presumes unknown concept: {cid}")

    def _check_connection(self, c: Connection) -> None:
        seg = self._segments.get(c.segment_id)
        if seg is None or seg.kind is not SegmentKind.CONNECTION or seg.concept_id not in c.path:
            raise ValueError(f"connection {c.segment_id} must reference a connection segment "
                             f"on its path")
        for cid in c.path:
            if cid not in self._concepts:
                raise ValueError(f"connection {c.segment_id} references unknown concept: {cid}")
        for a, b in pairwise(c.path):
            if not self._assoc.has_edge(a, b):
                raise ValueError(f"connection {c.segment_id} hop {a}-{b} is not a "
                                 f"prerequisite_of/related_to edge")

    def _check_item(self, q: QuizItem) -> None:
        for cid in q.concept_ids:
            if cid not in self._concepts:
                raise ValueError(f"quiz item {q.id} references unknown concept: {cid}")
        if q.review_status not in PUBLISHABLE:
            raise ValueError(f"quiz item {q.id} is not publishable ({q.review_status})")
        if q.core and not q.answer_verified_by_human:
            raise ValueError(f"core quiz item {q.id} lacks a human-verified answer key")
        self._check_citations(f"quiz item {q.id}", q.citations)
        if q.tests_connection_id is not None and q.tests_connection_id not in self._connections:
            raise ValueError(f"quiz item {q.id} tests unknown connection: {q.tests_connection_id}")

    def _check_fix(self, f: MisconceptionFix, known_tags: set[str]) -> None:
        seg = self._segments.get(f.segment_id)
        if seg is None or seg.kind is not SegmentKind.MISCONCEPTION_FIX:
            raise ValueError(f"fix {f.segment_id} must reference a misconception_fix segment")
        for tag in f.misconception_tags:
            parts = split_tag(tag)
            if parts is None or parts[0] != seg.concept_id:
                raise ValueError(f"fix {f.segment_id}: tag {tag!r} is not owned by "
                                 f"{seg.concept_id}")
            if tag not in known_tags:
                raise ValueError(f"fix {f.segment_id}: unknown misconception tag {tag!r}")

    # --- accessors ------------------------------------------------------------
    @property
    def content_version(self) -> ContentVersion | None:
        return self._content_version

    @property
    def concept_ids(self) -> list[str]:
        return list(self._concepts)

    @property
    def edges(self) -> tuple[Edge, ...]:
        return tuple(self._edges)

    @property
    def item_ids(self) -> list[str]:
        return list(self._items)

    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(self._segments[k] for k in sorted(self._segments))

    @property
    def connections(self) -> tuple[Connection, ...]:
        return tuple(self._connections[k] for k in sorted(self._connections))

    def neighbors(self, cid: str) -> list[str]:
        """Concepts joined to cid by a prerequisite_of or related_to edge (either direction)."""
        return sorted(self._assoc.neighbors(cid))

    def concept(self, cid: str) -> Concept:
        return self._concepts[cid]

    def item(self, item_id: str) -> QuizItem:
        return self._items[item_id]

    def source(self, sid: str) -> Source:
        return self._sources[sid]

    def passage(self, pid: str) -> Passage:
        return self._passages[pid]

    def segment(self, sid: str) -> Segment:
        return self._segments[sid]

    def connection(self, segment_id: str) -> Connection:
        return self._connections[segment_id]

    def segments_for(self, cid: str, lang: Lang = Lang.EN) -> list[Segment]:
        segs = [s for s in self._segments.values() if s.concept_id == cid and s.lang is lang]
        return sorted(segs, key=lambda s: (_KIND_ORDER[s.kind], s.id))

    def perspectives_for(self, cid: str) -> list[Perspective]:
        return sorted((p for p in self._perspectives.values() if p.concept_id == cid),
                      key=lambda p: p.segment_id)

    def connections_touching(self, cid: str) -> list[Connection]:
        return sorted((c for c in self._connections.values() if cid in c.path),
                      key=lambda c: c.segment_id)

    def items_for_concept(self, cid: str) -> list[QuizItem]:
        """Quiz items whose concept_ids include cid, sorted by id."""
        return sorted((q for q in self._items.values() if cid in q.concept_ids), key=lambda q: q.id)

    def adjacent(self, a: str, b: str) -> bool:
        """True iff a prerequisite_of or related_to edge joins a and b (either direction)."""
        return self._assoc.has_edge(a, b)

    def direct_prerequisites(self, cid: str) -> list[tuple[str, PrereqKind]]:
        return sorted(
            ((p, self._prereq.edges[p, cid]["kind"]) for p in self._prereq.predecessors(cid)),
            key=lambda t: t[0],
        )

    def prerequisite_closure(self, cid: str, max_hops: int) -> set[str]:
        lengths = nx.single_source_shortest_path_length(
            self._prereq.reverse(copy=False), cid, cutoff=max_hops
        )
        return {n for n, d in lengths.items() if d > 0}

    def downstream_count(self, cid: str) -> int:
        return len(nx.descendants(self._prereq, cid))

    def derived_minimal_dag(self) -> nx.DiGraph:
        reduced = nx.transitive_reduction(self._prereq)
        reduced.add_nodes_from(self._prereq.nodes(data=True))
        reduced.add_edges_from((u, v, self._prereq.edges[u, v]) for u, v in list(reduced.edges))
        return reduced

    def path_supported(self, members: set[str], max_hops: int) -> bool:
        for a, b in combinations(sorted(members), 2):
            try:
                if nx.shortest_path_length(self._assoc, a, b) > max_hops:
                    return False
            except nx.NetworkXNoPath:
                return False
        return True

    def misconception_fixes(self) -> tuple[MisconceptionFix, ...]:
        return tuple(self._fixes[k] for k in sorted(self._fixes))

    def descendants(self, cid: str) -> set[str]:
        """Concepts reachable from cid over prerequisite_of (its downstream concepts)."""
        return set(nx.descendants(self._prereq, cid))

    def support_paths(
        self, members: Iterable[str], max_hops: int
    ) -> tuple[tuple[str, ...], ...] | None:
        """One shortest prerequisite_of/related_to path per sorted member pair (part_of never
        counts). None when a pair is disconnected or further apart than max_hops."""
        out: list[tuple[str, ...]] = []
        for a, b in combinations(sorted(set(members)), 2):
            try:
                path = nx.shortest_path(self._assoc, a, b)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                return None
            if len(path) - 1 > max_hops:
                return None
            out.append(tuple(path))
        return tuple(out)
