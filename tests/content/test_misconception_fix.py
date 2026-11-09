import pytest
from pydantic import ValidationError

from evowiki.content.io import graph_from_document
from evowiki.content.models import MisconceptionFix
from tests.fixtures.devcontent import dev_document


def test_fix_rejects_repeated_or_missing_tags():
    with pytest.raises(ValidationError):
        MisconceptionFix(segment_id="x", misconception_tags=("pca:supervised", "pca:supervised"))
    with pytest.raises(ValidationError):
        MisconceptionFix(segment_id="x", misconception_tags=())


def _with_fix(seg_id: str, tags: list[str]) -> dict:
    doc = dev_document()
    doc["misconception_fixes"] = [{"segment_id": seg_id, "misconception_tags": tags}]
    return doc


def test_graph_accepts_and_lists_fixes():
    g = graph_from_document(_with_fix("pca.misconception_fix.supervised.en", ["pca:supervised"]))
    [fix] = g.misconception_fixes()
    assert fix.misconception_tags == ("pca:supervised",)


@pytest.mark.parametrize(("seg_id", "tags", "match"), [
    ("pca.definition.en", ["pca:supervised"], "misconception_fix segment"),
    ("pca.misconception_fix.supervised.en", ["matrix:commutative"], "owned by"),
    ("pca.misconception_fix.supervised.en", ["pca:not_a_tag"], "unknown misconception tag"),
    ("nope.en", ["pca:supervised"], "misconception_fix segment"),
])
def test_graph_rejects_bad_fixes(seg_id, tags, match):
    with pytest.raises(ValueError, match=match):
        graph_from_document(_with_fix(seg_id, tags))
