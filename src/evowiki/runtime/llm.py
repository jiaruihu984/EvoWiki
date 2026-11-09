import logging
import os
from collections.abc import Callable, Collection, Mapping
from types import MappingProxyType

from evowiki.llm.catalog import PROVIDERS, configured
from evowiki.llm.deadline import Hooks
from evowiki.llm.gateway import Provider
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.openai_compatible import OpenAICompatibleProvider
from evowiki.llm.providers import (
    DEEPSEEK_BASE_URL,
    ENV_KEYS,
    AnthropicProvider,
    DeepSeekProvider,
    deepseek_strict,
)
from evowiki.llm.usage_control import Tier

log = logging.getLogger("evowiki.llm")
DEFAULT_RUNTIME_MODEL = "claude-sonnet-5-5"  # latency budget (spec §5.8) over Opus depth
# Per-request timeouts for runtime calls, by tier (deep.* purposes get their own, longer
# budget). Each request sends its timeout to the SDK (an httpx per-phase timeout, which a
# server trickling keep-alive bytes never trips) AND runs under the same value as a TOTAL
# wall-clock deadline (llm.deadline): past it the request is aborted and raises
# ProviderTransientError (audited "provider_error", never retried by the gateway). The SDK
# clients never retry (max_retries=0, also forced on every deadline-bounded request) and carry
# the deep timeout only as a ceiling. HTTP 429 alone is waited out by the gateway (at most 3 waits of
# at most 30 s, a 429 itself comes back at once). Otherwise, per gateway call: (timeout + ABORT_GRACE_S) x
# RUNTIME_RECORD_MAX_ATTEMPTS (1); extraction purposes are never "deep.", so <= 182 s, under the
# 5-minute extraction-job lease (store.evidence.LEASE). Retries (and provider fallbacks, e.g.
# the deep writer's) are owned by the gateway / caller layer, never the SDK, and each one is a
# new call with its own deadline.
RUNTIME_TIMEOUT_FAST_S = 60.0
RUNTIME_TIMEOUT_GOOD_S = 180.0
RUNTIME_TIMEOUT_DEEP_S = 300.0
DEEP_PURPOSE_PREFIX = "deep."
# Runtime gateways never use the pipeline's record-mode retries.
RUNTIME_RECORD_MAX_ATTEMPTS = 1
# Runtime DeepSeek output budget, passed explicitly so it ignores the pipeline's
# EVOWIKI_DEEPSEEK_MAX_TOKENS / 32768 default (bounded latency over long reasoning).
RUNTIME_DEEPSEEK_MAX_TOKENS = 8192          # fast tier (and the anthropic-mode verifier)
RUNTIME_DEEPSEEK_GOOD_MAX_TOKENS = 16384    # good tier in DeepSeek mode (thinking on)
# DeepSeek mode runs two providers over the same API key: "deepseek" with thinking on (good
# tier) and this one with thinking off (fast tier). The distinct provider name keeps the tiers
# apart in input hashes, CallLog, usage records and the price table.
DEEPSEEK_NOTHINK = "deepseek-nothink"
# The assistant composer's 思考 level for Claude (runtime.models): the same model and key at a higher
# effort (EVOWIKI_RUNTIME_THINK_EFFORT, default "high"; the plain "anthropic" provider keeps
# EVOWIKI_RUNTIME_EFFORT, default "low" = 快速). A distinct provider name, as DEEPSEEK_NOTHINK, keeps the
# levels apart in input hashes, CallLog, usage records and the price table.
ANTHROPIC_THINK = "anthropic-think"
RUNTIME_ENV_KEYS: dict[str, str] = {**{name: spec.key_env for name, spec in PROVIDERS.items()},
                                    DEEPSEEK_NOTHINK: ENV_KEYS["deepseek"],
                                    ANTHROPIC_THINK: ENV_KEYS["anthropic"]}
# DeepSeek's reasoning_effort for the thinking providers, by purpose (others: the API's default,
# "high"). None today: deep.plan (low) is gone since the writer plans in its own loop (stage 4), and
# the writer (deep.write, a chat) does not think at all (DEEPSEEK_NOTHINK, 2026-10-28).
EFFORT_BY_PURPOSE: Mapping[str, str] = MappingProxyType({})


