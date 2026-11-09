import ast
import dataclasses
import inspect

from evowiki.content.models import (
    Edge,
    Passage,
    PrereqKind,
    Relation,
    ReviewStatus,
    SegmentKind,
)
from evowiki.pipeline import perspectives as perspectives_module
from evowiki.pipeline.edgeview import EdgeView
from evowiki.pipeline.perspectives import (
    MAX_PERSPECTIVES,
    PHASE_REASON,
    PHASE_VERIFY_PROMPT_VERSION,
    PHASE_VERIFY_PURPOSE,
    PRESUMES_PURPOSE,
    PROMPT_VERSION,
    PURPOSE,
    RETRIEVAL_VERSION,
    SYSTEM,
    _prompt,
    apply_phase_review,
    bridging_passages,
    derive_presumes,
    generate_perspectives,
    perspective_passages,
    phase_fingerprint,
    presume_candidates,
    slugify,
)
from evowiki.pipeline.review import ReviewState, ReviewTarget
from evowiki.pipeline.support import SUPPORT_PURPOSE
from tests.pipeline.helpers import MINI_PASSAGES, NOW


def _p(src, dst, kind=PrereqKind.FORMAL):
    return Edge(src=src, dst=dst, relation=Relation.PREREQUISITE_OF, kind=kind, strength=0.8)


VIEW_EDGES = [_p("eigenvalue", "pca"), _p("covariance", "pca"), _p("variance", "covariance"),
              _p("matrix", "eigenvalue"), _p("vector", "covariance", PrereqKind.PEDAGOGICAL)]
VIEW = EdgeView(VIEW_EDGES)
SID = "pca.perspective.variance_maximization.en"


def test_slugify():
    assert slugify("Variance maximization!") == "variance_maximization"
    assert slugify("???") == "view"


def test_presume_candidates_and_derivation():
    cands = presume_candidates("pca", {"variance"}, VIEW)
    assert cands == {"eigenvalue", "covariance", "matrix"}
    req, helpful = derive_presumes(
        "pca", {"variance"}, VIEW,
        {"covariance": "required", "eigenvalue": "not_needed", "matrix": "helpful"})
    assert req == ["covariance", "variance"]
    assert helpful == ["matrix", "vector"]


def test_generate_perspectives(make_ctx):
    gen = {(PURPOSE, "pca"): {"perspectives": [
        {"framing_label": "Variance maximization", "framing_phase": "ML", "sentences": [
            {"text": "PCA can be viewed as finding orthogonal directions of maximal variance.",
             "citations": ["p_pca2"]}]},
        {"framing_label": "Spectral view", "framing_phase": "LA", "sentences": [
            {"text": "PCA is the only way to reduce dimension.", "citations": ["p_pca1"]}]},
    ]}}
    ver = {
        (SUPPORT_PURPOSE, SID): {"verdicts": [{"index": 0, "supported": True}]},
        (SUPPORT_PURPOSE, "pca.perspective.spectral_view.en"): {"verdicts": [
            {"index": 0, "supported": False}]},
        (PRESUMES_PURPOSE, SID): {"verdicts": [
            {"concept_id": "covariance", "level": "required"},
            {"concept_id": "eigenvalue", "level": "not_needed"},
            {"concept_id": "matrix", "level": "helpful"},
            {"concept_id": "lora", "level": "required"}]},   # not a candidate -> ignored
        (PHASE_VERIFY_PURPOSE, SID): {"phase": "ML", "agrees": True, "reason": "scripted"},
    }
    ctx = make_ctx(gen, ver)
    segs, persps, report = generate_perspectives(ctx, ctx.concepts["pca"], VIEW)
    assert [s.id for s in segs] == [SID] and segs[0].kind is SegmentKind.PERSPECTIVE
    [p] = persps
    assert p.framing_label == "Variance maximization" and p.segment_id == SID
    assert p.presumes_required == ["covariance", "variance"]
    assert p.presumes_helpful == ["matrix", "vector"]
    assert "pca.perspective.spectral_view.en: no supported sentences" in report.dropped
    assert len(report.llm_call_ids) == 5
    assert p.framing_phase == "ML" and p.framing_phase_status is ReviewStatus.AUTO_VERIFIED
    [item] = ctx.queue.items(target=ReviewTarget.PERSPECTIVE)
    assert item.reason == PHASE_REASON and item.spot_check and item.target_id == SID
    assert item.payload == {"framing_phase": "ML"}
    assert item.fingerprint == phase_fingerprint(p, segs[0])


