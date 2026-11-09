import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from evowiki.content.models import LICENSE_TERMS, Concept, Passage, Source
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
GW_CONFIG = GatewayConfig(generator=ModelRef(provider="gen", model="g-1"),
                          verifier=ModelRef(provider="ver", model="v-1"))
FIXTURE_SOURCE = Source(
    id="fx", title="EvoWiki synthetic test notes (authored for tests)", locator="tests://fx",
    license="CC-BY-4.0", license_url="tests://fx/LICENSE", license_verified_at=NOW,
    **LICENSE_TERMS["CC-BY-4.0"].model_dump())


def _c(cid, name, domain, difficulty, aliases=(), zh=None):
    return Concept(id=cid, name_en=name, name_zh=zh or name, domain=domain,
                   difficulty=difficulty, aliases=list(aliases))


MINI_CONCEPTS = [
    _c("vector", "Vector", "la", 1, zh="向量"),
    _c("matrix", "Matrix", "la", 1, zh="矩阵"),
    _c("eigenvalue", "Eigenvalue", "la", 3, ["eigenvector"], zh="特征值"),
    _c("variance", "Variance", "prob", 2, zh="方差"),
    _c("covariance", "Covariance Matrix", "prob", 3, ["covariance"], zh="协方差矩阵"),
    _c("pca", "Principal Component Analysis", "ml", 4, ["PCA"], zh="主成分分析"),
    _c("svd", "Singular Value Decomposition", "la", 4, ["SVD"], zh="奇异值分解"),
    _c("neural_network", "Neural Network", "dl", 3, zh="神经网络"),
    _c("autoencoder", "Autoencoder", "dl", 4, zh="自编码器"),
    _c("lora", "LoRA", "dl", 5, ["low-rank adaptation"], zh="低秩适配"),
]

_P = {
    "p_vec": "A vector is an ordered list of numbers that can be added and scaled.",
    "p_mat": "A matrix is a rectangular array of numbers; multiplying a matrix by a vector "
             "applies a linear map.",
    "p_eig": "An eigenvector of a matrix is only scaled by the matrix, and the scale factor is "
             "its eigenvalue.",
    "p_cov": "The covariance matrix collects the variance of each feature on its diagonal and "
             "the covariance between features off the diagonal.",
    "p_pca1": "Principal component analysis (PCA) projects data onto the eigenvectors of the "
              "covariance matrix with the largest eigenvalues.",
    "p_pca2": "PCA can be viewed as finding orthogonal directions of maximal variance.",
    "p_svd": "The singular value decomposition (SVD) factors any matrix; PCA can be computed "
             "from the SVD of centered data.",
    "p_nn": "A neural network composes linear maps given by a matrix with nonlinear "
            "activations.",
    "p_ae": "An autoencoder is a neural network trained to reconstruct its input through a "
            "low-dimensional code; a linear autoencoder learns the same subspace as PCA.",
    "p_lora": "LoRA (low-rank adaptation) freezes the pretrained weights of a neural network "
              "and learns a low-rank update, related to a truncated SVD.",
}
MINI_PASSAGES = [Passage(id=pid, source_id="fx", locator=f"tests://fx#{pid}", text=t)
                 for pid, t in _P.items()]

Table = Mapping[tuple[str, str], dict[str, Any] | Callable[[LLMRequest], dict[str, Any]]]


def respond(table: Table) -> Callable[[str, LLMRequest], str]:
    def responder(model: str, req: LLMRequest) -> str:
        key = (req.purpose, req.subject)
        if key not in table:
            raise KeyError(f"no scripted response for {key}")
        value = table[key]
        return json.dumps(value(req) if callable(value) else value, ensure_ascii=False)

    return responder