def effort_for(purpose: str) -> str | None:
    return EFFORT_BY_PURPOSE.get(purpose)


def runtime_timeout(purpose: str, tier: Tier) -> float:
    """Per-request SDK timeout in seconds for a runtime call."""
    if purpose.startswith(DEEP_PURPOSE_PREFIX):
        return RUNTIME_TIMEOUT_DEEP_S
    return RUNTIME_TIMEOUT_GOOD_S if tier == "good" else RUNTIME_TIMEOUT_FAST_S


def runtime_gateway_config(env: Mapping[str, str] = os.environ) -> GatewayConfig:
    deepseek = ModelRef(provider="deepseek",
                        model=env.get("EVOWIKI_DEEPSEEK_MODEL", "deepseek-v4-pro"))
    mode = env.get("EVOWIKI_RUNTIME_PROVIDER", "auto") or "auto"
    if mode == "auto":
        mode = next((name for name in PROVIDERS if configured(name, env)), "anthropic")
    if mode == "anthropic":
        generator = ModelRef(provider="anthropic", model=env.get("EVOWIKI_RUNTIME_MODEL", DEFAULT_RUNTIME_MODEL))
        single = bool(env.get("ANTHROPIC_API_KEY")) and not env.get("DEEPSEEK_API_KEY")
        return GatewayConfig(generator=generator, verifier=generator if single else deepseek,
                             allow_single_provider=single)
    if mode == "deepseek":
        # User-approved spec §8 downgrade (single-provider mode, as in the content pipeline):
        # generator and verifier share DeepSeek, so verification is weaker evidence.
        return GatewayConfig(generator=deepseek, verifier=deepseek, allow_single_provider=True)
    if mode in PROVIDERS:
        spec = PROVIDERS[mode]
        model = (env.get(spec.model_env) or spec.default_model).strip()
        if not model:
            raise ValueError(f"EVOWIKI_RUNTIME_PROVIDER={mode} needs {spec.model_env}")
        generator = ModelRef(provider=mode, model=model)
        return GatewayConfig(generator=generator, verifier=generator, allow_single_provider=True)
    raise ValueError(f"EVOWIKI_RUNTIME_PROVIDER must be auto or one of {', '.join(PROVIDERS)}, got {mode!r}")


def check_runtime_provider(env: Mapping[str, str] = os.environ) -> None:
    """Validate configuration while keeping the local setup UI available without a key.

    Missing providers stay absent from runtime_providers, so paid requests fail closed.
    """
    runtime_gateway_config(env)
    if env.get("EVOWIKI_RUNTIME_PROVIDER") == "openai_compatible":
        spec = PROVIDERS["openai_compatible"]
        if not env.get(spec.base_url_env):
            raise ValueError(f"EVOWIKI_RUNTIME_PROVIDER=openai_compatible needs {spec.base_url_env}")


