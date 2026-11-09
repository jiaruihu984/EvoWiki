import pytest

from evowiki.content.models import Edge, Relation, ReviewStatus
from evowiki.pipeline.review import (
    ReviewQueue,
    ReviewState,
    ReviewTarget,
    review_fingerprint,
    review_item_id,
    spot_check_sample,
)
from tests.pipeline.helpers import NOW


def test_enqueue_is_idempotent_and_persists(tmp_path):
    q = ReviewQueue(tmp_path / "q.jsonl")
    a = q.enqueue(ReviewTarget.EDGE, "a|prerequisite_of|b", "single_provider", payload={"x": 1})
    b = q.enqueue(ReviewTarget.EDGE, "a|prerequisite_of|b", "single_provider")
    assert a.id == b.id and len(q.items()) == 1
    assert ReviewQueue(tmp_path / "q.jsonl").get(a.id).payload == {"x": 1}


def test_decide_rules(tmp_path):
    q = ReviewQueue(tmp_path / "q.jsonl")
    item = q.enqueue(ReviewTarget.SEGMENT, "pca.definition.en", "unsupported_sentences")
    with pytest.raises(ValueError, match="decided_by"):
        q.decide(item.id, ReviewState.APPROVED, decided_by=" ", now=NOW)
    with pytest.raises(ValueError, match="open"):
        q.decide(item.id, ReviewState.OPEN, decided_by="jerry", now=NOW)
    done = q.decide(item.id, ReviewState.APPROVED, decided_by="jerry", now=NOW, note="ok")
    assert done.state is ReviewState.APPROVED and done.decided_at == NOW
    with pytest.raises(ValueError, match="already decided"):
        q.decide(item.id, ReviewState.REJECTED, decided_by="jerry", now=NOW)
    assert ReviewQueue(tmp_path / "q.jsonl").get(item.id).decided_by == "jerry"


