from evowiki.content.models import Edge, PrereqKind, Relation, ReviewStatus
from evowiki.pipeline.connections import (
    PURPOSE,
    connection_segment_id,
    connection_targets,
    generate_connection,
)
from evowiki.pipeline.edgeview import EdgeView
from evowiki.pipeline.support import SUPPORT_PURPOSE

VIEW = EdgeView([
    Edge(src="pca", dst="autoencoder", relation=Relation.RELATED_TO, strength=0.7),
    Edge(src="svd", dst="pca", relation=Relation.RELATED_TO, strength=0.8),
    Edge(src="neural_network", dst="autoencoder", relation=Relation.PREREQUISITE_OF,
         kind=PrereqKind.FORMAL, strength=0.9),
    Edge(src="svd", dst="lora", relation=Relation.PREREQUISITE_OF,
         kind=PrereqKind.PEDAGOGICAL, strength=0.7),
])
TRIPLE = ["pca", "svd", "lora"]
TRIPLE_SID = "conn.pca.svd.lora.en"
GEN = {
    (PURPOSE, "autoencoder->pca"): {
        "sentences": [{"text": "A linear autoencoder learns the same subspace as PCA.",
                       "citations": ["p_ae"]}],
        "hop_citations": [["p_ae"]]},
    (PURPOSE, "pca->svd->lora"): {
        "sentences": [
            {"text": "PCA can be computed from the SVD of centered data.",
             "citations": ["p_svd"]},
            {"text": "LoRA learns a low-rank update related to a truncated SVD.",
             "citations": ["p_lora"]}],
        "hop_citations": [["p_svd"], ["p_lora"]]},
}


def _ver(second_ok: bool):
    return {
        (SUPPORT_PURPOSE, "conn.autoencoder.pca.en"): {"verdicts": [
            {"index": 0, "supported": True}]},
        (SUPPORT_PURPOSE, TRIPLE_SID): {"verdicts": [
            {"index": 0, "supported": True}, {"index": 1, "supported": second_ok}]},
    }


def test_targets_include_related_edges_and_valid_curated_paths():
    targets, invalid = connection_targets(VIEW, [TRIPLE, ["vector", "lora"], ["pca", "svd"]])
    assert targets == [["autoencoder", "pca"], ["pca", "svd"], TRIPLE]
    assert invalid == ["vector->lora: not a valid <=3-hop path over prerequisite_of/related_to"]


def test_segment_id():
    assert connection_segment_id(TRIPLE) == TRIPLE_SID


def test_generate_single_hop_connection(make_ctx):
    ctx = make_ctx(GEN, _ver(True))
    seg, conn, report = generate_connection(ctx, ["autoencoder", "pca"])
    assert seg.id == "conn.autoencoder.pca.en" and seg.concept_id == "autoencoder"
    assert seg.review_status is ReviewStatus.AUTO_VERIFIED
    assert conn.path == ("autoencoder", "pca") and conn.segment_id == seg.id
    assert len(report.llm_call_ids) == 2


def test_generate_two_hop_connection(make_ctx):
    ctx = make_ctx(GEN, _ver(True))
    seg, conn, _ = generate_connection(ctx, TRIPLE)
    assert list(conn.path) == TRIPLE and seg.citations == ("p_lora", "p_svd")


def test_hop_without_surviving_citation_drops_connection(make_ctx):
    ctx = make_ctx(GEN, _ver(False))
    seg, conn, report = generate_connection(ctx, TRIPLE)
    assert seg is None and conn is None
    assert f"{TRIPLE_SID}: hop svd-lora lacks a supporting citation" in report.dropped
    assert len(report.llm_call_ids) == 2
