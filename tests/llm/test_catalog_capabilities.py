"""Model-specific controls from the official sources linked in catalog.py."""

import pytest

from evowiki.llm.catalog import reasoning_levels, supports_images


@pytest.mark.parametrize(("model", "levels"), [
    ("gemini-2.5-flash", ("none", "low", "medium", "high")),
    ("gemini-2.5-pro", ("low", "medium", "high")),
    ("gemini-3-pro-preview", ("low", "high")),
    ("gemini-3.1-pro-preview", ("low", "medium", "high")),
    ("gemini-3-flash-preview", ("minimal", "low", "medium", "high")),
    ("gemini-3.1-flash-lite-preview", ("minimal", "low", "medium", "high")),
    ("gemini-3-unknown-future", ("default",)),
    ("gemini-3.8-unverified", ("default",)),
])
def test_gemini_models_have_distinct_verified_controls(model, levels):
    assert reasoning_levels("gemini", model) == levels


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"])
def test_claude_55_verified_effort_levels_and_model_specific_disable(model):
    levels = reasoning_levels("anthropic", model)
    assert tuple(level for level in levels if level != "none") == ("low", "medium", "high", "xhigh", "max")
    assert ("none" in levels) == (model == "claude-haiku-5-5")


def test_glm_default_remains_text_only_and_unknown_model_gets_no_invented_effort():
    assert reasoning_levels("glm", "glm-5.3") == ("low", "high", "max")
    assert not supports_images("glm", "glm-5.3")
    assert reasoning_levels("glm", "glm-5.9") == ("default",)
    assert reasoning_levels("openai", "gpt-6-unverified") == ("default",)
