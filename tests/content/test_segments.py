import pytest
from pydantic import ValidationError

from evowiki.content.models import (
    RENDERABLE_STATUSES,
    Connection,
    Lang,
    ReviewStatus,
    Segment,
    SegmentKind,
)


def test_segment_minimal_fields():
    s = Segment(id="pca.one_line.en", concept_id="pca", kind="one_line", lang="en",
                review_status="human_approved")
    assert s.kind is SegmentKind.ONE_LINE and s.lang is Lang.EN
    assert s.body == "" and s.citations == ()


def test_segment_is_frozen():
    s = Segment(id="x", concept_id="pca", kind="one_line", lang="en", review_status="flagged")
    with pytest.raises(ValidationError):
        s.kind = SegmentKind.DEFINITION


def test_unknown_kind_rejected():
    with pytest.raises(ValidationError):
        Segment(id="x", concept_id="pca", kind="summary", lang="en", review_status="flagged")


def test_flagged_is_not_renderable():
    assert ReviewStatus.FLAGGED not in RENDERABLE_STATUSES
    assert RENDERABLE_STATUSES == {ReviewStatus.AUTO_VERIFIED, ReviewStatus.HUMAN_APPROVED}


def test_connection_path_bounds():
    Connection(segment_id="c", path=("pca", "svd"))
    Connection(segment_id="c", path=("a", "b", "c", "d"))
    with pytest.raises(ValidationError):
        Connection(segment_id="c", path=("pca",))
    with pytest.raises(ValidationError):
        Connection(segment_id="c", path=("a", "b", "c", "d", "e"))


def test_connection_path_must_be_distinct():
    with pytest.raises(ValidationError, match="repeat"):
        Connection(segment_id="c", path=("pca", "svd", "pca"))
