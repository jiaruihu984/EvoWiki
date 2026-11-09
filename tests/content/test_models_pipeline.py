from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from evowiki.content.models import (
    BASE_SEGMENT_KINDS,
    LICENSE_TERMS,
    PUBLISHABLE,
    CitedSentence,
    Concept,
    Connection,
    ContentVersion,
    Edge,
    Lang,
    LicenseObligations,
    Passage,
    Perspective,
    QuizItem,
    Relation,
    ReviewStatus,
    Segment,
    SegmentKind,
    Source,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
NO_OBLIGATIONS = LicenseObligations(attribution=[], share_alike=False, non_commercial=False)


def _source(**over):
    terms = LICENSE_TERMS["CC-BY-SA-4.0"]
    base = dict(
        id="d2l", title="Dive into Deep Learning", locator="https://d2l.ai",
        license="CC-BY-SA-4.0", **terms.model_dump(),
    )
    base.update(over)
    return Source(**base)


def test_known_license_flags_must_match_table():
    assert _source().share_alike is True
    with pytest.raises(ValidationError, match="license flags"):
        _source(commercial_allowed=False)


def test_unknown_license_cannot_allow_derivatives():
    with pytest.raises(ValidationError, match="unknown license"):
        Source(id="mml", title="MML", locator="x", license="personal-use",
               derivative_allowed=True, redistribution_allowed=False, commercial_allowed=False,
               attribution_required=True, share_alike=False)
    ok = Source(id="mml", title="MML", locator="x", license="personal-use",
                derivative_allowed=False, redistribution_allowed=False, commercial_allowed=False,
                attribution_required=True, share_alike=False)
    assert ok.derivative_allowed is False


def test_arxiv_nonexclusive_license_forbids_derivatives():
    assert LICENSE_TERMS["arXiv-nonexclusive-1.0"].derivative_allowed is False


def test_license_verified_at_must_be_aware():
    with pytest.raises(ValidationError):
        _source(license_verified_at=datetime(2026, 10, 1))  # noqa: DTZ001
    assert _source(license_verified_at=NOW).license_verified_at == NOW


def test_license_check_fields_default_to_none_and_are_validated():
    src = _source()
    assert src.license_check_status is None and src.license_checked_at is None
    with pytest.raises(ValidationError):
        _source(license_checked_at=datetime(2026, 10, 1))  # noqa: DTZ001
    with pytest.raises(ValidationError):
        _source(license_check_status="maybe")
    assert _source(license_check_status="mismatch", license_checked_at=NOW
                   ).license_check_status == "mismatch"


def test_passage_requires_text():
    with pytest.raises(ValidationError):
        Passage(id="p1", source_id="d2l", locator="u#c0", text="")


def test_concept_and_edge_defaults_keep_seed_compatible():
    c = Concept(id="pca", name_en="PCA", name_zh="主成分分析", domain="ml", difficulty=4)
    assert c.aliases == [] and c.content_version is None
    e = Edge(src="a", dst="b", relation=Relation.RELATED_TO, strength=0.5)
    assert e.review_status is ReviewStatus.HUMAN_APPROVED and e.provenance is None
    assert PUBLISHABLE == {ReviewStatus.AUTO_VERIFIED, ReviewStatus.HUMAN_APPROVED}


def test_base_kinds_order():
    assert [k.value for k in BASE_SEGMENT_KINDS] == [
        "one_line", "intuition", "definition", "derivation", "example", "advanced"]


def test_cited_sentence_needs_citation():
    with pytest.raises(ValidationError):
        CitedSentence(text="PCA finds directions of maximal variance.", citations=[])


def _segment(**over):
    base = {
        "id": "pca.definition.en", "concept_id": "pca", "kind": SegmentKind.DEFINITION,
        "lang": Lang.EN,
        "sentences": [CitedSentence(text="A.", citations=["p2", "p1"]),
                      CitedSentence(text="B.", citations=["p1"])],
        "license_obligations": NO_OBLIGATIONS, "review_status": ReviewStatus.AUTO_VERIFIED,
    }
    base.update(over)
    return Segment(**base)


def test_segment_body_and_citations_are_derived():
    s = _segment()
    assert s.body == "A. B."
    assert s.citations == ("p1", "p2")
    dumped = s.model_dump(mode="json")
    assert dumped["body"] == "A. B." and dumped["citations"] == ["p1", "p2"]
    assert Segment.model_validate(dumped) == s


def test_zh_segment_requires_translated_from_and_joins_without_spaces():
    with pytest.raises(ValidationError, match="translated_from"):
        _segment(id="pca.definition.zh", lang=Lang.ZH)
    zh = _segment(id="pca.definition.zh", lang=Lang.ZH, translated_from="pca.definition.en",
                  sentences=[CitedSentence(text="甲。", citations=["p1"]),
                             CitedSentence(text="乙。", citations=["p1"])])
    assert zh.body == "甲。乙。"
    with pytest.raises(ValidationError, match="translated_from"):
        _segment(translated_from="x.en")


def test_perspective_presumes_rules():
    with pytest.raises(ValidationError):
        Perspective(segment_id="s", concept_id="pca", framing_label="f",
                    presumes_required=["pca"], presumes_helpful=[])
    with pytest.raises(ValidationError):
        Perspective(segment_id="s", concept_id="pca", framing_label="f",
                    presumes_required=["variance"], presumes_helpful=["variance"])


def test_connection_path_bounds():
    Connection(segment_id="s", path=["a", "b"])
    Connection(segment_id="s", path=["a", "b", "c", "d"])
    for bad in (["a"], ["a", "b", "c", "d", "e"], ["a", "b", "a"]):
        with pytest.raises(ValidationError):
            Connection(segment_id="s", path=bad)


def test_quiz_item_tags_must_name_one_of_its_concepts():
    QuizItem(id="q", concept_ids=["pca"], difficulty=2, kind="recall",
             kc_tags=["pca/variance_direction"], misconception_tags=["pca/m_scale_free"])
    with pytest.raises(ValidationError, match="tag"):
        QuizItem(id="q", concept_ids=["pca"], difficulty=2, kind="recall", kc_tags=["svd/x"])
    with pytest.raises(ValidationError, match="tag"):
        QuizItem(id="q", concept_ids=["pca"], difficulty=2, kind="recall", kc_tags=["nofmt"])


def test_quiz_item_new_defaults():
    q = QuizItem(id="q", concept_ids=["pca"], difficulty=2, kind="recall")
    assert q.core is False and q.stem is None and q.review_status is ReviewStatus.HUMAN_APPROVED


def test_content_version_requires_aware_time():
    ContentVersion(id="cv_x", created_at=NOW, snapshot_ref="versions/cv_x/graph.json")
    with pytest.raises(ValidationError):
        ContentVersion(id="cv_x", created_at=datetime(2026, 1, 1), snapshot_ref="r")  # noqa: DTZ001
