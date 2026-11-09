from evowiki.sources.search import bm25_rank, rrf_ids


def test_bm25_rank_scores_ids_and_drops_zero():
    docs = [("a", "矩阵乘法的定义"), ("b", "完全无关的句子"), ("c", "矩阵 乘法 例子")]
    out = bm25_rank(docs, "矩阵乘法", k=5)
    assert [i for i, _ in out][:2] in (["a", "c"], ["c", "a"])
    assert "b" not in [i for i, _ in out]


def test_rrf_ids_is_deterministic_on_ties():
    assert [i for i, _ in rrf_ids([["x", "y"], ["y", "x"]], k=2)] == ["x", "y"]
