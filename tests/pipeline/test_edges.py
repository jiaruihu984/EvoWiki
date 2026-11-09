from evowiki.content.models import Edge, PrereqKind, Relation, ReviewStatus
from evowiki.pipeline.edges import (
    PURPOSE,
    domain_part_of_edges,
    edge_key,
    enforce_dag,
    propose_edges,
    reconcile_edges,
)
from evowiki.pipeline.edgeview import EdgeView
from evowiki.pipeline.review import ReviewState, ReviewTarget
from tests.pipeline.helpers import MINI_CONCEPTS, NOW


def _e(src, dst, rel="prerequisite_of", kind="formal", strength=0.8, cites=(), why="r"):
    return {"src": src, "dst": dst, "relation": rel, "kind": kind if rel == "prerequisite_of"
            else None, "strength": strength, "citations": list(cites), "rationale": why}


GEN = {(PURPOSE, "pca"): {"edges": [
    _e("eigenvalue", "pca", cites=["p_pca1"]),                       # agree formal -> auto
    _e("covariance", "pca", kind="pedagogical", strength=0.6),       # agree ped, no cite
    _e("variance", "pca", kind="formal", cites=["p_pca2"]),          # kind disagreement
    _e("svd", "pca", rel="related_to", strength=0.7, cites=["p_svd"]),  # single provider
    _e("matrix", "pca", kind="formal", cites=["p_vec"]),             # invalid cite -> drop
    _e("pca", "pca", rel="related_to"),                              # self loop -> drop
    _e("ghost", "pca"),                                              # unknown -> drop
]}}
VER = {(PURPOSE, "pca"): {"edges": [
    _e("eigenvalue", "pca", strength=1.0, cites=["p_pca1"]),
    _e("covariance", "pca", kind="pedagogical", strength=0.8),
    _e("variance", "pca", kind="pedagogical", strength=0.5),
]}}


def _run(make_ctx, spot=0.0):
    ctx = make_ctx(GEN, VER, spot=spot)
    pca = ctx.concepts["pca"]
    recs_a, rep_a = propose_edges(ctx, "generator", pca)
    recs_b, _ = propose_edges(ctx, "verifier", pca)
    edges, report = reconcile_edges(ctx, recs_a + recs_b)
    return ctx, {edge_key(e.src, e.dst, e.relation): e for e in edges}, report, rep_a


def test_edge_key_sorts_related_endpoints():
    assert edge_key("pca", "svd", Relation.RELATED_TO) == "pca|related_to|svd"
    assert edge_key("svd", "pca", Relation.RELATED_TO) == "pca|related_to|svd"
    assert edge_key("svd", "pca", Relation.PREREQUISITE_OF) == "svd|prerequisite_of|pca"


def test_reconcile_applies_s2_rules(make_ctx):
    ctx, by_key, report, rep_a = _run(make_ctx)
    assert len(rep_a.llm_call_ids) == 1
    auto = by_key["eigenvalue|prerequisite_of|pca"]
    assert auto.review_status is ReviewStatus.AUTO_VERIFIED and auto.strength == 0.9
    assert auto.citations == ["p_pca1"] and auto.provenance == "agree:generator+verifier"
    ped = by_key["covariance|prerequisite_of|pca"]
    assert ped.review_status is ReviewStatus.FLAGGED
    dis = by_key["variance|prerequisite_of|pca"]
    assert dis.review_status is ReviewStatus.FLAGGED and dis.kind is PrereqKind.PEDAGOGICAL
    single = by_key["pca|related_to|svd"]
    assert single.review_status is ReviewStatus.FLAGGED
    assert "matrix|prerequisite_of|pca" not in by_key
    reasons = {i.target_id: i.reason for i in ctx.queue.items(target=ReviewTarget.EDGE)}
    assert reasons == {"covariance|prerequisite_of|pca": "pedagogical_agreement_needs_human",
                       "variance|prerequisite_of|pca": "kind_disagreement",
                       "pca|related_to|svd": "single_provider"}
    dropped = "\n".join(report.dropped)
    assert "formal edge without valid citation" in dropped
    assert "self-loop" in dropped and "unknown concept" in dropped


