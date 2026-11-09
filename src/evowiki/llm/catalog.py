"""Concrete model capabilities. No SDK calls or credentials in responses."""
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    key_env: str
    base_url_env: str
    base_url: str
    model_env: str
    models_env: str
    default_model: str


PROVIDERS = {
    "anthropic": ProviderSpec("Anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "https://api.anthropic.com", "EVOWIKI_RUNTIME_MODEL", "EVOWIKI_ANTHROPIC_MODELS", "claude-sonnet-5-5"),
    "deepseek": ProviderSpec("DeepSeek", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "https://api.deepseek.com", "EVOWIKI_DEEPSEEK_MODEL", "EVOWIKI_DEEPSEEK_MODELS", "deepseek-v4-pro"),
    "openai": ProviderSpec("OpenAI", "OPENAI_API_KEY", "OPENAI_BASE_URL", "https://api.openai.com/v1", "EVOWIKI_OPENAI_MODEL", "EVOWIKI_OPENAI_MODELS", "gpt-5.2"),
    "gemini": ProviderSpec("Google Gemini", "GEMINI_API_KEY", "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/", "EVOWIKI_GEMINI_MODEL", "EVOWIKI_GEMINI_MODELS", "gemini-2.5-flash"),
    "kimi": ProviderSpec("Kimi", "MOONSHOT_API_KEY", "MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1", "EVOWIKI_KIMI_MODEL", "EVOWIKI_KIMI_MODELS", "kimi-k3.1-pro"),
    "glm": ProviderSpec("智谱 GLM", "ZAI_API_KEY", "ZAI_BASE_URL", "https://api.z.ai/api/paas/v4/", "EVOWIKI_GLM_MODEL", "EVOWIKI_GLM_MODELS", "glm-5.3"),
    "openai_compatible": ProviderSpec("OpenAI 兼容接口", "EVOWIKI_COMPATIBLE_API_KEY", "EVOWIKI_COMPATIBLE_BASE_URL", "", "EVOWIKI_COMPATIBLE_MODEL", "EVOWIKI_COMPATIBLE_MODELS", ""),
}
LEVEL_NAMES = {"default": "默认", "none": "关闭", "minimal": "极低", "low": "低", "medium": "中", "high": "高", "xhigh": "很高", "max": "最高"}


def configured(provider: str, env: Mapping[str, str]) -> bool:
    s = PROVIDERS[provider]
    return bool(env.get(s.key_env) and (provider != "openai_compatible" or (env.get(s.base_url_env) and env.get(s.model_env))))


def reasoning_levels(provider: str, model: str) -> tuple[str, ...]:
    if provider == "anthropic":
        # All three 5.5 models support five effort levels. Only Haiku also
        # accepts disabled thinking (at high or below); "none" uses that switch.
        # https://platform.claude.com/docs/en/build-with-claude/effort
        if model.startswith("claude-haiku-5-5"):
            return ("none", "low", "medium", "high", "xhigh", "max")
        if model.startswith(("claude-sonnet-5-5", "claude-opus-5-5")):
            return ("low", "medium", "high", "xhigh", "max")
    if provider == "deepseek" and model.startswith("deepseek-v4-"):
        return ("none", "low", "high", "max")
    if provider == "openai":
        if model.startswith("gpt-5.2-pro"):
            return ("medium", "high", "xhigh")
        if model.startswith("gpt-5.2"):
            return ("none", "low", "medium", "high", "xhigh")
    if provider == "gemini":
        # 2.5 maps low/medium/high to 1024/8192/24576 thinking tokens;
        # minimal duplicates low, so it is not a separate control here.
        # https://ai.google.dev/gemini-api/docs/openai
        if model in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
            return ("none", "low", "medium", "high")
        if model == "gemini-2.5-pro":
            return ("low", "medium", "high")
        # Pro 3 has low/high; Pro 3.1 adds medium, while Flash exposes minimal.
        # Do not assign these levels to every future gemini-3* model.
        # https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/get-started-with-gemini-3
        if model == "gemini-3-pro-preview":
            return ("low", "high")
        if model == "gemini-3.1-pro-preview":
            return ("low", "medium", "high")
        if model in ("gemini-3-flash-preview", "gemini-3.1-flash-lite-preview"):
            return ("minimal", "low", "medium", "high")
    if provider == "kimi":
        if model.startswith("kimi-k3"):
            return ("low", "high", "max")
        if model.startswith("kimi-k2.5"):
            return ("none", "high")
        if model.startswith("kimi-k2-thinking"):
            return ("high",)
    if provider == "glm":
        if model.startswith("glm-5.3"):
            return ("low", "high", "max")
        if model.startswith("glm-5.2"):
            return ("none", "high", "max")
        if model.startswith("glm-4.7") or model in ("glm-5", "glm-5.1"):
            return ("none", "high")
    return ("default",)


def supports_images(provider: str, model: str, env: Mapping[str, str] | None = None) -> bool:
    if provider in {"anthropic", "anthropic-think"}:
        return model.startswith("claude-")
    if provider == "openai":
        return model.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "gpt-6", "o3", "o4"))
    if provider == "gemini":
        return model.startswith(("gemini-2", "gemini-3"))
    if provider == "kimi":
        return model.startswith(("kimi-k2.5", "kimi-k3"))
    if provider == "glm":
        return model.startswith("glm-4.6v")
    return provider == "openai_compatible" and (env or {}).get("EVOWIKI_COMPATIBLE_VISION", "").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    levels: tuple[str, ...]
    vision: bool = False

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def default_reasoning(self) -> str:
        return "low" if "low" in self.levels else self.levels[0]


def model_specs(env: Mapping[str, str] | None = None) -> tuple[ModelSpec, ...]:
    env = env or {}
    out = []
    for provider, s in PROVIDERS.items():
        names = [env.get(s.model_env) or s.default_model]
        if provider == "anthropic":
            names += [env.get("EVOWIKI_RUNTIME_FAST_MODEL", "claude-haiku-5-5"), env.get("EVOWIKI_DEEP_MODEL", "claude-sonnet-5-5"), "claude-opus-5-5"]
        elif provider == "deepseek":
            names += [env.get("EVOWIKI_DEEPSEEK_FAST_MODEL", "deepseek-v4-flash")]
        names += env.get(s.models_env, "").split(",")
        if provider == "openai_compatible" and not any(n.strip() for n in names):
            names = ["custom-model"]
        for name in dict.fromkeys(n.strip() for n in names if n.strip()):
            if len(f"{provider}/{name}") > 256 or any(c in name for c in "\r\n\x00"):
                raise ValueError(f"invalid model ID in {s.models_env}")
            out.append(ModelSpec(provider, name, reasoning_levels(provider, name), supports_images(provider, name, env)))
    return tuple(out)