def runtime_providers(env: Mapping[str, str] = os.environ) -> dict[str, Provider]:
    """Providers whose key is set and whose SDK (extra `llm`) is installed; others are skipped,
    so the service degrades instead of crashing (spec §5.7).

    DeepSeek mode: "deepseek" thinks (good tier, RUNTIME_DEEPSEEK_GOOD_MAX_TOKENS) and
    DEEPSEEK_NOTHINK does not (fast tier, RUNTIME_DEEPSEEK_MAX_TOKENS). Anthropic mode: the
    DeepSeek verifier is unchanged (no thinking switch sent, RUNTIME_DEEPSEEK_MAX_TOKENS)."""
    def timeout_for(tier: Tier) -> Callable[[str], float]:
        return lambda purpose: runtime_timeout(purpose, tier)

    factories = {
        "anthropic": lambda: AnthropicProvider(client=_anthropic_client(env),
                                               effort=env.get("EVOWIKI_RUNTIME_EFFORT", "low"),
                                               timeout_for=timeout_for("good"),
                                               http_client_factory=_anthropic_http),
        "deepseek": lambda: DeepSeekProvider(client=_deepseek_client(env),
                                              max_tokens=RUNTIME_DEEPSEEK_MAX_TOKENS,
                                              timeout_for=timeout_for("good"),
                                              http_client_factory=_openai_http,
                                              strict=deepseek_strict(env)),
        ANTHROPIC_THINK: lambda: AnthropicProvider(
            client=_anthropic_client(env), effort=env.get("EVOWIKI_RUNTIME_THINK_EFFORT", "high"),
            timeout_for=timeout_for("good"), http_client_factory=_anthropic_http),
    }
    if runtime_gateway_config(env).generator.provider == "deepseek":
        factories["deepseek"] = lambda: DeepSeekProvider(
            strict=deepseek_strict(env),
            client=_deepseek_client(env), max_tokens=RUNTIME_DEEPSEEK_GOOD_MAX_TOKENS,
            thinking=True, timeout_for=timeout_for("good"), http_client_factory=_openai_http,
            effort_for=effort_for)
    # thinking off (DeepSeek mode's fast tier); in anthropic mode too, for a user who picks
    # DeepSeek in the assistant composer (runtime.models)
    # chat_thinking=False: its chat turns (the assistant's 快速, the composer picker's fast level)
    # say thinking "disabled" too; without it chat sent no switch and DeepSeek thought by default
    factories[DEEPSEEK_NOTHINK] = lambda: DeepSeekProvider(
        strict=deepseek_strict(env),
        client=_deepseek_client(env), max_tokens=RUNTIME_DEEPSEEK_MAX_TOKENS,
        thinking=False, chat_thinking=False, timeout_for=timeout_for("fast"),
        http_client_factory=_openai_http)
    for name in PROVIDERS.keys() - {"anthropic", "deepseek"}:
        factories[name] = lambda name=name: _compatible_provider(name, env, timeout_for("good"))
    out: dict[str, Provider] = {}
    for name, make in factories.items():
        if not env.get(RUNTIME_ENV_KEYS[name]):
            continue
        if name in PROVIDERS and not configured(name, env):
            continue
        try:
            out[name] = make()
        except ImportError as exc:
            log.warning("provider %s skipped: SDK not installed (%s); `uv sync --extra llm`",
                        name, exc)
    return out


# Deep refine (plan rev 3.3 §10.2) runs long agent turns (sections, standalone HTML). Its purposes
# are all "deep.", so each request gets RUNTIME_TIMEOUT_DEEP_S; no SDK retries (as every runtime
# client): a failed call is a failed attempt of the job.
DEEP_DEEPSEEK_MAX_TOKENS = 32768


DEEP_EFFORT = "low"       # the page writer on Claude: the lowest effort (EVOWIKI_DEEP_EFFORT overrides)


def runtime_deep_providers(env: Mapping[str, str] = os.environ) -> dict[str, Provider]:
    """Providers for the deep tier (`runtime.usage.runtime_deep_config`). The page writer thinks
    on neither model (user ruling 2026-10-28: 「不要开思考，写页面全部开最低就够了吧」): Anthropic at
    the lowest effort (DEEP_EFFORT; this model rejects thinking "disabled", low effort is the
    off switch, `AnthropicProvider._effort_for_request`), DeepSeek on DEEPSEEK_NOTHINK, whose chat
    requests send thinking "disabled" (`chat_thinking=False`). "deepseek" (thinking on) stays for
    a harness pack that picks the good tier for a deep step (H10). Same skipping rules as
    runtime_providers. ANTHROPIC_THINK is available for RSI forge coders that pin the improver
    model on this gateway; page writing still uses its own no-thinking config."""
    def deep_timeout(purpose: str) -> float:
        return runtime_timeout(purpose, "good")

    factories = {
        "anthropic": lambda: AnthropicProvider(
            client=_anthropic_client(env), effort=env.get("EVOWIKI_DEEP_EFFORT", DEEP_EFFORT),
            timeout_for=deep_timeout, http_client_factory=_anthropic_http),
        ANTHROPIC_THINK: lambda: AnthropicProvider(
            client=_anthropic_client(env), effort=env.get("EVOWIKI_RUNTIME_THINK_EFFORT", "high"),
            timeout_for=deep_timeout, http_client_factory=_anthropic_http),
        "deepseek": lambda: DeepSeekProvider(
            strict=deepseek_strict(env),
            client=_deepseek_client(env), max_tokens=DEEP_DEEPSEEK_MAX_TOKENS, thinking=True,
            timeout_for=deep_timeout, http_client_factory=_openai_http, effort_for=effort_for),
        DEEPSEEK_NOTHINK: lambda: DeepSeekProvider(
            strict=deepseek_strict(env),
            client=_deepseek_client(env), max_tokens=DEEP_DEEPSEEK_MAX_TOKENS, thinking=False,
            chat_thinking=False, timeout_for=deep_timeout, http_client_factory=_openai_http),
    }
    for name in PROVIDERS.keys() - {"anthropic", "deepseek"}:
        factories[name] = lambda name=name: _compatible_provider(name, env, deep_timeout)
    out: dict[str, Provider] = {}
    for name, make in factories.items():
        if not env.get(RUNTIME_ENV_KEYS[name]):
            continue
        if name in PROVIDERS and not configured(name, env):
            continue
        try:
            out[name] = make()
        except ImportError as exc:
            log.warning("deep provider %s skipped: SDK not installed (%s)", name, exc)
    return out


