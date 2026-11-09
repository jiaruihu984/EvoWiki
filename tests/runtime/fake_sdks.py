"""Fake `anthropic` / `openai` SDK modules so runtime_providers can be exercised without the
optional `llm` extra (and without any network)."""

import sys
import types


def install_fake_sdks(monkeypatch) -> dict[str, dict]:
    """Replace the SDK modules in sys.modules; returns {sdk: client kwargs} as clients are made."""
    made: dict[str, dict] = {}

    def recorder(name):
        def make(**kw):
            made[name] = kw
            return f"client:{name}"
        return make

    anthropic = types.ModuleType("anthropic")
    anthropic.Anthropic = recorder("anthropic")
    openai = types.ModuleType("openai")
    openai.OpenAI = recorder("deepseek")
    monkeypatch.setitem(sys.modules, "anthropic", anthropic)
    monkeypatch.setitem(sys.modules, "openai", openai)
    return made