def test_resolve_semantics():
    q = ReviewQueue()
    auto = ReviewStatus.AUTO_VERIFIED
    assert q.resolve(ReviewTarget.SEGMENT, "s", auto) is auto
    q.enqueue(ReviewTarget.SEGMENT, "s", "spot_check", spot_check=True)
    assert q.resolve(ReviewTarget.SEGMENT, "s", auto) is auto
    q.enqueue(ReviewTarget.SEGMENT, "s", "unsupported_sentences")
    assert q.resolve(ReviewTarget.SEGMENT, "s", auto) is ReviewStatus.FLAGGED
    for item in q.items(state=ReviewState.OPEN):
        q.decide(item.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    assert q.resolve(ReviewTarget.SEGMENT, "s", ReviewStatus.FLAGGED) is ReviewStatus.HUMAN_APPROVED
    q2 = ReviewQueue()
    item = q2.enqueue(ReviewTarget.SEGMENT, "s", "spot_check", spot_check=True)
    q2.decide(item.id, ReviewState.REJECTED, decided_by="h", now=NOW)
    assert q2.resolve(ReviewTarget.SEGMENT, "s", auto) is None


def test_items_filters():
    q = ReviewQueue()
    q.enqueue(ReviewTarget.EDGE, "e", "r")
    q.enqueue(ReviewTarget.SEGMENT, "s", "r")
    assert [i.target for i in q.items(target=ReviewTarget.EDGE)] == [ReviewTarget.EDGE]
    assert len(q.items(state=ReviewState.OPEN)) == 2


def test_spot_check_sample_is_deterministic_fraction():
    ids = [f"id{i}" for i in range(1000)]
    s = spot_check_sample(ids, 0.1)
    assert s == spot_check_sample(ids, 0.1) and 50 < len(s) < 150
    assert spot_check_sample(ids, 0.0) == set() and spot_check_sample(ids, 1.0) == set(ids)


def test_review_fingerprint_ignores_review_status():
    a = Edge(src="a", dst="b", relation=Relation.RELATED_TO, strength=0.5,
             review_status=ReviewStatus.FLAGGED)
    b = a.model_copy(update={"review_status": ReviewStatus.HUMAN_APPROVED})
    c = a.model_copy(update={"strength": 0.6})
    assert review_fingerprint(a) == review_fingerprint(b) != review_fingerprint(c)


def test_decisions_bind_to_fingerprint():
    q = ReviewQueue()
    auto, flagged = ReviewStatus.AUTO_VERIFIED, ReviewStatus.FLAGGED
    a = q.enqueue(ReviewTarget.SEGMENT, "s", "unsupported_sentences", fingerprint="A")
    assert a.fingerprint == "A"
    assert a.id == review_item_id(ReviewTarget.SEGMENT, "s", "unsupported_sentences", "A")
    q.decide(a.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    assert q.resolve(ReviewTarget.SEGMENT, "s", flagged, fingerprint="A") is \
        ReviewStatus.HUMAN_APPROVED
    # new content: the old approval does not apply and the same reason opens a new item
    assert q.resolve(ReviewTarget.SEGMENT, "s", auto, fingerprint="B") is auto
    b = q.enqueue(ReviewTarget.SEGMENT, "s", "unsupported_sentences", fingerprint="B")
    assert b.id != a.id and b.state is ReviewState.OPEN
    assert q.resolve(ReviewTarget.SEGMENT, "s", flagged, fingerprint="B") is flagged
    # a rejected spot check on old content does not suppress new content
    spot = q.enqueue(ReviewTarget.SEGMENT, "t", "spot_check", spot_check=True, fingerprint="A")
    q.decide(spot.id, ReviewState.REJECTED, decided_by="h", now=NOW)
    assert q.resolve(ReviewTarget.SEGMENT, "t", auto, fingerprint="A") is None
    assert q.resolve(ReviewTarget.SEGMENT, "t", auto, fingerprint="B") is auto


def test_fingerprint_none_keeps_id_only_behavior():
    q = ReviewQueue()
    item = q.enqueue(ReviewTarget.EDGE, "e", "r")
    assert item.fingerprint is None
    assert item.id == review_item_id(ReviewTarget.EDGE, "e", "r")
    q.decide(item.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    flagged = ReviewStatus.FLAGGED
    assert q.resolve(ReviewTarget.EDGE, "e", flagged) is ReviewStatus.HUMAN_APPROVED
    # fingerprint-less decisions still count when a fingerprint is given
    assert q.resolve(ReviewTarget.EDGE, "e", flagged, fingerprint="X") is \
        ReviewStatus.HUMAN_APPROVED
    # without a fingerprint, every item for the id counts (unchanged behavior)
    fp_item = q.enqueue(ReviewTarget.EDGE, "e", "r2", fingerprint="Y")
    q.decide(fp_item.id, ReviewState.REJECTED, decided_by="h", now=NOW)
    assert q.resolve(ReviewTarget.EDGE, "e", flagged) is None


def test_resolve_can_filter_by_reason():
    q = ReviewQueue()
    auto = ReviewStatus.AUTO_VERIFIED
    a = q.enqueue(ReviewTarget.PERSPECTIVE, "p", "covers_review", spot_check=True,
                  fingerprint="A")
    b = q.enqueue(ReviewTarget.PERSPECTIVE, "p", "framing_phase_review", spot_check=True,
                  fingerprint="A")
    q.decide(a.id, ReviewState.REJECTED, decided_by="h", now=NOW)
    q.decide(b.id, ReviewState.APPROVED, decided_by="h", now=NOW)
    assert q.resolve(ReviewTarget.PERSPECTIVE, "p", auto, fingerprint="A") is None
    assert q.resolve(ReviewTarget.PERSPECTIVE, "p", auto, fingerprint="A",
                     reason="covers_review") is None
    assert q.resolve(ReviewTarget.PERSPECTIVE, "p", auto, fingerprint="A",
                     reason="framing_phase_review") is ReviewStatus.HUMAN_APPROVED