def test_reconcile_honours_human_decisions(make_ctx):
    ctx, _, _, _ = _run(make_ctx)
    items = {i.target_id: i for i in ctx.queue.items(target=ReviewTarget.EDGE)}
    ctx.queue.decide(items["pca|related_to|svd"].id, ReviewState.APPROVED, decided_by="h",
                     now=NOW)
    ctx.queue.decide(items["variance|prerequisite_of|pca"].id, ReviewState.REJECTED,
                     decided_by="h", now=NOW)
    pca = ctx.concepts["pca"]
    recs = propose_edges(ctx, "generator", pca)[0] + propose_edges(ctx, "verifier", pca)[0]
    by_key = {edge_key(e.src, e.dst, e.relation): e for e in reconcile_edges(ctx, recs)[0]}
    assert by_key["pca|related_to|svd"].review_status is ReviewStatus.HUMAN_APPROVED
    assert "variance|prerequisite_of|pca" not in by_key


def test_spot_check_sampling_enqueues_auto_edges(make_ctx):
    ctx, _, _, _ = _run(make_ctx, spot=1.0)
    spots = [i for i in ctx.queue.items(target=ReviewTarget.EDGE) if i.spot_check]
    assert [i.target_id for i in spots] == ["eigenvalue|prerequisite_of|pca"]


def _pe(src, dst, strength, status=ReviewStatus.AUTO_VERIFIED):
    return Edge(src=src, dst=dst, relation=Relation.PREREQUISITE_OF, kind=PrereqKind.FORMAL,
                strength=strength, review_status=status)


def test_enforce_dag_flags_weakest_cycle_closer_and_keeps_all_edges(make_ctx):
    ctx = make_ctx()
    edges = [_pe("vector", "matrix", 0.9), _pe("matrix", "eigenvalue", 0.8),
             _pe("eigenvalue", "vector", 0.3)]
    out, report = enforce_dag(ctx, edges)
    status = {(e.src, e.dst): e.review_status for e in out}
    assert len(out) == 3
    assert status[("eigenvalue", "vector")] is ReviewStatus.FLAGGED
    assert status[("vector", "matrix")] is ReviewStatus.AUTO_VERIFIED
    assert report.flagged == ["eigenvalue|prerequisite_of|vector"]
    [item] = ctx.queue.items(target=ReviewTarget.EDGE)
    assert item.reason == "cycle"


def test_domain_part_of_edges():
    concepts = [*MINI_CONCEPTS]
    edges = domain_part_of_edges(concepts)
    assert all(e.relation is Relation.PART_OF and e.provenance == "rule:domain" for e in edges)
    assert {(e.src, e.dst) for e in edges} >= {("pca", "machine_learning"),
                                              ("vector", "linear_algebra")}


def test_edge_view_queries():
    edges = [_pe("vector", "matrix", 0.9), _pe("matrix", "eigenvalue", 0.8),
             Edge(src="eigenvalue", dst="pca", relation=Relation.PREREQUISITE_OF,
                  kind=PrereqKind.PEDAGOGICAL, strength=0.5),
             Edge(src="pca", dst="svd", relation=Relation.RELATED_TO, strength=0.5),
             _pe("svd", "lora", 0.5, status=ReviewStatus.FLAGGED)]
    v = EdgeView(edges)
    assert v.prereq_closure({"eigenvalue"}, 2) == {"matrix", "vector"}
    assert v.prereq_closure({"pca"}, 3, kind=PrereqKind.FORMAL) == set()
    assert v.direct_prereqs("pca", PrereqKind.PEDAGOGICAL) == {"eigenvalue"}
    assert v.adjacent("svd", "pca") and v.adjacent("pca", "eigenvalue")
    assert not v.adjacent("svd", "lora")  # flagged edge invisible
    assert v.path_valid(["eigenvalue", "pca", "svd"]) and not v.path_valid(["vector", "pca"])
    assert v.related_pairs() == [("pca", "svd")]
