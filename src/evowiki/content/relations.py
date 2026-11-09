"""Canonical relation catalog (Stage I-B spec §4.1). A pure function of published content.

A relation is the unordered pair of endpoints of one or more published connection paths.
Perspective segments bind to relations only through reviewed `covers_relation_ids`. Nothing
here reads a user's view V, beliefs or experiment condition (spec §4.6.3).
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evowiki.content.graph import ContentGraph
from evowiki.content.models import PUBLISHABLE

RELATION_CATALOG_VERSION = "relcat-v1"
_PREFIX = "rel:"


class RelationCatalogError(ValueError):
    """Published content declares a relation binding that the catalog cannot support."""


def relation_id(a: str, b: str) -> str:
    if a == b:
        raise ValueError("a relation needs two distinct endpoints")
    x, y = sorted((a, b))
    return f"{_PREFIX}{x}~{y}"


def endpoints_of(rid: str) -> tuple[str, str]:
    if not rid.startswith(_PREFIX) or rid.count("~") != 1:
        raise ValueError(f"not a relation id: {rid!r}")
    a, b = rid[len(_PREFIX):].split("~")
    return a, b


class RelationEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    relation_id: str
    endpoints: tuple[str, str]
    hops: int = Field(ge=1, le=3)
    path: tuple[str, ...]
    connection_segment_ids: tuple[str, ...] = Field(min_length=1)
    perspective_segment_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> "RelationEntry":
        if self.relation_id != relation_id(*self.endpoints) or list(self.endpoints) != sorted(
                self.endpoints):
            raise ValueError(f"{self.relation_id}: endpoints {self.endpoints} do not match")
        if (self.path[0], self.path[-1]) != self.endpoints or len(self.path) - 1 != self.hops:
            raise ValueError(f"{self.relation_id}: path {self.path} does not match endpoints/hops")
        return self

    @property
    def primary_connection_segment_id(self) -> str:
        return self.connection_segment_ids[0]


class RelationCatalog:
    def __init__(self, entries: Iterable[RelationEntry]):
        items = sorted(entries, key=lambda e: e.relation_id)
        self._by_id = {e.relation_id: e for e in items}
        if len(self._by_id) != len(items):
            raise RelationCatalogError("duplicate relation ids")
        seg: dict[str, set[str]] = defaultdict(set)
        for e in items:
            for sid in (*e.connection_segment_ids, *e.perspective_segment_ids):
                seg[sid].add(e.relation_id)
        self._of_segment = {s: tuple(sorted(r)) for s, r in seg.items()}

    @property
    def entries(self) -> tuple[RelationEntry, ...]:
        return tuple(self._by_id.values())

    def ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def get(self, rid: str) -> RelationEntry:
        return self._by_id[rid]

    def for_concept(self, cid: str) -> tuple[RelationEntry, ...]:
        return tuple(e for e in self._by_id.values() if cid in e.endpoints)

    def relations_of_segment(self, sid: str) -> tuple[str, ...]:
        return self._of_segment.get(sid, ())

    def to_json(self) -> dict:
        return {"version": RELATION_CATALOG_VERSION,
                "entries": [e.model_dump(mode="json") for e in self.entries]}


def build_relation_catalog(graph: ContentGraph) -> RelationCatalog:
    groups: dict[str, list] = defaultdict(list)
    for conn in graph.connections:
        groups[relation_id(conn.path[0], conn.path[-1])].append(conn)
    covers: dict[str, set[str]] = defaultdict(set)
    for cid in graph.concept_ids:
        for p in graph.perspectives_for(cid):
            if not p.covers_relation_ids or p.covers_review_status not in PUBLISHABLE:
                continue
            presumed = (set(p.presumes_required) | set(p.presumes_helpful)) - {p.concept_id}
            for rid in p.covers_relation_ids:
                if rid not in groups:
                    raise RelationCatalogError(
                        f"{p.segment_id} covers {rid}, which has no published connection")
                a, b = endpoints_of(rid)
                other = b if a == p.concept_id else a if b == p.concept_id else None
                if other is None or other not in presumed:
                    raise RelationCatalogError(
                        f"{p.segment_id} covers {rid}: it must join {p.concept_id} to a "
                        f"concept the perspective presumes")
                covers[rid].add(p.segment_id)
    entries = []
    for rid, conns in groups.items():
        conns = sorted(conns, key=lambda c: (len(c.path), c.segment_id))
        a, b = endpoints_of(rid)
        primary = tuple(conns[0].path)
        path = primary if primary[0] == a else tuple(reversed(primary))
        entries.append(RelationEntry(
            relation_id=rid, endpoints=(a, b), hops=len(path) - 1, path=path,
            connection_segment_ids=tuple(c.segment_id for c in conns),
            perspective_segment_ids=tuple(sorted(covers[rid]))))
    return RelationCatalog(entries)


def c_test(catalog: RelationCatalog, route: Iterable[str]) -> tuple[str, ...]:
    """Spec §4.4: every relation whose endpoints are both on the learning route. Each catalog
    entry has at least one published connection segment by construction."""
    on_route = set(route)
    return tuple(sorted(e.relation_id for e in catalog.entries if set(e.endpoints) <= on_route))


def _sha(obj) -> str:
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def catalog_hash(catalog: RelationCatalog) -> str:
    return _sha(catalog.to_json())


def c_test_hash(ids: Iterable[str]) -> str:
    return _sha(sorted(ids))
