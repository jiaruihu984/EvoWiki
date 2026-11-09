"""Synthetic content graph and quiz fixtures for isolated tests."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evowiki.content.graph import ContentGraph
from evowiki.content.io import graph_from_document
from evowiki.content.models import Connection, Relation, Segment, SegmentKind, split_tag
from evowiki.content.relations import RelationCatalog, build_relation_catalog
from evowiki.content.segments import SegmentIndex

DEV_WORLD_PATH = Path(__file__).parent / "data" / "dev_world.json"
DEV_SOURCE_ID = "dev-synthetic"
DEV_CONTENT_VERSION = "cv_dev_stage1"
VERIFIED_AT = datetime(2026, 10, 2, tzinfo=UTC)
BASE_KINDS = (SegmentKind.ONE_LINE, SegmentKind.DEFINITION, SegmentKind.INTUITION,
              SegmentKind.DERIVATION, SegmentKind.ADVANCED)
EXTRA_PATHS: tuple[tuple[str, ...], ...] = (("covariance", "pca", "autoencoder"),
                                            ("pca", "svd", "lora"))
# (concept, framing slug, presumes_required, presumes_helpful)
PERSPECTIVES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("pca", "variance_maximization", ("covariance", "eigenvalue"), ("variance",)),
    ("pca", "low_rank_reconstruction", ("svd",), ("matrix",)),
    ("pca", "linear_autoencoder", ("autoencoder", "neural_network"), ("svd",)),
    ("svd", "matrix_factorization", ("matrix",), ("eigenvalue",)),
    ("svd", "spectral_view", ("eigenvalue",), ("pca",)),
    ("autoencoder", "nonlinear_pca", ("pca", "neural_network"), ()),
    ("autoencoder", "compression", ("neural_network",), ()),
    ("lora", "low_rank_update", ("svd",), ("matrix",)),
    ("lora", "parameter_efficient_finetuning", ("neural_network",), ()),
    ("eigenvalue", "invariant_directions", ("linear_transformation",), ("matrix",)),
)

DEV_CONTENT_VERSION_V2 = "cv_dev_stage1b"
V2_CONCEPTS = (
    {"id": "eigendecomposition", "name_en": "Eigendecomposition", "name_zh": "特征分解",
     "domain": "la", "difficulty": 3},
    {"id": "low_rank_approximation", "name_en": "Low-Rank Approximation",
     "name_zh": "低秩近似", "domain": "la", "difficulty": 4},
    {"id": "dimensionality_reduction", "name_en": "Dimensionality Reduction",
     "name_zh": "降维", "domain": "ml", "difficulty": 3},
    {"id": "representation_learning", "name_en": "Representation Learning",
     "name_zh": "表示学习", "domain": "dl", "difficulty": 4},
    {"id": "fine_tuning", "name_en": "Fine-Tuning", "name_zh": "微调", "domain": "dl",
     "difficulty": 3},
)
V2_EDGES = (
    ("eigenvalue", "eigendecomposition", "prerequisite_of", "formal", 0.9),
    ("eigendecomposition", "pca", "prerequisite_of", "formal", 0.9),
    ("eigendecomposition", "svd", "prerequisite_of", "pedagogical", 0.6),
    ("svd", "low_rank_approximation", "prerequisite_of", "formal", 0.9),
    ("dimensionality_reduction", "pca", "prerequisite_of", "pedagogical", 0.7),
    ("neural_network", "representation_learning", "prerequisite_of", "formal", 0.8),
    ("representation_learning", "autoencoder", "related_to", None, 0.7),
    ("low_rank_approximation", "lora", "related_to", None, 0.7),
    ("neural_network", "fine_tuning", "prerequisite_of", "formal", 0.8),
    ("fine_tuning", "lora", "prerequisite_of", "pedagogical", 0.7),
)
V2_ITEMS = tuple(
    {"id": f"q_{cid}_1", "concept_ids": [cid], "kc_tags": [f"{cid}:core"], "difficulty": d,
     "kind": "recall", "answer_verified_by_human": True}
    for cid, d in (("eigendecomposition", 3), ("low_rank_approximation", 4),
                   ("dimensionality_reduction", 3), ("representation_learning", 4),
                   ("fine_tuning", 3)))
V2_EXTRA_PATHS = EXTRA_PATHS + (("pca", "autoencoder", "representation_learning"),
                                ("low_rank_approximation", "svd", "pca"),
                                ("svd", "lora", "fine_tuning"))
# (concept, slug, presumes_required, presumes_helpful, covers)
V2_PERSPECTIVES = tuple(p + ((),) for p in PERSPECTIVES if p[0] != "pca") + (
    ("pca", "covariance_eigendecomposition", ("covariance", "eigendecomposition"), ("variance",),
     ("rel:covariance~pca", "rel:eigendecomposition~pca")),
    ("pca", "variance_preserving_reduction", ("dimensionality_reduction",), (),
     ("rel:dimensionality_reduction~pca",)),
    ("pca", "linear_representation", ("autoencoder", "representation_learning"),
     ("neural_network",), ("rel:autoencoder~pca", "rel:pca~representation_learning")),
    # helpful includes low_rank_approximation so the covers join pca to a concept in A(p).
    ("pca", "low_rank_reconstruction", ("svd",), ("matrix", "low_rank_approximation"),
     ("rel:pca~svd", "rel:low_rank_approximation~pca")),
)
# Reviewed framing phase of the covered PCA perspectives, keyed by (concept, slug); recorded as
# human_approved. Readiness classifies by this label only, never by the domains inside A(p).
V2_PERSPECTIVE_PHASES: dict[tuple[str, str], str] = {
    ("pca", "covariance_eigendecomposition"): "LA",
    ("pca", "variance_preserving_reduction"): "ML",
    ("pca", "linear_representation"): "DL",
    ("pca", "low_rank_reconstruction"): "LA",
}


@dataclass(frozen=True)
class DevContent:
    graph: ContentGraph
    segments: SegmentIndex
    all_segments: tuple[Segment, ...]
    all_connections: tuple[Connection, ...]
    misconception_fix_segments: dict[str, frozenset[str]]
    perspective_concepts: dict[str, frozenset[str]]
    segment_kinds: dict[str, str]
    segment_concepts: dict[str, str]
    relations: RelationCatalog


def _pid(cid: str) -> str:
    return f"dev.{cid}"


def _segment(sid: str, cid: str, kind: SegmentKind, text: str, cites: list[str],
             version: str = DEV_CONTENT_VERSION) -> dict[str, Any]:
    return {
        "id": sid, "concept_id": cid, "kind": kind.value, "lang": "en",
        "review_status": "human_approved",
        "sentences": [{"text": text, "citations": cites}],
        "license_obligations": {"attribution": [DEV_SOURCE_ID], "share_alike": False,
                                "non_commercial": False},
        "content_version": version,
    }


def _build_document(data: dict[str, Any], perspectives: tuple[tuple[Any, ...], ...],
                    extra_paths: tuple[tuple[str, ...], ...], version: str,
                    snapshot_ref: str,
                    phases: Mapping[tuple[str, str], str] | None = None) -> dict[str, Any]:
    """Expand a concepts/edges/quiz_items world into a load_graph-style JSON document.

    Perspective tuples are (concept, slug, required, helpful) in v1 or carry a fifth element
    `covers` (relation ids, recorded as human_approved when non-empty) in v2. `phases` maps
    (concept, slug) to a framing phase, recorded as human_approved.
    """
    names = {c["id"]: c["name_en"] for c in data["concepts"]}
    segments: list[dict[str, Any]] = []
    for cid, name in names.items():
        for kind in BASE_KINDS:
            segments.append(_segment(f"{cid}.{kind.value}.en", cid, kind,
                                     f"[dev placeholder] {kind.value} of {name}.", [_pid(cid)],
                                     version))
        for n in (1, 2):
            segments.append(_segment(f"{cid}.example.{n:02d}.en", cid, SegmentKind.EXAMPLE,
                                     f"[dev placeholder] example {n} of {name}.", [_pid(cid)],
                                     version))
    fixes = []
    tags = sorted({t for q in data["quiz_items"] for t in q.get("misconception_tags", [])})
    for tag in tags:
        parts = split_tag(tag)
        if parts is None:
            raise ValueError(f"malformed misconception tag {tag!r}")
        cid, slug = parts
        sid = f"{cid}.misconception_fix.{slug}.en"
        segments.append(_segment(sid, cid, SegmentKind.MISCONCEPTION_FIX,
                                 f"[dev placeholder] correction of '{slug}' for {names[cid]}.",
                                 [_pid(cid)], version))
        fixes.append({"segment_id": sid, "misconception_tags": [tag]})
    perspective_docs = []
    for entry in perspectives:
        if len(entry) not in (4, 5):
            raise ValueError(f"perspective tuple must have 4 or 5 elements: {entry!r}")
        cid, slug, req, helpful = entry[:4]
        covers: tuple[str, ...] = entry[4] if len(entry) == 5 else ()
        sid = f"{cid}.perspective.{slug}.en"
        label = slug.replace("_", " ")
        segments.append(_segment(sid, cid, SegmentKind.PERSPECTIVE,
                                 f"[dev placeholder] {label} view of {names[cid]}.",
                                 [_pid(x) for x in (cid, *req)], version))
        pdoc: dict[str, Any] = {"segment_id": sid, "concept_id": cid, "framing_label": label,
                                "presumes_required": list(req),
                                "presumes_helpful": list(helpful)}
        if covers:
            pdoc["covers_relation_ids"] = list(covers)
            pdoc["covers_review_status"] = "human_approved"
        if phases and (cid, slug) in phases:
            pdoc["framing_phase"] = phases[(cid, slug)]
            pdoc["framing_phase_status"] = "human_approved"
        perspective_docs.append(pdoc)
    paths = [(e["src"], e["dst"]) for e in data["edges"]
             if e["relation"] != Relation.PART_OF.value] + list(extra_paths)
    connections = []
    for p in paths:
        sid = "conn." + ".".join(p) + ".en"
        text = "[dev placeholder] how " + " relates to ".join(names[x] for x in p) + "."
        segments.append(_segment(sid, p[0], SegmentKind.CONNECTION, text, [_pid(x) for x in p],
                                 version))
        connections.append({"segment_id": sid, "path": list(p)})
    return {
        **data,
        "sources": [{
            "id": DEV_SOURCE_ID, "title": "EvoWiki synthetic development content (not canonical)",
            "locator": "evowiki:dev", "license": "CC-BY-4.0", "derivative_allowed": True,
            "redistribution_allowed": True, "commercial_allowed": True,
            "attribution_required": True, "share_alike": False,
            "license_verified_at": VERIFIED_AT.isoformat(),
        }],
        "passages": [{"id": _pid(cid), "source_id": DEV_SOURCE_ID, "locator": f"dev:{cid}",
                      "text": f"Synthetic development passage about {name}."}
                     for cid, name in names.items()],
        "segments": segments,
        "perspectives": perspective_docs,
        "connections": connections,
        "misconception_fixes": fixes,
        "content_version": {"id": version, "created_at": VERIFIED_AT.isoformat(),
                            "snapshot_ref": snapshot_ref},
    }


def _load_world(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dev_document(path: Path = DEV_WORLD_PATH) -> dict[str, Any]:
    """The synthetic content as a load_graph-style JSON document (a fresh dict on every call)."""
    return _build_document(_load_world(path), PERSPECTIVES, EXTRA_PATHS, DEV_CONTENT_VERSION,
                           "evowiki/experiments/data/dev_world.json")


def dev_document_v2(path: Path = DEV_WORLD_PATH) -> dict[str, Any]:
    """Stage I-B synthetic world: v1 plus the V2 concepts, edges and items, relation covers and
    PCA perspectives with reviewed LA/ML/DL framing phases (a fresh dict on every call)."""
    data = _load_world(path)
    data["concepts"] = [*data["concepts"], *(dict(c) for c in V2_CONCEPTS)]
    edges = []
    for src, dst, relation, kind, strength in V2_EDGES:
        edge: dict[str, Any] = {"src": src, "dst": dst, "relation": relation}
        if kind is not None:
            edge["kind"] = kind
        edge["strength"] = strength
        edges.append(edge)
    data["edges"] = [*data["edges"], *edges]
    data["quiz_items"] = [*data["quiz_items"],
                          *({**q, "concept_ids": list(q["concept_ids"]),
                             "kc_tags": list(q["kc_tags"])} for q in V2_ITEMS)]
    return _build_document(data, V2_PERSPECTIVES, V2_EXTRA_PATHS, DEV_CONTENT_VERSION_V2,
                           "evowiki/experiments/devcontent.py#v2", V2_PERSPECTIVE_PHASES)


def _content_from_document(doc: dict[str, Any]) -> DevContent:
    graph = graph_from_document(doc)
    segs = tuple(graph.segment(s["id"]) for s in doc["segments"])
    conns = tuple(graph.connection(c["segment_id"]) for c in doc["connections"])
    return DevContent(
        graph=graph,
        segments=SegmentIndex(list(segs), list(conns), graph),
        all_segments=segs,
        all_connections=conns,
        misconception_fix_segments={f.segment_id: frozenset(f.misconception_tags)
                                    for f in graph.misconception_fixes()},
        perspective_concepts={
            p.segment_id: frozenset({p.concept_id, *p.presumes_required, *p.presumes_helpful})
            for cid in graph.concept_ids for p in graph.perspectives_for(cid)
        },
        segment_kinds={s.id: s.kind.value for s in segs},
        segment_concepts={s.id: s.concept_id for s in segs},
        relations=build_relation_catalog(graph),
    )


def build_dev_content(path: Path = DEV_WORLD_PATH) -> DevContent:
    return _content_from_document(dev_document(path))


def build_dev_content_v2(path: Path = DEV_WORLD_PATH) -> DevContent:
    return _content_from_document(dev_document_v2(path))
