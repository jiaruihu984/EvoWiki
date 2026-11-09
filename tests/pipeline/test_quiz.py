import pytest

from evowiki.content.models import Connection, ReviewStatus
from evowiki.pipeline.quiz import (
    ANSWER_PURPOSE,
    CONNECTION_MAX_ITEMS,
    PROMPT_VERSION,
    PURPOSE,
    _prompt,
    apply_quiz_review,
    generate_quiz,
    normalize_tag,
    quiz_item_id,
)
from evowiki.pipeline.review import ReviewState, ReviewTarget
from tests.pipeline.helpers import NOW

STEM_OK = "Onto which vectors does PCA project the data?"
STEM_BAD = "Is PCA invariant to feature scaling?"
STEM_OUT = "Who invented PCA?"


def _item(stem, key, cites, kind="recall", kc=(), mis=(), transfer_kind=None):
    d = {"stem": stem, "answer_key": key, "kind": kind, "difficulty": 3,
         "kc_tags": list(kc), "misconception_tags": list(mis), "citations": list(cites)}
    if transfer_kind is not None:
        d["transfer_kind"] = transfer_kind
    return d


GEN = {(PURPOSE, "pca"): {"items": [
    _item(STEM_OK, "The top eigenvectors of the covariance matrix", ["p_pca1"],
          kc=["variance_direction", "svd/x"], mis=["scale invariance"]),
    _item(STEM_BAD, "Yes", ["p_pca2"], kind="application"),
    _item(STEM_OUT, "Pearson", ["p_zzz"]),
]}}
VER = {
    (ANSWER_PURPOSE, quiz_item_id(STEM_OK)): {"answer": "top eigenvectors of covariance",
                                              "key_is_correct": True},
    (ANSWER_PURPOSE, quiz_item_id(STEM_BAD)): {"answer": "No", "key_is_correct": False,
                                               "reason": "PCA is scale sensitive"},
}


def test_normalize_tag():
    assert normalize_tag("Variance Direction", "pca", ["pca"], misconception=False) == \
        "pca/variance_direction"
    assert normalize_tag("pca/scale", "pca", ["pca"], misconception=True) == "pca/m_scale"
    assert normalize_tag("svd/x", "pca", ["pca"], misconception=False) is None


def test_non_core_items(make_ctx):
    ctx = make_ctx(GEN, VER)
    items, report = generate_quiz(ctx, ctx.concepts["pca"], core=False)
    by_stem = {q.stem: q for q in items}
    ok = by_stem[STEM_OK]
    assert ok.review_status is ReviewStatus.AUTO_VERIFIED and not ok.core
    assert ok.kc_tags == ["pca/variance_direction"]
    assert ok.misconception_tags == ["pca/m_scale_invariance"]
    assert ok.citations == ["p_pca1"] and ok.concept_ids == ["pca"]
    bad = by_stem[STEM_BAD]
    assert bad.review_status is ReviewStatus.FLAGGED
    [item] = ctx.queue.items(target=ReviewTarget.QUIZ_ANSWER)
    assert item.reason == "verifier_mismatch" and item.target_id == bad.id
    assert STEM_OUT not in by_stem
    dropped = "\n".join(report.dropped)
    assert "citation outside retrieved passages" in dropped and "dropped tag svd/x" in dropped
    assert len(report.llm_call_ids) == 3


def test_core_items_need_human_verification(make_ctx):
    ctx = make_ctx(GEN, VER)
    items, _ = generate_quiz(ctx, ctx.concepts["pca"], core=True)
    ok = next(q for q in items if q.stem == STEM_OK)
    assert ok.core and ok.review_status is ReviewStatus.FLAGGED
    assert not ok.answer_verified_by_human
    core_item = next(i for i in ctx.queue.items(target=ReviewTarget.QUIZ_ANSWER)
                     if i.target_id == ok.id and i.reason == "core_answer_key")
    ctx.queue.decide(core_item.id, ReviewState.APPROVED, decided_by="jerry", now=NOW)
    approved = apply_quiz_review(ctx.queue, ok)
    assert approved.review_status is ReviewStatus.HUMAN_APPROVED
    assert approved.answer_verified_by_human


def test_connection_items_are_transfer(make_ctx):
    conn = Connection(segment_id="conn.autoencoder.pca.en", path=["autoencoder", "pca"])
    stem = "How does a linear autoencoder relate to PCA?"
    gen = {(PURPOSE, conn.segment_id): {"items": [
        _item(stem, "It learns the same subspace", ["p_ae"], kc=["pca/subspace"],
              transfer_kind="seen")]}}
    ver = {(ANSWER_PURPOSE, quiz_item_id(stem)): {"answer": "same subspace",
                                                  "key_is_correct": True}}
    ctx = make_ctx(gen, ver)
    [q], _ = generate_quiz(ctx, ctx.concepts["autoencoder"], core=False, connection=conn)
    assert q.kind == "transfer" and q.concept_ids == ["autoencoder", "pca"]
    assert q.tests_connection_id == conn.segment_id and q.kc_tags == ["pca/subspace"]
    assert q.transfer_kind == "seen"


