from collections import defaultdict
from itertools import pairwise

from evowiki.content.graph import ContentGraph
from evowiki.content.models import (
    RENDERABLE_STATUSES,
    Connection,
    Lang,
    Segment,
    SegmentKind,
)


class SegmentIndex:
    """Canonical segments indexed by (concept, kind, lang). Read-only after construction."""

    def __init__(self, segments: list[Segment], connections: list[Connection], graph: ContentGraph):
        known = set(graph.concept_ids)
        self._segments: dict[str, Segment] = {}
        for s in segments:
            if s.id in self._segments:
                raise ValueError(f"duplicate segment id: {s.id}")
            if s.concept_id not in known:
                raise ValueError(f"segment {s.id} references unknown concept: {s.concept_id}")
            self._segments[s.id] = s
        self._connections: dict[str, Connection] = {}
        for c in connections:
            if c.segment_id in self._connections:
                raise ValueError(f"duplicate connection for segment: {c.segment_id}")
            seg = self._segments.get(c.segment_id)
            if seg is None or seg.kind is not SegmentKind.CONNECTION:
                raise ValueError(f"connection {c.segment_id} must reference a connection segment")
            for a, b in pairwise(c.path):
                if a not in known or b not in known or not graph.adjacent(a, b):
                    raise ValueError(f"connection {c.segment_id}: no edge between {a} and {b}")
            self._connections[c.segment_id] = c
        self._by_key: dict[tuple[str, SegmentKind, Lang], list[Segment]] = defaultdict(list)
        for s in sorted(self._segments.values(), key=lambda s: s.id):
            self._by_key[(s.concept_id, s.kind, s.lang)].append(s)

    def segment(self, sid: str) -> Segment:
        return self._segments[sid]

    def renderable(self, cid: str, kind: SegmentKind, lang: Lang) -> list[Segment]:
        """Approved segments in `lang`, sorted by id; falls back to English if none exist."""
        for lg in (lang, Lang.EN):
            found = [
                s for s in self._by_key.get((cid, kind, lg), [])
                if s.review_status in RENDERABLE_STATUSES
            ]
            if found:
                return found
        return []

    def prereq_segment(self, cid: str, lang: Lang) -> Segment | None:
        """The segment used to refresh or link a prerequisite: one_line, else definition."""
        for kind in (SegmentKind.ONE_LINE, SegmentKind.DEFINITION):
            found = self.renderable(cid, kind, lang)
            if found:
                return found[0]
        return None

    def connections_covering(self, a: str, b: str) -> list[Connection]:
        """Approved connections whose path contains both a and b, sorted by segment id."""
        return [
            c for sid, c in sorted(self._connections.items())
            if a in c.path and b in c.path
            and self._segments[sid].review_status in RENDERABLE_STATUSES
        ]

    def connections_touching(self, cid: str) -> list[Connection]:
        """Approved connections whose path contains cid, sorted by segment id."""
        return [
            c for sid, c in sorted(self._connections.items())
            if cid in c.path and self._segments[sid].review_status in RENDERABLE_STATUSES
        ]
