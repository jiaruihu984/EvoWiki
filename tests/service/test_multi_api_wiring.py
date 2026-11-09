"""Actual PostgreSQL service construction for non-Claude installations and price gates."""
import pytest

from evowiki.llm.catalog import PROVIDERS
from evowiki.llm.usage import Account
from evowiki.service import deps
from evowiki.service.deps import build_services
from tests.runtime.helpers import T0, FixedClock


def test_deleted_default_key_can_be_repaired_by_explicitly_selecting_another_available_model(db_url, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from evowiki.service.app import create_app
    from tests.service.conftest import H, signup

    env = {"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash",
           "EVOWIKI_RUNTIME_PROVIDER": "anthropic", "DEEPSEEK_API_KEY": "fake",
           "EVOWIKI_FORGET_LEDGER": str(tmp_path / "forget.jsonl")}
    providers = {"deepseek": object(), "deepseek-nothink": object()}
    monkeypatch.setattr(deps, "runtime_providers", lambda _: providers)
    monkeypatch.setattr(deps, "runtime_deep_providers", lambda _: providers)
    svc = build_services(env, clock=FixedClock(T0))
    try:
        svc.durable = None  # No workflow is invoked; retain the real memory-reconciliation gate.
        with TestClient(create_app(svc), base_url="http://127.0.0.1") as client:
            signup(client, "learner", lang="en")
            account = Account(user_id="learner", group="assistant")
            with pytest.raises(ValueError, match="默认模型"):
                svc.gateway.route_ref("generator", account)
            assert client.post("/assistant/model", headers=H("learner"),
                               json={"model": "deepseek/deepseek-v4-pro"}).status_code == 200
            assert svc.gateway.route_ref("generator", account).provider == "deepseek"
            assert svc.deep.llm.writer_config("learner").generator.provider == "deepseek"
            # This is routing inspection, with zero provider calls and no budget confirmation.
            assert client.get("/usage/settings", headers=H("learner")).json()["budget_confirmed"] is False
    finally:
        svc.pool.close()


@pytest.mark.parametrize("provider", ["openai", "gemini", "kimi", "glm", "openai_compatible"])
def test_new_provider_only_wires_chat_memory_and_writer(db_url, monkeypatch, tmp_path, provider):
    spec = PROVIDERS[provider]
    env = {"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash", spec.key_env: "fake",
           "EVOWIKI_FORGET_LEDGER": str(tmp_path / "forget.jsonl")}
    if provider in {"kimi", "openai_compatible"}:
        model = spec.default_model or "custom-unit-model"
        env.update({spec.model_env: model, spec.base_url_env: "http://unit.invalid/v1"})
        price_file = tmp_path / "synthetic-prices.toml"
        price_file.write_text(f'[models."{provider}:{model}"]\ninput_per_mtok=1\noutput_per_mtok=2\n', encoding="utf-8")
        env["EVOWIKI_PRICES_FILE"] = str(price_file)
    monkeypatch.setattr(deps, "runtime_providers", lambda _: {provider: object()})
    monkeypatch.setattr(deps, "runtime_deep_providers", lambda _: {provider: object()})
    svc = build_services(env, clock=FixedClock(T0))
    try:
        assert svc.assistant is not None and svc.gateway is not None and svc.deep.llm is not None
        ref = svc.gateway.route_ref("generator", Account(user_id="new", group="assistant"), "assistant.turn")
        assert ref.provider == provider
        writer = svc.deep.llm.writer_config("new").generator
        assert writer.provider == provider and writer.model == ref.model
        assert svc.models.default == f"{provider}/{ref.model}"
    finally:
        svc.pool.close()


def test_unpriced_optional_model_does_not_disable_priced_models(db_url, monkeypatch, tmp_path):
    env = {"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash", "OPENAI_API_KEY": "fake",
           "EVOWIKI_OPENAI_MODELS": "private-unknown-model",
           "EVOWIKI_FORGET_LEDGER": str(tmp_path / "forget.jsonl")}
    monkeypatch.setattr(deps, "runtime_providers", lambda _: {"openai": object()})
    monkeypatch.setattr(deps, "runtime_deep_providers", lambda _: {"openai": object()})
    svc = build_services(env, clock=FixedClock(T0))
    try:
        assert "openai/gpt-5.2" in svc.models.configs
        assert "openai/private-unknown-model" not in svc.models.configs
        assert svc.models.unavailable["openai/private-unknown-model"] == "pricing_missing"
        assert svc.gateway.route_ref("generator", Account(user_id="new", group="assistant")).model == "gpt-5.2"
    finally:
        svc.pool.close()


def test_unpriced_default_never_calls_provider_or_forge(db_url, monkeypatch, tmp_path):
    env = {"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash", "MOONSHOT_API_KEY": "fake",
           "EVOWIKI_FORGET_LEDGER": str(tmp_path / "forget.jsonl")}
    monkeypatch.setattr(deps, "runtime_providers", lambda _: {"kimi": object()})
    monkeypatch.setattr(deps, "runtime_deep_providers", lambda _: {"kimi": object()})
    svc = build_services(env, clock=FixedClock(T0))
    try:
        assert svc.gateway is not None
        assert svc.models.unavailable["kimi/kimi-k3.1-pro"] == "pricing_missing"
        with pytest.raises(ValueError, match="默认模型"):
            svc.gateway.route_ref("generator", Account(user_id="new", group="assistant"))
        with pytest.raises(ValueError, match="重新选择"):
            svc.deep.llm.writer_config("new")
    finally:
        svc.pool.close()
