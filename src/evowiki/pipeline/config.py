import json
import os
from collections.abc import Mapping

from pydantic import BaseModel, Field

from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.pipeline.workspace import Workspace

DEMO_SPINE = ["vector", "matrix", "linear_transformation", "eigenvalue", "eigendecomposition",
              "variance", "covariance", "pca", "svd", "rank", "low_rank_approximation",
              "dimensionality_reduction", "neural_network", "autoencoder",
              "representation_learning", "fine_tuning", "lora"]
SPINE_PATHS = [["covariance", "pca", "autoencoder"], ["pca", "svd", "lora"],
               ["low_rank_approximation", "svd", "pca"], ["eigendecomposition", "pca"],
               ["covariance", "pca"], ["dimensionality_reduction", "pca"],
               ["pca", "autoencoder", "representation_learning"],
               ["eigendecomposition", "svd"], ["low_rank_approximation", "lora"],
               ["svd", "lora", "fine_tuning"]]
PERSPECTIVE_CONCEPTS = ["pca", "svd", "autoencoder", "lora"]


class PipelineConfig(BaseModel):
    gateway: GatewayConfig
    spot_check_fraction: float = Field(default=0.1, ge=0.0, le=1.0)
    retrieval_k: int = Field(default=8, ge=1)
    demo_spine: list[str] = Field(default_factory=lambda: list(DEMO_SPINE))
    spine_paths: list[list[str]] = Field(default_factory=lambda: [list(p) for p in SPINE_PATHS])
    perspective_concepts: list[str] = Field(default_factory=lambda: list(PERSPECTIVE_CONCEPTS))


def default_config(env: Mapping[str, str] = os.environ) -> PipelineConfig:
    return PipelineConfig(gateway=GatewayConfig(
        generator=ModelRef(provider="anthropic", model="claude-opus-5-5"),
        verifier=ModelRef(provider="deepseek",
                          model=env.get("EVOWIKI_DEEPSEEK_MODEL", "deepseek-v4-pro"))))


def load_config(ws: Workspace, env: Mapping[str, str] = os.environ) -> PipelineConfig:
    base = default_config(env).model_dump(mode="json")
    path = ws.root / "pipeline.json"
    if path.exists():
        base.update(json.loads(path.read_text(encoding="utf-8")))
    return PipelineConfig.model_validate(base)
