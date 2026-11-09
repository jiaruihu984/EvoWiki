"""Concrete model selection, separate from provider credentials and usage tiers."""
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from evowiki.llm.catalog import PROVIDERS, ModelSpec, configured, model_specs
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.usage_control import Tier

NO_KEY = "没有配置密钥"
NO_BALANCE = "余额不足，充值后可用"
BAD_KEY = "密钥无效，换一个密钥后可用"
NO_ACCESS = "没有使用权限"


def model_config(spec: ModelSpec, reasoning: str | None = None) -> GatewayConfig:
    ref = ModelRef(provider=spec.provider, model=spec.model,
                   reasoning_effort=reasoning or spec.default_reasoning)
    return GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True)


def runtime_model_configs(env: Mapping[str, str] = os.environ) -> dict[str, dict[Tier, GatewayConfig]]:
    return {spec.id: {"fast": model_config(spec), "good": model_config(spec)}
            for spec in model_specs(env) if configured(spec.provider, env)}


def runtime_writer_configs(env: Mapping[str, str] = os.environ) -> dict[str, GatewayConfig]:
    return {spec.id: model_config(spec) for spec in model_specs(env) if configured(spec.provider, env)}


def default_model(config: GatewayConfig) -> str:
    provider = {"anthropic-think": "anthropic", "deepseek-nothink": "deepseek"}.get(
        config.generator.provider, config.generator.provider)
    return f"{provider}/{config.generator.model}"


@dataclass(frozen=True)
class ModelState:
    id: str
    name: str
    provider: str
    provider_name: str
    levels: tuple[str, ...]
    available: bool
    reason: str = ""


def refusal_reason(attempt: Mapping | None) -> str:
    if attempt is None or attempt.get("error_type") != "ProviderRejectedError":
        return ""
    msg = (attempt.get("error_message") or "").lower()
    if "http 402" in msg or "credit balance" in msg:
        return NO_BALANCE
    if "http 401" in msg:
        return BAD_KEY
    if "http 403" in msg:
        return NO_ACCESS
    return NO_BALANCE if "balance" in msg else NO_ACCESS


def model_states(configs: Mapping[str, object], latest: Callable,
                 catalog: tuple[ModelSpec, ...] | None = None,
                 unavailable: Mapping[str, str] | None = None) -> list[ModelState]:
    out = []
    for spec in catalog if catalog is not None else model_specs():
        reason = (unavailable or {}).get(spec.id, "")
        if spec.id not in configs:
            reason = reason or NO_KEY
        elif not reason:
            reason = refusal_reason(latest((spec.provider,), spec.model))
        out.append(ModelState(spec.id, spec.model, spec.provider, PROVIDERS[spec.provider].name,
                              spec.levels, not reason, reason))
    return out


@dataclass(frozen=True)
class ModelChoice:
    configs: Mapping[str, Mapping[Tier, GatewayConfig]]
    default: str
    catalog: tuple[ModelSpec, ...] = field(default_factory=model_specs)
    unavailable: Mapping[str, str] = field(default_factory=dict)
    health_since: datetime | None = None

    def current(self, picked: str | None) -> str:
        # Never silently send a removed explicit choice to a different vendor.
        return picked if picked else self.default

    def spec(self, model: str) -> ModelSpec | None:
        return next((s for s in self.catalog if s.id == model), None)

    def reasoning(self, model: str, picked: str | None) -> str | None:
        spec = self.spec(model)
        return None if spec is None else picked if picked in spec.levels else spec.default_reasoning
