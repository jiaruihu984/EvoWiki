"""Concrete model/reasoning selection over disposable PostgreSQL; never calls an API."""
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from evowiki.llm.catalog import model_specs
from evowiki.llm.usage import Account
from evowiki.runtime.models import NO_BALANCE, NO_KEY, ModelChoice
from evowiki.service.app import create_app
from evowiki.store.usage import PgUsageMeter, chosen_models, set_model_choice
from tests.runtime.helpers import T0, FixedClock
from tests.service.conftest import H

DS = "deepseek/deepseek-v4-pro"
CLAUDE = "anthropic/claude-sonnet-5-5"
OPENAI = "openai/gpt-5.2"
CUSTOM = "openai_compatible/test-model"
GLM = "glm/glm-5.3"


@pytest.fixture
def api(services):
    catalog = model_specs({"EVOWIKI_COMPATIBLE_MODEL": "test-model"})
    services.models = ModelChoice({DS: {}, CLAUDE: {}, OPENAI: {}, CUSTOM: {}, GLM: {}}, DS, catalog)
    return TestClient(create_app(services), base_url="http://127.0.0.1")


def _attempt(pool, provider, model, error_type, message, n):
    with pool.connection() as conn:
        conn.execute(
            "insert into llm_attempt (id, input_hash, purpose, prompt_version, subject, provider, "
            "model, params, status, error_type, error_message, latency, ts) values "
            "(%s, 'h', 'assistant.turn', 'v', 's', %s, %s, %s, %s, %s, %s, 1, %s)",
            (f"att{n}", provider, model, Jsonb({}), "provider_error" if error_type else "accepted",
             error_type, message, T0.replace(minute=n)))


def _by_id(body):
    return {m["id"]: m for m in body["models"]}


def test_catalog_has_separate_provider_model_and_reasoning(api):
    body = api.get("/assistant/models", headers=H("u1")).json()
    assert (body["current"], body["reasoning"]) == (DS, "low")
    rows = _by_id(body)
    assert {m["provider"] for m in rows.values()} == {
        "anthropic", "deepseek", "openai", "gemini", "kimi", "glm", "openai_compatible"}
    assert rows[OPENAI] == {
        "id": OPENAI, "name": "gpt-5.2", "provider": "openai", "provider_name": "OpenAI",
        "available": True, "reason": "", "levels": [
            {"reasoning": "none", "name": "关闭"}, {"reasoning": "low", "name": "低"},
            {"reasoning": "medium", "name": "中"}, {"reasoning": "high", "name": "高"},
            {"reasoning": "xhigh", "name": "很高"}]}
    assert rows[CUSTOM]["levels"] == [{"reasoning": "default", "name": "默认"}]
    assert [level["reasoning"] for level in rows[GLM]["levels"]] == ["low", "high", "max"]
    assert rows[GLM]["available"]
    assert "tier" not in body


def test_pick_persists_model_and_reasoning_for_only_this_user(api, services):
    response = api.post("/assistant/model", json={"model": OPENAI, "reasoning": "xhigh"}, headers=H("u1"))
    assert response.status_code == 200, response.text
    assert (response.json()["current"], response.json()["reasoning"]) == (OPENAI, "xhigh")
    assert api.get("/assistant/models", headers=H("u1")).json()["reasoning"] == "xhigh"
    other = api.get("/assistant/models", headers=H("u2")).json()
    assert (other["current"], other["reasoning"]) == (DS, "low")
    meter = PgUsageMeter(services.pool, clock=FixedClock(T0), tz=UTC)
    account = Account(user_id="u1", group="assistant")
    assert meter.chosen_model(account) == OPENAI
    assert meter.chosen_reasoning(account) == "xhigh"
    assert meter.chosen_reasoning(Account(user_id="u2", group="assistant")) is None
    with services.pool.connection() as conn:
        assert chosen_models(conn, "u1") == {"assistant": OPENAI, "reasoning": "xhigh"}


def test_reasoning_only_validates_current_and_model_switch_resets_unsupported_level(api):
    assert api.post("/assistant/model", json={"reasoning": "max"}, headers=H("u1")).status_code == 200
    response = api.post("/assistant/model", json={"model": OPENAI}, headers=H("u1"))
    assert (response.json()["current"], response.json()["reasoning"]) == (OPENAI, "low")
    response = api.post("/assistant/model", json={"model": CUSTOM}, headers=H("u1"))
    assert (response.json()["current"], response.json()["reasoning"]) == (CUSTOM, "default")


@pytest.mark.parametrize("body,status", [({}, 422), ({"tier": "fast"}, 422),
    ({"model": "gpt"}, 404), ({"model": OPENAI, "reasoning": "max"}, 422),
    ({"reasoning": "invalid"}, 422)])
