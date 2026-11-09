"""Fast / good tier configs for the runtime gateway (plan rev 3.2 §9.4). good = the runtime
config as before; fast = cheaper models, overridable by env. In DeepSeek mode the fast tier is
the same model on the non-thinking provider (DEEPSEEK_NOTHINK), the good tier the thinking one."""

import os
from collections.abc import Mapping

from evowiki.llm.catalog import ModelSpec, reasoning_levels
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.providers import ENV_KEYS
from evowiki.llm.usage_control import Tier
from evowiki.runtime.llm import ANTHROPIC_THINK, DEEPSEEK_NOTHINK, runtime_gateway_config

DEFAULT_IMPROVER_MODEL = "claude-opus-5-5"          # report 2026-11-05-rsi-why: 7/9 effective patches, Sonnet 5/9
DEFAULT_RUNTIME_FAST_MODEL = "claude-haiku-4-5"
DEFAULT_DEEPSEEK_FAST_MODEL = "deepseek-v4-flash"  # anthropic-mode fast verifier
DEFAULT_DEEP_MODEL = "claude-sonnet-5-5"            # deep refine writer (plan rev 3.3 §10.2)


def runtime_tier_configs(env: Mapping[str, str] = os.environ) -> dict[Tier, GatewayConfig]:
    good = runtime_gateway_config(env)
    if good.generator.provider == "deepseek":
        # Fast = the good tier's model with thinking off (only the thinking mode differs);
        # EVOWIKI_DEEPSEEK_FAST_MODEL still overrides the model.
        nothink = ModelRef(provider=DEEPSEEK_NOTHINK,
                           model=env.get("EVOWIKI_DEEPSEEK_FAST_MODEL", good.generator.model))
        fast = GatewayConfig(generator=nothink, verifier=nothink, allow_single_provider=True)
    else:
        if good.generator.provider != "anthropic":
            return {"fast": _concrete_config(good.generator), "good": _concrete_config(good.generator)}
        flash = ModelRef(provider="deepseek",
                         model=env.get("EVOWIKI_DEEPSEEK_FAST_MODEL",
                                       DEFAULT_DEEPSEEK_FAST_MODEL))
        ref = ModelRef(provider="anthropic", model=env.get("EVOWIKI_RUNTIME_FAST_MODEL",
                                                         DEFAULT_RUNTIME_FAST_MODEL))
        fast = GatewayConfig(generator=ref, verifier=ref if good.single_provider else flash,
                             allow_single_provider=good.single_provider)
    return {"fast": fast, "good": good}


def _concrete_config(ref: ModelRef) -> GatewayConfig:
    spec = ModelSpec(ref.provider, ref.model, reasoning_levels(ref.provider, ref.model))
    selected = ref.model_copy(update={"reasoning_effort": spec.default_reasoning})
    return GatewayConfig(generator=selected, verifier=selected, allow_single_provider=True)


def _deep_deepseek(env: Mapping[str, str]) -> ModelRef:
    """The page writer on DeepSeek: thinking off (DEEPSEEK_NOTHINK, the deep tier's own provider,
    `runtime.llm.runtime_deep_providers`; user ruling 2026-10-28)."""
    return ModelRef(provider=DEEPSEEK_NOTHINK,
                    model=env.get("EVOWIKI_DEEPSEEK_MODEL", "deepseek-v4-pro"))


def runtime_deep_config(env: Mapping[str, str] = os.environ) -> GatewayConfig:
    """The page writer follows the same resolved provider as the main gateway, never thinking.
    Claude uses EVOWIKI_DEEP_MODEL (lowest effort); its verifier is DeepSeek when configured,
    otherwise Claude. DeepSeek uses its no-thinking provider for both roles."""
    deepseek = _deep_deepseek(env)
    resolved = runtime_gateway_config(env).generator
    if resolved.provider not in ("anthropic", "deepseek"):
        return _concrete_config(resolved)
    if resolved.provider == "anthropic":
        claude = ModelRef(provider="anthropic", model=env.get("EVOWIKI_DEEP_MODEL",
                                                              DEFAULT_DEEP_MODEL))
        if env.get(ENV_KEYS["deepseek"]):
            return GatewayConfig(generator=claude, verifier=deepseek)
        return GatewayConfig(generator=claude, verifier=claude, allow_single_provider=True)
    return GatewayConfig(generator=deepseek, verifier=deepseek, allow_single_provider=True)


def runtime_visual_config(env: Mapping[str, str] = os.environ) -> GatewayConfig | None:
    """The model of the visualization step (`deep.visual`): EVOWIKI_VISUAL_MODEL, on the deep tier's providers, when
    set and an Anthropic key is there; None = the writer's model draws too."""
    model = env.get("EVOWIKI_VISUAL_MODEL")
    if not model or not env.get(ENV_KEYS["anthropic"]):
        return None
    ref = ModelRef(provider="anthropic", model=model)
    return GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True)


def runtime_improver_ref(env: Mapping[str, str] = os.environ) -> ModelRef | None:
    """The harness improver's own model (`harness.improver`): EVOWIKI_IMPROVER_MODEL, thinking on, whatever the product
    runs on (user ruling 2026-11-05: never the product's cheap model). None without an Anthropic key: the good tier."""
    if not env.get(ENV_KEYS["anthropic"]):
        return None
    return ModelRef(provider=ANTHROPIC_THINK, model=env.get("EVOWIKI_IMPROVER_MODEL", DEFAULT_IMPROVER_MODEL))


def runtime_deep_fallback_config(env: Mapping[str, str] = os.environ) -> GatewayConfig | None:
    """The page writer's fallback: when Claude is selected and both keys are set, Claude writes and
    DeepSeek (thinking off, as the writer) takes over when Claude is unavailable (429 / 5xx,
    unreachable, rejected). Without the Anthropic key DeepSeek is already the writer: no
    fallback."""
    if not (env.get(ENV_KEYS["anthropic"]) and env.get(ENV_KEYS["deepseek"])):
        return None
    if runtime_gateway_config(env).generator.provider != "anthropic":
        return None
    deepseek = _deep_deepseek(env)
    return GatewayConfig(generator=deepseek, verifier=deepseek, allow_single_provider=True)
