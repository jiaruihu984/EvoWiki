
from evowiki.goals.contracts import (
    Band,
)
from evowiki.wiki.catalog import all_known, concept_bands
from tests.wiki.fakes import FakeK, zscore_pca_k


def test_all_known_needs_every_aspect_known_and_fails_closed_on_empty():
    k = zscore_pca_k()
    assert concept_bands(k, "pca") == (Band.LIKELY, Band.GAP)
    assert all_known(k, ["zscore", "var"])
    assert not all_known(k, ["zscore", "pca"])
    assert not all_known(k, ["unknown-concept"])   # no aspects → never "all known"
    assert not all_known(k, [])
    assert all_known(zscore_pca_k(pca_known=True), ["pca"])


def test_concept_bands_treats_missing_cells_as_no_evidence():
    class Sparse(FakeK):
        def cells(self, concept_ids):
            return {}
    k = Sparse({"x": {"term": Band.KNOWN}})
    assert concept_bands(k, "x") == (Band.NONE,)