def test_invalid_request_never_half_writes_choice(api, services, body, status):
    assert api.post("/assistant/model", json={"model": CLAUDE, "reasoning": "high"}, headers=H("u1")).status_code == 200
    response = api.post("/assistant/model", json=body, headers=H("u1"))
    assert response.status_code == status, response.text
    with services.pool.connection() as conn:
        assert chosen_models(conn, "u1") == {"assistant": CLAUDE, "reasoning": "high"}


def test_refusal_is_scoped_to_concrete_model_and_later_success_clears_it(api, services):
    _attempt(services.pool, "anthropic", "claude-sonnet-5-5", "ProviderRejectedError", "HTTP 402", 1)
    rows = _by_id(api.get("/assistant/models", headers=H("u1")).json())
    assert (rows[CLAUDE]["available"], rows[CLAUDE]["reason"]) == (False, NO_BALANCE)
    response = api.post("/assistant/model", json={"model": CLAUDE}, headers=H("u1"))
    assert response.status_code == 409 and response.json()["detail"] == NO_BALANCE
    _attempt(services.pool, "openai", "different-model", "ProviderRejectedError", "HTTP 403", 2)
    assert _by_id(api.get("/assistant/models", headers=H("u1")).json())[OPENAI]["available"]
    _attempt(services.pool, "anthropic", "claude-sonnet-5-5", None, None, 3)
    assert _by_id(api.get("/assistant/models", headers=H("u1")).json())[CLAUDE]["available"]


def test_unconfigured_and_unpriced_models_are_disabled_with_reasons(api, services):
    old = services.models
    services.models = ModelChoice({DS: {}, OPENAI: {}}, DS, old.catalog, {OPENAI: "未配置价格"})
    rows = _by_id(api.get("/assistant/models", headers=H("u1")).json())
    assert (rows[CLAUDE]["available"], rows[CLAUDE]["reason"]) == (False, NO_KEY)
    assert (rows[OPENAI]["available"], rows[OPENAI]["reason"]) == (False, "未配置价格")
    for model in (CLAUDE, OPENAI):
        assert api.post("/assistant/model", json={"model": model}, headers=H("u1")).status_code == 409


def test_removed_explicit_choice_never_silently_switches_provider(api, services):
    with services.pool.connection() as conn:
        set_model_choice(conn, "u1", "claude", "high", now=T0)
    body = api.get("/assistant/models", headers=H("u1")).json()
    assert body["current"] == "claude" and body["reasoning"] is None
    assert api.post("/assistant/model", json={"reasoning": "low"}, headers=H("u1")).status_code == 409
    assert api.post("/assistant/model", json={"model": OPENAI}, headers=H("u1")).status_code == 200


def test_reasoning_only_rejects_disabled_current_model_without_writing(api, services):
    assert api.post("/assistant/model", json={"model": CLAUDE}, headers=H("u1")).status_code == 200
    _attempt(services.pool, "anthropic", "claude-sonnet-5-5", "ProviderRejectedError", "HTTP 403", 1)
    response = api.post("/assistant/model", json={"reasoning": "high"}, headers=H("u1"))
    assert response.status_code == 409
    with services.pool.connection() as conn:
        assert chosen_models(conn, "u1") == {"assistant": CLAUDE, "reasoning": "low"}


def test_configuration_restart_releases_old_bad_key_but_new_refusal_still_blocks(api, services):
    from dataclasses import replace

    from evowiki.runtime.models import BAD_KEY

    services.models = replace(services.models, health_since=T0)
    _attempt(services.pool, "anthropic", "claude-sonnet-5-5", "ProviderRejectedError", "HTTP 401", 1)
    assert not _by_id(api.get("/assistant/models", headers=H("u1")).json())[CLAUDE]["available"]
    assert api.post("/assistant/model", json={"model": CLAUDE}, headers=H("u1")).status_code == 409

    # Native key save restarts build_services, capturing a new health epoch. Audit rows stay intact.
    services.models = replace(services.models, health_since=T0.replace(minute=2))
    restarted = TestClient(create_app(services), base_url="http://127.0.0.1")
    assert _by_id(restarted.get("/assistant/models", headers=H("u1")).json())[CLAUDE]["available"]
    picked = restarted.post("/assistant/model", json={"model": CLAUDE}, headers=H("u1"))
    assert picked.status_code == 200 and picked.json()["current"] == CLAUDE
    with services.pool.connection() as conn:
        assert conn.execute("select count(*) as n from llm_attempt where id='att1'").fetchone()["n"] == 1

    _attempt(services.pool, "anthropic", "claude-sonnet-5-5", "ProviderRejectedError", "HTTP 401", 3)
    rows = _by_id(restarted.get("/assistant/models", headers=H("u1")).json())
    assert not rows[CLAUDE]["available"] and rows[CLAUDE]["reason"] == BAD_KEY
    assert rows[DS]["available"]
    assert restarted.post("/assistant/model", json={"model": CLAUDE}, headers=H("u1")).status_code == 409
    assert restarted.get("/assistant/models", headers=H("u1")).json()["current"] == CLAUDE