def test_prompt_v3_lists_neighbouring_concepts_with_domains(make_ctx):
    seen = []

    def gen_pca(req):
        seen.append(req)
        return {"perspectives": []}

    view = EdgeView([*VIEW_EDGES, Edge(src="pca", dst="autoencoder",
                                       relation=Relation.RELATED_TO, strength=0.8)])
    ctx = make_ctx({(PURPOSE, "pca"): gen_pca}, {})
    generate_perspectives(ctx, ctx.concepts["pca"], view)
    [req] = seen
    assert PROMPT_VERSION == "perspectives.generate.v3" and MAX_PERSPECTIVES == 4
    assert req.prompt_version == PROMPT_VERSION
    assert "Concept: pca (Principal Component Analysis, domain ml)." in req.prompt
    near = req.prompt.split("Neighbouring concepts:\n", 1)[1].split("\nList up to", 1)[0]
    assert near.splitlines() == ["- autoencoder (Autoencoder, domain dl)",
                                 "- covariance (Covariance Matrix, domain prob)",
                                 "- eigenvalue (Eigenvalue, domain la)"]
    assert "[p_ae]" in req.prompt  # neighbour passages are retrievable
    assert "framing_phase" in req.prompt and "framing_phase" in req.system
    assert "representation-learning / autoencoder framing" in req.prompt
    assert "different phases" in req.prompt


ONE_VIEW = {(PURPOSE, "pca"): {"perspectives": [
    {"framing_label": "Variance maximization", "framing_phase": "DL", "sentences": [
        {"text": "PCA can be viewed as finding orthogonal directions of maximal variance.",
         "citations": ["p_pca2"]}]}]}}


def _one_view_ctx(make_ctx, verdict):
    seen = []

    def verify(req):
        seen.append(req)
        return verdict

    ver = {(SUPPORT_PURPOSE, SID): {"verdicts": [{"index": 0, "supported": True}]},
           (PRESUMES_PURPOSE, SID): {"verdicts": []},
           (PHASE_VERIFY_PURPOSE, SID): verify}
    return make_ctx(ONE_VIEW, ver), seen


def test_phase_disagreement_fails_closed(make_ctx):
    ctx, seen = _one_view_ctx(make_ctx, {"phase": "ML", "agrees": False, "reason": "variance"})
    segs, [p], report = generate_perspectives(ctx, ctx.concepts["pca"], VIEW)
    assert [s.id for s in segs] == [SID]          # the perspective itself survives
    assert p.framing_phase is None and p.framing_phase_status is None
    assert ctx.queue.items(target=ReviewTarget.PERSPECTIVE) == []
    assert any(d.startswith(f"{SID}: verifier rejected framing_phase DL") for d in report.dropped)
    [req] = seen
    assert req.prompt_version == PHASE_VERIFY_PROMPT_VERSION == "perspective_phase.verify.v1"
    assert "proposed framing_phase DL" in req.prompt and segs[0].body in req.prompt


def test_phase_agreement_on_another_phase_fails_closed(make_ctx):
    ctx, _ = _one_view_ctx(make_ctx, {"phase": "ML", "agrees": True, "reason": "?"})
    _, [p], _ = generate_perspectives(ctx, ctx.concepts["pca"], VIEW)
    assert p.framing_phase is None and p.framing_phase_status is None


def _phased(make_ctx, state):
    ctx, _ = _one_view_ctx(make_ctx, {"phase": "DL", "agrees": True})
    [seg], [p], _ = generate_perspectives(ctx, ctx.concepts["pca"], VIEW)
    assert p.framing_phase == "DL" and p.framing_phase_status is ReviewStatus.AUTO_VERIFIED
    [item] = ctx.queue.items(target=ReviewTarget.PERSPECTIVE)
    ctx.queue.decide(item.id, state, decided_by="t", now=NOW)
    return ctx, seg, p


def test_human_approve_upgrades_phase(make_ctx):
    ctx, seg, p = _phased(make_ctx, ReviewState.APPROVED)
    out = apply_phase_review(ctx.queue, p, seg)
    assert out.framing_phase == "DL" and out.framing_phase_status is ReviewStatus.HUMAN_APPROVED
    # covers are reviewed later and independently: they do not unbind the phase decision
    covered = out.model_copy(update={"covers_relation_ids": ["rel:covariance~pca"],
                                     "covers_review_status": ReviewStatus.AUTO_VERIFIED})
    assert apply_phase_review(ctx.queue, covered, seg).framing_phase_status is (
        ReviewStatus.HUMAN_APPROVED)
    rewritten = seg.model_copy(update={"sentences": (
        seg.sentences[0].model_copy(update={"text": "PCA keeps the top components."}),)})
    assert apply_phase_review(ctx.queue, p, rewritten).framing_phase_status is (
        ReviewStatus.AUTO_VERIFIED)                       # approval not carried to new text


def test_human_reject_clears_phase(make_ctx):
    ctx, seg, p = _phased(make_ctx, ReviewState.REJECTED)
    out = apply_phase_review(ctx.queue, p, seg)
    assert out.framing_phase is None and out.framing_phase_status is None


# --- perspectives.retrieval.bridge-v1 -------------------------------------------------------

AE_VIEW = EdgeView([*VIEW_EDGES, Edge(src="pca", dst="autoencoder",
                                      relation=Relation.RELATED_TO, strength=0.8)])