def _anthropic_http(timeout: float, event_hooks: Hooks):
    """The SDK's default httpx client (its limits, redirects, env proxies) for ONE
    deadline-bounded request (llm.deadline)."""
    import anthropic  # optional extra `llm`

    return anthropic.DefaultHttpxClient(timeout=timeout, event_hooks=event_hooks)


def _openai_http(timeout: float, event_hooks: Hooks):
    """As _anthropic_http, for the OpenAI-compatible DeepSeek client."""
    from openai import DefaultHttpxClient  # optional extra `llm`

    return DefaultHttpxClient(timeout=timeout, event_hooks=event_hooks)


def _anthropic_client(env: Mapping[str, str]):
    import anthropic  # optional extra `llm`

    base = {"base_url": env["ANTHROPIC_BASE_URL"]} if env.get("ANTHROPIC_BASE_URL") else {}
    return anthropic.Anthropic(api_key=env[ENV_KEYS["anthropic"]], **base,
                               timeout=RUNTIME_TIMEOUT_DEEP_S, max_retries=0)


def _deepseek_client(env: Mapping[str, str]):
    from openai import OpenAI  # optional extra `llm`; DeepSeek is OpenAI-compatible

    return OpenAI(api_key=env[ENV_KEYS["deepseek"]], base_url=env.get("DEEPSEEK_BASE_URL") or DEEPSEEK_BASE_URL,
                  timeout=RUNTIME_TIMEOUT_DEEP_S, max_retries=0)


def _compatible_provider(name: str, env: Mapping[str, str],
                         timeout_for: Callable[[str], float]) -> OpenAICompatibleProvider:
    spec = PROVIDERS[name]
    return OpenAICompatibleProvider(name=name, env=env,
                                    base_url=env.get(spec.base_url_env) or spec.base_url,
                                    timeout_for=timeout_for, http_client_factory=_openai_http)


def missing_runtime_providers(config: GatewayConfig, available: Collection[str]) -> list[str]:
    """Providers a configured role (generator / verifier) needs but that are not available."""
    return sorted({config.generator.provider, config.verifier.provider} - set(available))


def provider_config_problem(config: GatewayConfig, available: Collection[str]) -> str | None:
    """Operator-facing explanation of a missing runtime provider, or None if all are present.
    Names env vars only, never their values."""
    missing = missing_runtime_providers(config, available)
    if not missing:
        return None
    keys = " and ".join(dict.fromkeys(RUNTIME_ENV_KEYS.get(p, f"credentials for {p}") for p in missing))
    msg = (f"runtime LLM provider(s) {missing} not available (generator "
           f"{config.generator.provider}:{config.generator.model}, verifier "
           f"{config.verifier.provider}:{config.verifier.model}). Set {keys} "
           "(and `uv sync --extra llm`)")
    if not config.single_provider and "deepseek" in available:
        msg += (", or set EVOWIKI_RUNTIME_PROVIDER=deepseek for the user-approved "
                "single-provider dev mode (spec §8 downgrade)")
    return msg + "."
