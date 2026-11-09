import json

import networkx as nx
import pytest

from evowiki.content.io import load_graph
from evowiki.content.models import (
    CitedSentence,
    Connection,
    Edge,
    Lang,
    LicenseObligations,
    Perspective,
    PrereqKind,
    QuizItem,
    Relation,
    ReviewStatus,
    Segment,
    SegmentKind,
)
from evowiki.pipeline.covers import covers_fingerprint
from evowiki.pipeline.edges import enforce_dag
from evowiki.pipeline.perspectives import PHASE_REASON, phase_fingerprint
from evowiki.pipeline.review import (
    ReviewQueue,
    ReviewState,
    ReviewTarget,
    review_fingerprint,
)
from evowiki.pipeline.snapshot import ContentBundle, publishable_bundle, snapshot
from tests.pipeline.helpers import FIXTURE_SOURCE, MINI_CONCEPTS, MINI_PASSAGES, NOW

A, F, H = ReviewStatus.AUTO_VERIFIED, ReviewStatus.FLAGGED, ReviewStatus.HUMAN_APPROVED
OBL = LicenseObligations(attribution=["fx"], share_alike=False, non_commercial=False)


def _seg(sid, cid, kind, cite, status=A, lang=Lang.EN, tf=None):
    return Segment(id=sid, concept_id=cid, kind=kind, lang=lang,
                   sentences=[CitedSentence(text="x.", citations=[cite])],
                   license_obligations=OBL, review_status=status, translated_from=tf)


