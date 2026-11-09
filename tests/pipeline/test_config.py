import json

from evowiki.pipeline.config import DEMO_SPINE, default_config, load_config
from evowiki.pipeline.workspace import Workspace


def test_default_config_uses_anthropic_generator_and_deepseek_verifier():
    cfg = default_config({})
    assert (cfg.gateway.generator.provider, cfg.gateway.generator.model) == (
        "anthropic", "claude-opus-5-5")
    assert (cfg.gateway.verifier.provider, cfg.gateway.verifier.model) == (
        "deepseek", "deepseek-v4-pro")
    assert cfg.demo_spine == DEMO_SPINE
    assert default_config({"EVOWIKI_DEEPSEEK_MODEL": "deepseek-x"}).gateway.verifier.model == (
        "deepseek-x")


def test_load_config_overrides_top_level_fields_from_pipeline_json(tmp_path):
    ws = Workspace(tmp_path)
    assert load_config(ws, {}) == default_config({})
    (tmp_path / "pipeline.json").write_text(
        json.dumps({"spot_check_fraction": 0.25, "perspective_concepts": ["pca"]}),
        encoding="utf-8")
    cfg = load_config(ws, {})
    assert cfg.spot_check_fraction == 0.25
    assert cfg.perspective_concepts == ["pca"]
    assert cfg.gateway == default_config({}).gateway
