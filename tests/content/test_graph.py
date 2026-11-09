import pytest

from evowiki.content.graph import ContentGraph
from evowiki.content.models import Concept, Edge, PrereqKind, QuizItem, Relation


def _c(cid: str) -> Concept:
    return Concept(id=cid, name_en=cid, name_zh=cid, domain="la", difficulty=1)


def test_rejects_prerequisite_cycle():
    edges = [
        Edge(src="a", dst="b", relation=Relation.PREREQUISITE_OF, kind=PrereqKind.FORMAL, strength=1),
        Edge(src="b", dst="a", relation=Relation.PREREQUISITE_OF, kind=PrereqKind.FORMAL, strength=1),
    ]
    with pytest.raises(ValueError, match="cycle"):
        ContentGraph([_c("a"), _c("b")], edges, [])


def test_rejects_duplicate_quiz_item_id():
    q = QuizItem(id="q1", concept_ids=["a"], difficulty=1, kind="recall")
    with pytest.raises(ValueError, match="duplicate quiz item id: q1"):
        ContentGraph([_c("a")], [], [q, q.model_copy()])


def test_rejects_duplicate_prerequisite_edge():
    edges = [
        Edge(src="a", dst="b", relation=Relation.PREREQUISITE_OF, kind=PrereqKind.FORMAL, strength=1),
        Edge(src="a", dst="b", relation=Relation.PREREQUISITE_OF, kind=PrereqKind.PEDAGOGICAL,
             strength=0.5),
    ]
    with pytest.raises(ValueError, match="duplicate prerequisite_of edge"):
        ContentGraph([_c("a"), _c("b")], edges, [])


def test_rejects_unknown_concept_in_edge():
    edge = Edge(src="a", dst="zzz", relation=Relation.RELATED_TO, strength=0.5)
    with pytest.raises(ValueError, match="unknown"):
        ContentGraph([_c("a")], [edge], [])


def test_direct_prerequisites_sorted_with_kind(graph):
    assert graph.direct_prerequisites("pca") == [
        ("covariance", PrereqKind.FORMAL),
        ("eigenvalue", PrereqKind.FORMAL),
    ]
    assert graph.direct_prerequisites("svd") == [("eigenvalue", PrereqKind.PEDAGOGICAL)]


def test_prerequisite_closure_respects_hops(graph):
    assert graph.prerequisite_closure("pca", max_hops=1) == {"eigenvalue", "covariance"}
    assert graph.prerequisite_closure("pca", max_hops=2) == {
        "eigenvalue", "covariance", "linear_transformation", "matrix", "variance",
    }


def test_downstream_count(graph):
    # eigenvalue -> pca, svd, lora
    assert graph.downstream_count("eigenvalue") == 3
    assert graph.downstream_count("lora") == 0


def test_minimal_dag_is_derived_and_originals_kept(graph):
    dag = graph.derived_minimal_dag()
    assert not dag.has_edge("matrix", "eigenvalue")  # implied by matrix->lt->eigenvalue
    assert ("matrix", PrereqKind.FORMAL) in graph.direct_prerequisites("eigenvalue")


def test_path_supported_uses_prereq_and_related(graph):
    assert graph.path_supported({"pca", "svd", "autoencoder"}, max_hops=3)


def test_path_supported_ignores_part_of(graph):
    # via part_of they are 2 hops apart (vector-linear_algebra-neural_network);
    # via prerequisite/related edges they are 5 hops apart
    assert not graph.path_supported({"vector", "neural_network"}, max_hops=3)


def test_item_lookup(graph):
    assert graph.item("q_pca_eig").concept_ids == ["pca", "eigenvalue"]


def test_items_for_concept_sorted(graph):
    assert [q.id for q in graph.items_for_concept("pca")] == ["q_pca_1", "q_pca_eig"]
    assert [q.id for q in graph.items_for_concept("eigenvalue")] == ["q_eig_1", "q_pca_eig"]
    assert graph.items_for_concept("lora") == []


def test_adjacent_uses_prereq_and_related_edges_only(graph):
    assert graph.adjacent("eigenvalue", "pca") and graph.adjacent("pca", "eigenvalue")
    assert graph.adjacent("pca", "svd")
    assert not graph.adjacent("vector", "linear_algebra")  # part_of
    assert not graph.adjacent("pca", "lora")


def test_item_ids_in_insertion_order(graph):
    assert graph.item_ids == ["q_eig_1", "q_pca_1", "q_pca_eig", "q_cov_1"]
