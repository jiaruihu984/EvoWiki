from evowiki.content.models import Concept, Passage
from evowiki.pipeline.linking import link_concepts, mentions
from evowiki.pipeline.retrieval import PassageIndex


def _c(cid, name, aliases=()):
    return Concept(id=cid, name_en=name, name_zh=name, domain="la", difficulty=2,
                   aliases=list(aliases))


COV = _c("covariance", "Covariance Matrix", ["covariance"])
MAT = _c("matrix", "Matrix")
EIG = _c("eigenvalue", "Eigenvalue", ["eigenvector"])


def test_link_concepts_prefers_longest_match():
    assert link_concepts("The covariance matrix is symmetric.", [COV, MAT]) == {"covariance"}
    assert link_concepts("A matrix and its covariance.", [COV, MAT]) == {"covariance", "matrix"}


def test_link_concepts_plural_and_word_boundaries():
    assert link_concepts("Eigenvalues of a matrix.", [EIG, MAT]) == {"eigenvalue", "matrix"}
    assert link_concepts("matrixes? no: submatrix", [MAT]) == {"matrix"}
    assert link_concepts("submatrix only", [MAT]) == set()


def test_mentions_uses_aliases():
    assert mentions("each eigenvector is scaled", EIG)
    assert not mentions("nothing here", EIG)


P = [Passage(id="p1", source_id="s", locator="a", text="The covariance matrix of data."),
     Passage(id="p2", source_id="s", locator="b", text="A matrix multiplies vectors."),
     Passage(id="p3", source_id="s", locator="c", text="Unrelated cooking text.")]


def test_index_ranks_phrase_hits_first_and_omits_zero_scores():
    idx = PassageIndex(P)
    assert [p.id for p in idx.search(["covariance matrix"])] == ["p1", "p2"]
    assert idx.search(["zebra"]) == []
    assert [p.id for p in idx.search(["matrix"], k=1)] == ["p1"]


def test_index_for_concepts_uses_names_and_aliases():
    assert PassageIndex(P).for_concepts([COV])[0].id == "p1"
