import math

from evowiki.retrieval.embed import Embedder, HashingEmbedder, embedder_from_env


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hashing_embedder_is_deterministic_normalized_and_lexically_similar():
    e = HashingEmbedder()
    assert isinstance(e, Embedder) and e.dim == 384 and e.model_version == "hash-v1-d384"
    a, b, c, empty, zh = e.embed_documents(["principal component analysis of variance",
                                            "variance and principal components",
                                            "low rank adapters for transformers", "",
                                            "主成分分析"])
    assert e.embed_query("principal component analysis of variance") == a
    for v in (a, b, c, empty, zh):
        assert len(v) == 384 and math.isclose(math.sqrt(_dot(v, v)), 1.0, rel_tol=1e-9)
    assert _dot(a, b) > _dot(a, c)
    assert _dot(zh, e.embed_query("主成分")) > _dot(zh, e.embed_query("低秩"))
    assert HashingEmbedder(dim=16).dim == 16


def test_embedder_from_env_is_multilingual_by_default():
    assert isinstance(embedder_from_env({"EVOWIKI_EMBEDDER": "hash"}), HashingEmbedder)
    default = embedder_from_env({})  # lazy: nothing is downloaded here
    assert default.id == "fastembed:sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    assert embedder_from_env({"EVOWIKI_EMBED_MODEL": "Qwen/Qwen3-Embedding-0.6B-Q"}).id \
        == "fastembed:Qwen/Qwen3-Embedding-0.6B-Q"