def _bundle() -> tuple[ContentBundle, ReviewQueue]:
    q = ReviewQueue()
    lora_item = q.enqueue(ReviewTarget.EDGE, "svd|prerequisite_of|lora", "single_provider")
    q.decide(lora_item.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    q.enqueue(ReviewTarget.SEGMENT, "pca.one_line.en", "unsupported_sentences")
    core_item = q.enqueue(ReviewTarget.QUIZ_ANSWER, "q_core_ok", "core_answer_key")
    q.decide(core_item.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    q.enqueue(ReviewTarget.QUIZ_ANSWER, "q_core_open", "core_answer_key")
    pre = Relation.PREREQUISITE_OF
    edges = [
        Edge(src="eigenvalue", dst="pca", relation=pre, kind=PrereqKind.FORMAL, strength=0.9,
             citations=["p_pca1"], review_status=A),
        Edge(src="covariance", dst="pca", relation=pre, kind=PrereqKind.PEDAGOGICAL,
             strength=0.6, review_status=F),
        Edge(src="pca", dst="svd", relation=Relation.RELATED_TO, strength=0.8,
             citations=["p_svd"], review_status=H),
        Edge(src="svd", dst="lora", relation=pre, kind=PrereqKind.PEDAGOGICAL, strength=0.7,
             review_status=F),
        Edge(src="pca", dst="machine_learning", relation=Relation.PART_OF, strength=1.0,
             review_status=A),
    ]
    segments = [
        _seg("pca.definition.en", "pca", SegmentKind.DEFINITION, "p_pca1"),
        _seg("pca.one_line.en", "pca", SegmentKind.ONE_LINE, "p_pca2", status=F),
        _seg("pca.definition.zh", "pca", SegmentKind.DEFINITION, "p_pca1", lang=Lang.ZH,
             tf="pca.definition.en"),
        _seg("pca.one_line.zh", "pca", SegmentKind.ONE_LINE, "p_pca2", lang=Lang.ZH,
             tf="pca.one_line.en"),
        _seg("pca.perspective.variance.en", "pca", SegmentKind.PERSPECTIVE, "p_pca2"),
        _seg("conn.pca.svd.en", "pca", SegmentKind.CONNECTION, "p_svd"),
        _seg("conn.vector.lora.en", "vector", SegmentKind.CONNECTION, "p_vec"),
    ]
    perspectives = [Perspective(segment_id="pca.perspective.variance.en", concept_id="pca",
                                framing_label="variance", presumes_required=["variance"])]
    connections = [Connection(segment_id="conn.pca.svd.en", path=["pca", "svd"]),
                   Connection(segment_id="conn.vector.lora.en", path=["vector", "lora"])]
    quiz = [
        QuizItem(id="q_core_ok", concept_ids=["pca"], difficulty=3, kind="recall", core=True,
                 stem="s1", answer_key="k", citations=["p_pca1"], review_status=F),
        QuizItem(id="q_core_open", concept_ids=["pca"], difficulty=3, kind="recall", core=True,
                 stem="s2", answer_key="k", citations=["p_pca1"], review_status=F),
        QuizItem(id="q_transfer", concept_ids=["pca", "svd"], difficulty=4, kind="transfer",
                 stem="s3", answer_key="k", tests_connection_id="conn.pca.svd.en",
                 citations=["p_svd"], review_status=A),
        QuizItem(id="q_orphan", concept_ids=["vector", "lora"], difficulty=4, kind="transfer",
                 stem="s4", answer_key="k", tests_connection_id="conn.vector.lora.en",
                 citations=["p_vec"], review_status=A),
    ]
    return ContentBundle(sources=[FIXTURE_SOURCE], passages=MINI_PASSAGES,
                         concepts=MINI_CONCEPTS, edges=edges, segments=segments,
                         perspectives=perspectives, connections=connections,
                         quiz_items=quiz), q


def test_publishable_bundle_rules():
    pub = publishable_bundle(*_bundle())
    keys = {(e.src, e.dst) for e in pub.edges}
    assert keys == {("eigenvalue", "pca"), ("pca", "svd"), ("svd", "lora")}
    assert next(e for e in pub.edges if e.dst == "lora").review_status is H
    assert {s.id for s in pub.segments} == {"pca.definition.en", "pca.definition.zh",
                                           "pca.perspective.variance.en", "conn.pca.svd.en"}
    assert [c.segment_id for c in pub.connections] == ["conn.pca.svd.en"]
    by_id = {q.id: q for q in pub.quiz_items}
    assert set(by_id) == {"q_core_ok", "q_transfer"}
    assert by_id["q_core_ok"].answer_verified_by_human and by_id["q_core_ok"].review_status is H
    assert {p.id for p in pub.passages} == {"p_pca1", "p_pca2", "p_svd"}
    assert [s.id for s in pub.sources] == ["fx"]


def test_snapshot_writes_loadable_versioned_graph(tmp_path):
    bundle, q = _bundle()
    cv = snapshot(bundle, q, tmp_path / "versions", NOW)
    assert cv.id.startswith("cv_") and cv.snapshot_ref == f"versions/{cv.id}/graph.json"
    path = tmp_path / cv.snapshot_ref
    g = load_graph(path)
    assert g.content_version == cv
    assert g.segment("pca.definition.en").content_version == cv.id
    assert g.concept("pca").content_version == cv.id
    assert g.item("q_core_ok").answer_verified_by_human
    assert snapshot(bundle, q, tmp_path / "versions", NOW.replace(year=2027)) == cv
    assert len(list((tmp_path / "versions").iterdir())) == 1


def test_snapshot_id_changes_with_content(tmp_path):
    bundle, q = _bundle()
    cv1 = snapshot(bundle, q, tmp_path / "v", NOW)
    changed = bundle.model_copy(update={"segments": bundle.segments[:1]})
    cv2 = snapshot(changed, q, tmp_path / "v", NOW)
    assert cv1.id != cv2.id


def test_snapshot_removes_invalid_output(tmp_path, monkeypatch):
    bundle, q = _bundle()

    def boom(path):
        raise ValueError("invalid graph")

    monkeypatch.setattr("evowiki.pipeline.snapshot.load_graph", boom)
    with pytest.raises(ValueError, match="invalid graph"):
        snapshot(bundle, q, tmp_path / "v", NOW)
    assert not list((tmp_path / "v").rglob("graph.json"))


def test_snapshot_document_is_canonical_json(tmp_path):
    bundle, q = _bundle()
    cv = snapshot(bundle, q, tmp_path / "v", NOW)
    doc = json.loads((tmp_path / "v" / cv.id / "graph.json").read_text(encoding="utf-8"))
    assert [c["id"] for c in doc["concepts"]] == sorted(c["id"] for c in doc["concepts"])
    assert doc["content_version"]["id"] == cv.id


def test_stale_approval_does_not_upgrade_changed_segment():
    seg = _seg("pca.definition.en", "pca", SegmentKind.DEFINITION, "p_pca1", status=F)
    q = ReviewQueue()
    stale = q.enqueue(ReviewTarget.SEGMENT, seg.id, "unsupported_sentences",
                      fingerprint="stale-content")
    q.decide(stale.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    bundle = ContentBundle(sources=[FIXTURE_SOURCE], passages=MINI_PASSAGES,
                           concepts=MINI_CONCEPTS, segments=[seg])
    assert publishable_bundle(bundle, q).segments == []

    current = q.enqueue(ReviewTarget.SEGMENT, seg.id, "unsupported_sentences",
                        fingerprint=review_fingerprint(seg))
    q.decide(current.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    [kept] = publishable_bundle(bundle, q).segments
    assert kept.review_status is H


def test_approved_cycle_item_does_not_break_snapshot(make_ctx, tmp_path):
    ctx = make_ctx()
    pre = Relation.PREREQUISITE_OF
    edges = [Edge(src=s, dst=d, relation=pre, kind=PrereqKind.FORMAL, strength=w,
                  review_status=A)
             for s, d, w in (("vector", "matrix", 0.9), ("matrix", "eigenvalue", 0.8),
                             ("eigenvalue", "vector", 0.3))]
    edges, _ = enforce_dag(ctx, edges)
    [item] = ctx.queue.items(target=ReviewTarget.EDGE)
    assert item.reason == "cycle"
    ctx.queue.decide(item.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    bundle = ContentBundle(sources=[FIXTURE_SOURCE], passages=MINI_PASSAGES,
                           concepts=MINI_CONCEPTS, edges=edges)
    pub = publishable_bundle(bundle, ctx.queue)
    dag = nx.DiGraph([(e.src, e.dst) for e in pub.edges if e.relation is pre])
    assert nx.is_directed_acyclic_graph(dag)
    published = {(e.src, e.dst): e.review_status for e in pub.edges}
    assert published[("eigenvalue", "vector")] is H  # the human-approved edge wins
    assert len(published) == 2
    cv = snapshot(bundle, ctx.queue, tmp_path / "versions", NOW)
    load_graph(tmp_path / cv.snapshot_ref)
    assert len(ctx.queue.items(target=ReviewTarget.EDGE)) == 1  # snapshot never enqueues


def _two_sentence(sid, cites, status=A, lang=Lang.EN, tf=None):
    return Segment(id=sid, concept_id="pca", kind=SegmentKind.DEFINITION, lang=lang,
                   sentences=[CitedSentence(text=f"s{i}.", citations=[c])
                              for i, c in enumerate(cites)],
                   license_obligations=OBL, review_status=status, translated_from=tf)


@pytest.mark.parametrize("new_cites", [["p_pca1"], ["p_pca1", "p_svd"]],
                         ids=["sentence-count", "citations"])
def test_stale_zh_of_regenerated_en_is_not_published(new_cites):
    old_zh = _two_sentence("pca.definition.zh", ["p_pca1", "p_pca2"], lang=Lang.ZH,
                           tf="pca.definition.en")
    new_en = _two_sentence("pca.definition.en", new_cites, status=F)
    q = ReviewQueue()
    item = q.enqueue(ReviewTarget.SEGMENT, new_en.id, "unsupported_sentences",
                     fingerprint=review_fingerprint(new_en))
    q.decide(item.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    bundle = ContentBundle(sources=[FIXTURE_SOURCE], passages=MINI_PASSAGES,
                           concepts=MINI_CONCEPTS, segments=[new_en, old_zh])
    assert [s.id for s in publishable_bundle(bundle, q).segments] == ["pca.definition.en"]

    aligned = _two_sentence("pca.definition.zh", new_cites, lang=Lang.ZH,
                            tf="pca.definition.en")
    bundle = bundle.model_copy(update={"segments": [new_en, aligned]})
    assert {s.id for s in publishable_bundle(bundle, q).segments} == {
        "pca.definition.en", "pca.definition.zh"}


def _with_covers(covers: list[str], status=A) -> tuple[ContentBundle, ReviewQueue, Perspective]:
    bundle, q = _bundle()
    [p] = bundle.perspectives
    p = p.model_copy(update={"covers_relation_ids": covers, "covers_review_status": status})
    return bundle.model_copy(update={"perspectives": [p]}), q, p


def _persp_seg(bundle: ContentBundle, p: Perspective) -> Segment:
    return next(s for s in bundle.segments if s.id == p.segment_id)


def test_dangling_covers_are_dropped_at_publish():
    # conn.vector.lora.en is not published (its path is invalid), so its relation dangles
    bundle, q, _ = _with_covers(["rel:pca~svd", "rel:lora~vector"])
    [p] = publishable_bundle(bundle, q).perspectives
    assert p.covers_relation_ids == ["rel:pca~svd"] and p.covers_review_status is A

    bundle, q, _ = _with_covers(["rel:lora~vector"])
    [p] = publishable_bundle(bundle, q).perspectives
    assert p.covers_relation_ids == [] and p.covers_review_status is None


def test_rejected_covers_review_clears_covers():
    bundle, q, p = _with_covers(["rel:pca~svd"])
    item = q.enqueue(ReviewTarget.PERSPECTIVE, p.segment_id, "covers_review", spot_check=True,
                     fingerprint=covers_fingerprint(p, _persp_seg(bundle, p)))
    [pub] = publishable_bundle(bundle, q).perspectives
    assert pub.covers_relation_ids == ["rel:pca~svd"] and pub.covers_review_status is A
    q.decide(item.id, ReviewState.REJECTED, decided_by="h", now=NOW)
    [pub] = publishable_bundle(bundle, q).perspectives
    assert pub.segment_id == p.segment_id  # the perspective itself still publishes
    assert pub.covers_relation_ids == [] and pub.covers_review_status is None


def test_unpublishable_covers_status_clears_covers():
    bundle, q, _ = _with_covers(["rel:pca~svd"], status=F)
    [pub] = publishable_bundle(bundle, q).perspectives
    assert pub.covers_relation_ids == [] and pub.covers_review_status is None

    bundle, q, p = _with_covers(["rel:pca~svd"])   # an open non-spot-check item flags it
    q.enqueue(ReviewTarget.PERSPECTIVE, p.segment_id, "covers_review",
              fingerprint=covers_fingerprint(p, _persp_seg(bundle, p)))
    [pub] = publishable_bundle(bundle, q).perspectives
    assert pub.covers_relation_ids == [] and pub.covers_review_status is None


def test_covers_approval_does_not_carry_to_rewritten_perspective_text():
    bundle, q, p = _with_covers(["rel:pca~svd"])
    item = q.enqueue(ReviewTarget.PERSPECTIVE, p.segment_id, "covers_review", spot_check=True,
                     fingerprint=covers_fingerprint(p, _persp_seg(bundle, p)))
    q.decide(item.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    [pub] = publishable_bundle(bundle, q).perspectives
    assert pub.covers_review_status is H
    new_text = (CitedSentence(text="y.", citations=["p_pca2"]),)
    rewritten = [s.model_copy(update={"sentences": new_text}) if s.id == p.segment_id else s
                 for s in bundle.segments]
    [pub] = publishable_bundle(bundle.model_copy(update={"segments": rewritten}), q).perspectives
    assert pub.covers_relation_ids == ["rel:pca~svd"] and pub.covers_review_status is A


def _with_phase(status) -> tuple[ContentBundle, ReviewQueue, Perspective]:
    bundle, q, p = _with_covers(["rel:pca~svd"])
    p = p.model_copy(update={"framing_phase": "LA", "framing_phase_status": status})
    return bundle.model_copy(update={"perspectives": [p]}), q, p


def test_phase_publishes_only_with_publishable_status():
    bundle, q, _ = _with_phase(A)
    [pub] = publishable_bundle(bundle, q).perspectives
    assert pub.framing_phase == "LA" and pub.framing_phase_status is A
    bundle, q, _ = _with_phase(F)
    [pub] = publishable_bundle(bundle, q).perspectives
    assert pub.framing_phase is None and pub.framing_phase_status is None
    assert pub.covers_relation_ids == ["rel:pca~svd"]     # covers are reviewed independently


def test_phase_review_is_applied_at_publish():
    bundle, q, p = _with_phase(A)
    item = q.enqueue(ReviewTarget.PERSPECTIVE, p.segment_id, PHASE_REASON, spot_check=True,
                     fingerprint=phase_fingerprint(p, _persp_seg(bundle, p)))
    q.decide(item.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    [pub] = publishable_bundle(bundle, q).perspectives
    assert pub.framing_phase == "LA" and pub.framing_phase_status is H
    assert pub.covers_review_status is A                  # a phase approval is not a covers one

    bundle, q, p = _with_phase(A)
    item = q.enqueue(ReviewTarget.PERSPECTIVE, p.segment_id, PHASE_REASON, spot_check=True,
                     fingerprint=phase_fingerprint(p, _persp_seg(bundle, p)))
    q.decide(item.id, ReviewState.REJECTED, decided_by="h", now=NOW)
    [pub] = publishable_bundle(bundle, q).perspectives
    assert pub.framing_phase is None and pub.framing_phase_status is None
    assert pub.covers_relation_ids == ["rel:pca~svd"] and pub.covers_review_status is A


def test_rejected_covers_keep_reviewed_phase():
    bundle, q, p = _with_phase(A)
    item = q.enqueue(ReviewTarget.PERSPECTIVE, p.segment_id, "covers_review", spot_check=True,
                     fingerprint=covers_fingerprint(p, _persp_seg(bundle, p)))
    q.decide(item.id, ReviewState.REJECTED, decided_by="h", now=NOW)
    [pub] = publishable_bundle(bundle, q).perspectives
    assert pub.covers_relation_ids == [] and pub.framing_phase == "LA"