def test_connection_items_keep_transfer_kind(make_ctx):
    conn = Connection(segment_id="conn.autoencoder.pca.en", path=["autoencoder", "pca"])
    seen = "How does a linear autoencoder relate to PCA?"
    near = "What subspace does a linear autoencoder with tied weights recover?"
    ver = {(ANSWER_PURPOSE, quiz_item_id(s)): {"answer": "subspace", "key_is_correct": True}
           for s in (seen, near)}
    gen = {(PURPOSE, conn.segment_id): {"items": [
        _item(seen, "It learns the same subspace", ["p_ae"], transfer_kind="seen"),
        _item(near, "The principal subspace", ["p_ae"], transfer_kind="near"),
    ]}}
    ctx = make_ctx(gen, ver)
    items, _ = generate_quiz(ctx, ctx.concepts["autoencoder"], core=False, connection=conn)
    assert {q.transfer_kind for q in items} == {"seen", "near"}
    assert {q.stem for q in items} == {seen, near}


def test_connection_items_without_transfer_kind_are_dropped(make_ctx):
    conn = Connection(segment_id="conn.autoencoder.pca.en", path=["autoencoder", "pca"])
    seen = "How does a linear autoencoder relate to PCA?"
    missing = "Name one link between autoencoders and PCA."
    ver = {(ANSWER_PURPOSE, quiz_item_id(s)): {"answer": "subspace", "key_is_correct": True}
           for s in (seen, missing)}
    gen = {(PURPOSE, conn.segment_id): {"items": [
        _item(seen, "It learns the same subspace", ["p_ae"], transfer_kind="seen"),
        _item(missing, "Shared subspace", ["p_ae"]),
    ]}}
    ctx = make_ctx(gen, ver)
    items, report = generate_quiz(ctx, ctx.concepts["autoencoder"], core=False,
                                  connection=conn)
    assert {q.stem for q in items} == {seen}
    assert any(d.startswith(quiz_item_id(missing)) and "transfer_kind" in d
               for d in report.dropped)


def test_connection_quiz_asks_for_exactly_one_seen_and_one_near(make_ctx):
    """F8: connection prompts ask for exactly two items with no 'up to N' contradiction; at
    most two items are kept and a different max_items fails closed."""
    conn = Connection(segment_id="conn.autoencoder.pca.en", path=["autoencoder", "pca"])
    ctx = make_ctx({}, {})
    text = _prompt(ctx, ["autoencoder", "pca"], conn, [], CONNECTION_MAX_ITEMS)
    assert "exactly two quiz items, one 'seen' and one 'near'" in text
    assert "up to" not in text
    assert "up to 4 quiz items" in _prompt(ctx, ["pca"], None, [], 4)
    assert PROMPT_VERSION == "quiz.generate.v3" and CONNECTION_MAX_ITEMS == 2
    with pytest.raises(ValueError, match="exactly 2"):
        generate_quiz(ctx, ctx.concepts["autoencoder"], core=False, connection=conn,
                      max_items=4)
    stems = [f"Connection question {i}?" for i in range(3)]
    gen = {(PURPOSE, conn.segment_id): {"items": [
        _item(s, "k", ["p_ae"], transfer_kind=k) for s, k in zip(stems, ("seen", "near", "near"),
                                                                 strict=True)]}}
    ver = {(ANSWER_PURPOSE, quiz_item_id(s)): {"answer": "k", "key_is_correct": True}
           for s in stems}
    ctx = make_ctx(gen, ver)
    items, _ = generate_quiz(ctx, ctx.concepts["autoencoder"], core=False, connection=conn)
    assert [q.stem for q in items] == stems[:2]


def test_concept_items_never_carry_transfer_kind(make_ctx):
    gen = {(PURPOSE, "pca"): {"items": [
        _item(STEM_OK, "The top eigenvectors of the covariance matrix", ["p_pca1"],
              transfer_kind="seen")]}}
    ctx = make_ctx(gen, VER)
    [q], _ = generate_quiz(ctx, ctx.concepts["pca"], core=False)
    assert q.kind == "recall" and q.transfer_kind is None


def test_core_approval_does_not_carry_to_changed_answer_key(make_ctx):
    ctx = make_ctx(GEN, VER)
    items, _ = generate_quiz(ctx, ctx.concepts["pca"], core=True)
    ok = next(q for q in items if q.stem == STEM_OK)
    core_item = next(i for i in ctx.queue.items(target=ReviewTarget.QUIZ_ANSWER)
                     if i.target_id == ok.id and i.reason == "core_answer_key")
    ctx.queue.decide(core_item.id, ReviewState.APPROVED, decided_by="jerry", now=NOW)
    changed = ok.model_copy(update={"answer_key": "The bottom eigenvectors"})
    assert changed.id == ok.id
    still = apply_quiz_review(ctx.queue, changed)
    assert still.review_status is ReviewStatus.FLAGGED
    assert not still.answer_verified_by_human
    approved = apply_quiz_review(ctx.queue, ok)
    assert approved.review_status is ReviewStatus.HUMAN_APPROVED
    assert approved.answer_verified_by_human
