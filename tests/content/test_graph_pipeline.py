import json
from datetime import UTC, datetime

import pytest

from evowiki.content.graph import ContentGraph
from evowiki.content.io import load_graph
from evowiki.content.models import (
    LICENSE_TERMS,
    CitedSentence,
    Concept,
    Connection,
    ContentVersion,
    Edge,
    Lang,
    LicenseObligations,
    Passage,
    Perspective,
    PrereqKind,
    QuizItem,
    Relation,
    ReviewStatus,
    Segment,
    SegmentKind,
    Source,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
OBL = LicenseObligations(attribution=["src"], share_alike=True, non_commercial=False)


def _source(**over) -> Source:
    base = dict(id="src", title="T", locator="https://x", license="CC-BY-SA-4.0",
                license_verified_at=NOW, **LICENSE_TERMS["CC-BY-SA-4.0"].model_dump())
    base.update(over)
    return Source(**base)


def _c(cid: str, domain: str = "la") -> Concept:
    return Concept(id=cid, name_en=cid, name_zh=cid, domain=domain, difficulty=2)


def _seg(sid, cid, kind, lang=Lang.EN, status=ReviewStatus.AUTO_VERIFIED, cites=("p1",),
         translated_from=None) -> Segment:
    return Segment(id=sid, concept_id=cid, kind=kind, lang=lang,
                   sentences=[CitedSentence(text="x.", citations=list(cites))],
                   license_obligations=OBL, review_status=status, translated_from=translated_from)


def _bundle(**over) -> dict:
    b = {
        "concepts": [_c("variance"), _c("eigenvalue"), _c("pca", "ml"), _c("ml_root", "ml")],
        "edges": [
            Edge(src="eigenvalue", dst="pca", relation=Relation.PREREQUISITE_OF,
                 kind=PrereqKind.FORMAL, strength=0.9, citations=["p1"],
                 review_status=ReviewStatus.AUTO_VERIFIED),
            Edge(src="variance", dst="pca", relation=Relation.RELATED_TO, strength=0.6),
            Edge(src="pca", dst="ml_root", relation=Relation.PART_OF, strength=1.0),
        ],
        "quiz_items": [QuizItem(id="q1", concept_ids=["pca"], difficulty=3, kind="recall",
                                stem="s", answer_key="k", core=True,
                                answer_verified_by_human=True, citations=["p1"])],
        "sources": [_source()],
        "passages": [Passage(id="p1", source_id="src", locator="u#c0", text="PCA uses eigenvalues.")],
        "segments": [
            _seg("pca.definition.en", "pca", SegmentKind.DEFINITION),
            _seg("pca.one_line.en", "pca", SegmentKind.ONE_LINE),
            _seg("pca.definition.zh", "pca", SegmentKind.DEFINITION, lang=Lang.ZH,
                 translated_from="pca.definition.en"),
            _seg("pca.perspective.variance.en", "pca", SegmentKind.PERSPECTIVE),
            _seg("conn.variance.pca.en", "variance", SegmentKind.CONNECTION),
        ],
        "perspectives": [Perspective(segment_id="pca.perspective.variance.en", concept_id="pca",
                                     framing_label="variance maximization",
                                     presumes_required=["variance"], presumes_helpful=[])],
        "connections": [Connection(segment_id="conn.variance.pca.en", path=["variance", "pca"])],
        "content_version": ContentVersion(id="cv_1", created_at=NOW, snapshot_ref="r"),
    }
    b.update(over)
    return b


def _build(**over) -> ContentGraph:
    b = _bundle(**over)
    return ContentGraph(b.pop("concepts"), b.pop("edges"), b.pop("quiz_items"), **b)


def test_full_bundle_builds_and_accessors_work():
    g = _build()
    assert g.content_version.id == "cv_1"
    assert [s.id for s in g.segments_for("pca")] == [
        "pca.one_line.en", "pca.definition.en", "pca.perspective.variance.en"]
    assert [s.id for s in g.segments_for("pca", Lang.ZH)] == ["pca.definition.zh"]
    assert g.perspectives_for("pca")[0].presumes_required == ["variance"]
    assert [c.segment_id for c in g.connections_touching("pca")] == ["conn.variance.pca.en"]
    assert g.passage("p1").source_id == "src" and g.source("src").share_alike
    assert len(g.edges) == 3


def test_seed_graph_still_loads(graph):
    assert graph.content_version is None
    assert "pca" in graph.concept_ids


@pytest.mark.parametrize("bad_source", [
    {"license_verified_at": None},
    {"license": "arXiv-nonexclusive-1.0",
     **LICENSE_TERMS["arXiv-nonexclusive-1.0"].model_dump()},
])
def test_passages_require_eligible_verified_source(bad_source):
    with pytest.raises(ValueError, match="ineligible source"):
        _build(sources=[_source(**bad_source)])


def test_unknown_citation_rejected():
    segs = [_seg("pca.definition.en", "pca", SegmentKind.DEFINITION, cites=("p404",))]
    with pytest.raises(ValueError, match="unknown passage: p404"):
        _build(segments=segs, perspectives=[], connections=[])


def test_flagged_segment_and_edge_rejected():
    segs = [_seg("pca.definition.en", "pca", SegmentKind.DEFINITION,
                 status=ReviewStatus.FLAGGED)]
    with pytest.raises(ValueError, match="not publishable"):
        _build(segments=segs, perspectives=[], connections=[])
    edges = [Edge(src="eigenvalue", dst="pca", relation=Relation.PREREQUISITE_OF,
                  kind=PrereqKind.FORMAL, strength=0.9, review_status=ReviewStatus.FLAGGED)]
    with pytest.raises(ValueError, match="not publishable"):
        _build(edges=edges, connections=[])


def test_core_quiz_item_needs_human_verified_answer():
    q = QuizItem(id="q1", concept_ids=["pca"], difficulty=3, kind="recall", core=True,
                 answer_verified_by_human=False)
    with pytest.raises(ValueError, match="human-verified"):
        _build(quiz_items=[q])


def test_connection_hop_must_be_prereq_or_related_edge():
    segs = [_seg("conn.pca.ml_root.en", "pca", SegmentKind.CONNECTION)]
    conns = [Connection(segment_id="conn.pca.ml_root.en", path=["pca", "ml_root"])]
    with pytest.raises(ValueError, match="hop"):
        _build(segments=segs, perspectives=[], connections=conns)


def test_perspective_must_reference_known_concepts():
    p = Perspective(segment_id="pca.perspective.variance.en", concept_id="pca",
                    framing_label="f", presumes_required=["nope"])
    with pytest.raises(ValueError, match="unknown concept"):
        _build(perspectives=[p])


def test_zh_segment_must_translate_matching_en_segment():
    segs = [_seg("pca.definition.en", "pca", SegmentKind.DEFINITION),
            _seg("pca.one_line.zh", "pca", SegmentKind.ONE_LINE, lang=Lang.ZH,
                 translated_from="pca.definition.en")]
    with pytest.raises(ValueError, match="translated_from"):
        _build(segments=segs, perspectives=[], connections=[])


def test_quiz_tests_connection_must_exist():
    q = QuizItem(id="q2", concept_ids=["pca"], difficulty=3, kind="transfer",
                 tests_connection_id="conn.nope.en")
    with pytest.raises(ValueError, match="unknown connection"):
        _build(quiz_items=[q])


def test_minimal_dag_keeps_kind(graph):
    dag = graph.derived_minimal_dag()
    assert dag.edges["eigenvalue", "pca"]["kind"] is PrereqKind.FORMAL


def test_load_graph_reads_optional_keys(tmp_path):
    b = _bundle()
    doc = {k: ([m.model_dump(mode="json") for m in v] if isinstance(v, list)
               else v.model_dump(mode="json")) for k, v in b.items()}
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    g = load_graph(path)
    assert g.segment("pca.definition.zh").translated_from == "pca.definition.en"
    assert g.content_version.id == "cv_1"
