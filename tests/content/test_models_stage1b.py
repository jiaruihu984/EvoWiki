import hashlib
import json

import pytest
from pydantic import ValidationError

from evowiki.content.models import Perspective, QuizItem, ReviewStatus
from evowiki.pipeline.review import review_fingerprint


def test_perspective_covers_default_empty():
    p = Perspective(segment_id="pca.perspective.x.en", concept_id="pca", framing_label="x")
    assert p.covers_relation_ids == [] and p.covers_review_status is None


def test_perspective_covers_require_status():
    with pytest.raises(ValidationError, match="covers_review_status"):
        Perspective(segment_id="s", concept_id="pca", framing_label="x",
                    presumes_required=["svd"], covers_relation_ids=["rel:pca~svd"])


def test_perspective_covers_unique():
    with pytest.raises(ValidationError, match="repeat"):
        Perspective(segment_id="s", concept_id="pca", framing_label="x",
                    presumes_required=["svd"], covers_relation_ids=["rel:pca~svd"] * 2,
                    covers_review_status=ReviewStatus.AUTO_VERIFIED)


def _item(**kw):
    base = {"id": "q_1", "concept_ids": ["pca", "svd"], "difficulty": 3, "kind": "transfer",
            "tests_connection_id": "conn.pca.svd.en"}
    return QuizItem(**{**base, **kw})


def test_transfer_kind_ok():
    assert _item(transfer_kind="near").transfer_kind == "near"


@pytest.mark.parametrize("kw", [{"kind": "recall"}, {"tests_connection_id": None}])
def test_transfer_kind_needs_transfer_item_with_connection(kw):
    with pytest.raises(ValidationError, match="transfer_kind"):
        _item(transfer_kind="seen", **kw)


def _legacy_fingerprint(model, new_fields):
    exclude = {"review_status", "content_version", "answer_verified_by_human", *new_fields}
    data = model.model_dump(mode="json", exclude=exclude)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def test_fingerprints_unchanged_by_default_stage1b_fields():
    p = Perspective(segment_id="s", concept_id="pca", framing_label="x", presumes_required=["svd"])
    q = _item()
    # the legacy model had none of the Stage I-B perspective fields
    assert review_fingerprint(p) == _legacy_fingerprint(
        p, {"covers_relation_ids", "covers_review_status", "framing_phase",
            "framing_phase_status"})
    assert review_fingerprint(q) == _legacy_fingerprint(q, {"transfer_kind"})


def test_fingerprints_track_set_stage1b_fields():
    base = {"segment_id": "s", "concept_id": "pca", "framing_label": "x",
            "presumes_required": ["svd"]}
    covered = {**base, "covers_relation_ids": ["rel:pca~svd"]}
    flagged = Perspective(**covered, covers_review_status=ReviewStatus.FLAGGED)
    approved = Perspective(**covered, covers_review_status=ReviewStatus.HUMAN_APPROVED)
    assert review_fingerprint(flagged) == review_fingerprint(approved)   # status is bookkeeping
    assert review_fingerprint(flagged) != review_fingerprint(Perspective(**base))
    assert review_fingerprint(_item(transfer_kind="near")) != review_fingerprint(_item())


def test_framing_phase_requires_status():
    with pytest.raises(ValidationError, match="framing_phase_status"):
        Perspective(segment_id="s", concept_id="pca", framing_label="x", framing_phase="DL")
    with pytest.raises(ValidationError):
        Perspective(segment_id="s", concept_id="pca", framing_label="x", framing_phase="CS",
                    framing_phase_status=ReviewStatus.AUTO_VERIFIED)
    with pytest.raises(ValidationError, match="requires a framing_phase"):
        Perspective(segment_id="s", concept_id="pca", framing_label="x",
                    framing_phase_status=ReviewStatus.HUMAN_APPROVED)
    p = Perspective(segment_id="s", concept_id="pca", framing_label="x")
    assert p.framing_phase is None and p.framing_phase_status is None


def test_fingerprint_tracks_set_framing_phase_but_not_its_status():
    base = {"segment_id": "s", "concept_id": "pca", "framing_label": "x",
            "presumes_required": ["svd"]}
    plain = Perspective(**base)
    auto = Perspective(**base, framing_phase="LA", framing_phase_status=ReviewStatus.AUTO_VERIFIED)
    human = Perspective(**base, framing_phase="LA",
                        framing_phase_status=ReviewStatus.HUMAN_APPROVED)
    dl = Perspective(**base, framing_phase="DL", framing_phase_status=ReviewStatus.AUTO_VERIFIED)
    assert review_fingerprint(auto) == review_fingerprint(human)   # status is bookkeeping
    assert len({review_fingerprint(plain), review_fingerprint(auto), review_fingerprint(dl)}) == 3