# Two extra passages that jointly mention PCA and the autoencoder neighbour.
BRIDGE_PASSAGES = [
    *MINI_PASSAGES,
    Passage(id="p_ae2", source_id="fx", locator="tests://fx#p_ae2",
            text="A linear autoencoder with one hidden layer is equivalent to PCA."),
    Passage(id="p_ae3", source_id="fx", locator="tests://fx#p_ae3",
            text="Training an autoencoder recovers the principal subspace found by PCA."),
]


def _bridge_ctx(make_ctx, k, gen=None, passages=None):
    ctx = make_ctx(gen or {}, {}, passages=passages or BRIDGE_PASSAGES)
    return dataclasses.replace(ctx, retrieval_k=k)


def _neighbours_of(ctx, view):
    return [ctx.concepts[c] for c in perspectives_module._neighbours("pca", view)]


def test_bridging_passage_missed_by_top_k_reaches_prompt(make_ctx):
    seen = []

    def gen_pca(req):
        seen.append(req)
        return {"perspectives": []}

    ctx = _bridge_ctx(make_ctx, 2, {(PURPOSE, "pca"): gen_pca})
    neighbours = _neighbours_of(ctx, AE_VIEW)
    plain = [p.id for p in ctx.retrieve([ctx.concepts["pca"], *neighbours])]
    assert "p_ae" not in plain                      # the plain top-k misses it
    generate_perspectives(ctx, ctx.concepts["pca"], AE_VIEW)
    [req] = seen
    assert "[p_ae]" in req.prompt
    block = req.prompt.split("Passages:\n", 1)[1]
    ids = [line.split("]", 1)[0][1:] for line in block.splitlines()]
    assert ids[:len(plain)] == plain                # focal evidence first, in its order


def test_bridging_passages_require_both_concepts(make_ctx):
    ctx = _bridge_ctx(make_ctx, 2)
    pca = ctx.concepts["pca"]
    got = bridging_passages(ctx, pca, [ctx.concepts["autoencoder"]], per_neighbour=10)
    assert {p.id for p in got} == {"p_ae", "p_ae2", "p_ae3"}
    assert [p.id for p in bridging_passages(ctx, pca, [ctx.concepts["lora"]])] == []
    # the matched passages are drawn from the context's eligible pool, unchanged
    assert all(ctx.passages[p.id] is p for p in got)


def test_bridging_passages_are_deduplicated_against_focal_evidence(make_ctx):
    ctx = _bridge_ctx(make_ctx, 8)
    pca = ctx.concepts["pca"]
    neighbours = _neighbours_of(ctx, AE_VIEW)
    focal = [p.id for p in ctx.retrieve([pca, *neighbours])]
    bridge = [p.id for p in bridging_passages(ctx, pca, neighbours)]
    assert set(focal) & set(bridge)                 # overlap exists, so dedup is exercised
    merged = [p.id for p in perspective_passages(ctx, pca, neighbours)]
    assert len(merged) == len(set(merged))
    assert merged == focal + [b for b in bridge if b not in focal]


def test_bridging_additions_are_capped(make_ctx):
    ctx = _bridge_ctx(make_ctx, 1)
    pca = ctx.concepts["pca"]
    neighbours = [ctx.concepts["autoencoder"]]
    focal = [p.id for p in ctx.retrieve([pca, *neighbours])]
    bridge = bridging_passages(ctx, pca, neighbours, per_neighbour=10)
    assert len([b for b in bridge if b.id not in focal]) > 2
    merged = perspective_passages(ctx, pca, neighbours, per_neighbour=10)
    assert len(merged) - len(focal) == 2 * len(neighbours)
    assert perspective_passages(ctx, pca, []) == ctx.retrieve([pca])   # no neighbours, no bridge


def test_request_carries_retrieval_version_and_unchanged_prompt(make_ctx):
    seen = []

    def gen_pca(req):
        seen.append(req)
        return {"perspectives": []}

    ctx = _bridge_ctx(make_ctx, 2, {(PURPOSE, "pca"): gen_pca})
    generate_perspectives(ctx, ctx.concepts["pca"], AE_VIEW)
    [req] = seen
    assert RETRIEVAL_VERSION == "perspectives.retrieval.bridge-v1"
    assert req.params == {"retrieval": RETRIEVAL_VERSION}
    assert req.prompt_version == PROMPT_VERSION == "perspectives.generate.v3"
    assert req.system == SYSTEM
    neighbours = _neighbours_of(ctx, AE_VIEW)
    passages = perspective_passages(ctx, ctx.concepts["pca"], neighbours)
    assert req.prompt == _prompt(ctx.concepts["pca"], passages, neighbours)


def test_retrieval_code_has_no_concept_specific_path():
    known = ("pca", "autoencoder", "covariance", "eigenvalue", "svd", "lora", "neural_network",
             "representation_learning")
    for fn in (bridging_passages, perspective_passages):
        tree = ast.parse(inspect.getsource(fn))
        literals = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not any(k in lit for k in known for lit in literals), fn.__name__
