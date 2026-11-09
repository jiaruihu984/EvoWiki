from evowiki.content.models import Connection, Perspective, ReviewStatus
from evowiki.pipeline.covers import (
    PROPOSE_PURPOSE,
    VERIFY_PROMPT_VERSION,
    VERIFY_PURPOSE,
    apply_covers_review,
    covers_candidates,
    covers_fingerprint,
    review_covers,
)
from evowiki.pipeline.review import ReviewState, ReviewTarget
from tests.pipeline.helpers import NOW

P = Perspective(segment_id="pca.perspective.linear_autoencoder.en", concept_id="pca",
                framing_label="linear autoencoder", presumes_required=["autoencoder"],
                presumes_helpful=["svd"])
CONNS = [Connection(segment_id="conn.pca.autoencoder.en", path=("pca", "autoencoder")),
         Connection(segment_id="conn.pca.svd.en", path=("pca", "svd")),
         Connection(segment_id="conn.pca.svd.lora.en", path=("pca", "svd", "lora")),
         Connection(segment_id="conn.vector.matrix.en", path=("vector", "matrix"))]


def test_candidates_join_concept_to_presumed():
    assert covers_candidates(P, CONNS) == {"rel:autoencoder~pca": ("pca", "autoencoder"),
                                           "rel:pca~svd": ("pca", "svd")}


def test_review_keeps_only_agreed_relations(make_ctx, seg_factory):
    seen = []

    def verify(req):
        seen.append(req)
        return {"verdicts": [{"relation_id": "rel:autoencoder~pca", "covers": True},
                             {"relation_id": "rel:pca~svd", "covers": False, "reason": "no"}]}

    gen = {(PROPOSE_PURPOSE, P.segment_id):
           {"relation_ids": ["rel:autoencoder~pca", "rel:pca~svd", "rel:bogus~pca"]}}
    ctx = make_ctx(gen, {(VERIFY_PURPOSE, P.segment_id): verify})
    conn_seg = seg_factory("conn.pca.autoencoder.en", "pca", "p_ae")
    out, rep = review_covers(ctx, P, seg_factory(P.segment_id, "pca"), CONNS,
                             {conn_seg.id: conn_seg})
    assert out.covers_relation_ids == ["rel:autoencoder~pca"]
    assert out.covers_review_status is ReviewStatus.AUTO_VERIFIED
    items = ctx.queue.items(target=ReviewTarget.PERSPECTIVE)
    assert [i.reason for i in items] == ["covers_review"] and items[0].spot_check
    assert len(rep.llm_call_ids) == 2
    # the verifier sees the same per-relation line (path + connection text) as the proposer
    [vreq] = seen
    assert vreq.prompt_version == VERIFY_PROMPT_VERSION == "perspective_covers.verify.v2"
    assert f"- rel:autoencoder~pca: pca -> autoencoder. {conn_seg.body}" in vreq.prompt
    assert "- rel:pca~svd: pca -> svd." in vreq.prompt


def test_no_candidates_means_no_llm_call(make_ctx, seg_factory):
    lone = P.model_copy(update={"presumes_required": ["matrix"], "presumes_helpful": []})
    ctx = make_ctx({}, {})
    out, rep = review_covers(ctx, lone, seg_factory(lone.segment_id, "pca"), CONNS, {})
    assert out.covers_relation_ids == [] and out.covers_review_status is None
    assert rep.llm_call_ids == []


COVERED = P.model_copy(update={"covers_relation_ids": ["rel:autoencoder~pca"],
                               "covers_review_status": ReviewStatus.AUTO_VERIFIED})


def _decided(ctx, seg, state):
    item = ctx.queue.enqueue(ReviewTarget.PERSPECTIVE, COVERED.segment_id, "covers_review",
                             spot_check=True, fingerprint=covers_fingerprint(COVERED, seg))
    assert apply_covers_review(ctx.queue, COVERED, seg).covers_review_status is (
        ReviewStatus.AUTO_VERIFIED)  # an open spot-check keeps the auto status
    ctx.queue.decide(item.id, state, decided_by="t", now=NOW)


def test_human_approve_upgrades_covers(make_ctx, seg_factory):
    ctx, seg = make_ctx({}, {}), seg_factory(P.segment_id, "pca")
    _decided(ctx, seg, ReviewState.APPROVED)
    out = apply_covers_review(ctx.queue, COVERED, seg)
    assert out.covers_review_status is ReviewStatus.HUMAN_APPROVED


def test_human_reject_clears_covers(make_ctx, seg_factory):
    ctx, seg = make_ctx({}, {}), seg_factory(P.segment_id, "pca")
    _decided(ctx, seg, ReviewState.REJECTED)
    out = apply_covers_review(ctx.queue, COVERED, seg)
    assert out.covers_relation_ids == [] and out.covers_review_status is None


def test_decision_does_not_carry_to_rewritten_perspective_text(make_ctx, seg_factory):
    ctx = make_ctx({}, {})
    old_seg = seg_factory(P.segment_id, "pca", "p_pca2")
    new_seg = seg_factory(P.segment_id, "pca", "p_ae")   # same id, rewritten body
    assert old_seg.body != new_seg.body
    _decided(ctx, old_seg, ReviewState.APPROVED)
    assert (apply_covers_review(ctx.queue, COVERED, old_seg).covers_review_status
            is ReviewStatus.HUMAN_APPROVED)
    assert (apply_covers_review(ctx.queue, COVERED, new_seg).covers_review_status
            is ReviewStatus.AUTO_VERIFIED)               # approval not carried over
    reject_ctx = make_ctx({}, {})
    _decided(reject_ctx, old_seg, ReviewState.REJECTED)
    assert apply_covers_review(reject_ctx.queue, COVERED, new_seg).covers_relation_ids == [
        "rel:autoencoder~pca"]                           # nor is a rejection


def test_covers_decision_ignores_framing_phase(make_ctx, seg_factory):
    ctx, seg = make_ctx({}, {}), seg_factory(P.segment_id, "pca")
    _decided(ctx, seg, ReviewState.APPROVED)
    phased = COVERED.model_copy(update={"framing_phase": "DL",
                                        "framing_phase_status": ReviewStatus.AUTO_VERIFIED})
    assert covers_fingerprint(phased, seg) == covers_fingerprint(COVERED, seg)
    assert (apply_covers_review(ctx.queue, phased, seg).covers_review_status
            is ReviewStatus.HUMAN_APPROVED)
