"""One canonical Segment (spec 4.1) serves both the pipeline (cited sentences) and renderer seeds."""

import pytest
from pydantic import ValidationError

from evowiki.content.graph import ContentGraph
from evowiki.content.models import (
    PUBLISHABLE,
    RENDERABLE_STATUSES,
    CitedSentence,
    Concept,
    Lang,
    LicenseObligations,
    ReviewStatus,
    Segment,
    SegmentKind,
)

OBL = LicenseObligations(attribution=["fx"], share_alike=False, non_commercial=False)
SENTS = [CitedSentence(text="A.", citations=["p2"]), CitedSentence(text="B.", citations=["p1"])]
BASE = {"id": "pca.definition.en", "concept_id": "pca", "kind": SegmentKind.DEFINITION,
        "lang": Lang.EN, "review_status": ReviewStatus.AUTO_VERIFIED}


def test_status_aliases_are_one_set():
    assert RENDERABLE_STATUSES is PUBLISHABLE


def test_matching_explicit_body_and_citations_are_accepted():
    s = Segment(**BASE, sentences=SENTS, body="A. B.", citations=("p1", "p2"),
                license_obligations=OBL)
    assert s == Segment(**BASE, sentences=SENTS, license_obligations=OBL)


@pytest.mark.parametrize("extra", [{"body": "other"}, {"citations": ("p1",)}])
def test_body_or_citations_disagreeing_with_sentences_raise(extra):
    with pytest.raises(ValidationError, match="disagree"):
        Segment(**BASE, sentences=SENTS, license_obligations=OBL, **extra)


def test_seed_segment_without_sentences_keeps_free_body_and_no_translation_rule():
    s = Segment(**{**BASE, "id": "pca.definition.zh", "lang": Lang.ZH}, body="手写")
    assert s.body == "手写" and s.citations == () and s.translated_from is None
    assert Segment.model_validate(s.model_dump(mode="json")) == s


def test_segments_are_frozen():
    s = Segment(**BASE, sentences=SENTS, license_obligations=OBL)
    with pytest.raises(ValidationError):
        s.body = "x"


def _graph(seg: Segment) -> ContentGraph:
    c = Concept(id="pca", name_en="PCA", name_zh="主成分分析", domain="ml", difficulty=3)
    return ContentGraph([c], [], [], segments=[seg])


def test_canonical_graph_rejects_segments_without_sentences():
    with pytest.raises(ValueError, match="no cited sentences"):
        _graph(Segment(**BASE, body="seed", license_obligations=OBL))


def test_canonical_graph_rejects_segments_without_license_obligations():
    with pytest.raises(ValueError, match="license obligations"):
        _graph(Segment(**{**BASE, "id": "s"}, sentences=[CitedSentence(text="A.",
                                                                          citations=["p1"])]))
