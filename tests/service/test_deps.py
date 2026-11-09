import logging

import pytest

from evowiki.retrieval.embed import HashingEmbedder
from evowiki.runtime.llm import DEEPSEEK_NOTHINK
from evowiki.runtime.usage import runtime_tier_configs
from evowiki.service import deps
from evowiki.service.deps import build_services, default_new_id
from evowiki.store.llm_logs import PgAttemptLog, PgCallLog
from evowiki.store.usage import PgUsageMeter
from tests.runtime.helpers import T0, FixedClock, seed_runtime


def test_default_new_id_is_prefixed_and_unique():
    a, b = default_new_id("rp"), default_new_id("rp")
    assert a.startswith("rp_") and a != b


@pytest.mark.parametrize("keys,expected", [
    ({"DEEPSEEK_API_KEY": "fake"}, "deepseek"),
    ({"ANTHROPIC_API_KEY": "fake"}, "anthropic"),
    ({"ANTHROPIC_API_KEY": "fake", "DEEPSEEK_API_KEY": "fake"}, "anthropic"),
])
def test_each_supported_key_setup_wires_assistant_and_pages(env, monkeypatch, keys, expected):
    providers = {"anthropic": object(), "anthropic-think": object(),
                 "deepseek": object(), DEEPSEEK_NOTHINK: object()}
    selected = {name: value for name, value in providers.items()
                if ("ANTHROPIC_API_KEY" if name.startswith("anthropic") else "DEEPSEEK_API_KEY") in keys}
    monkeypatch.setattr(deps, "runtime_providers", lambda _: selected)
    monkeypatch.setattr(deps, "runtime_deep_providers", lambda _: selected)
    svc = build_services({**env, **keys}, clock=FixedClock(T0))
    try:
        assert svc.assistant is not None and svc.gateway is not None
        assert svc.gateway.config.generator.provider == expected
        writer = DEEPSEEK_NOTHINK if expected == "deepseek" else expected   # pages never think
        assert svc.deep.llm is not None and svc.deep.llm.config.generator.provider == writer
    finally:
        svc.pool.close()


def test_new_database_can_build_services_and_register_a_user(db_url):
    from fastapi.testclient import TestClient

    from evowiki.service.app import create_app

    svc = build_services({"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash"},
                         clock=FixedClock(T0))
    try:
        assert svc.ctx.content.version_id == "cv_empty"
        # Include real DBOS launch/shutdown, not only isolated route calls.
        with TestClient(create_app(svc), base_url="http://127.0.0.1") as client:
            response = client.post("/users", json={"user_id": "first_user",
                                   "survey": {"math": "none", "ml": "none", "dl": "none"}})
            assert response.status_code == 201, response.text
            assert client.get("/me", headers={"X-EvoWiki-User": "first_user"}).status_code == 200
            assert client.get("/health/ready").status_code == 200
    finally:
        svc.pool.close()


@pytest.fixture
def env(db_url, pool):
    seed_runtime(pool)
    return {"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash"}


def test_build_services_without_llm_keys_degrades(env):
    svc = build_services(env, clock=FixedClock(T0))
    try:
        assert svc.gateway is None
        assert isinstance(svc.embedder, HashingEmbedder)
        assert svc.ctx.content.version_id
    finally:
        svc.pool.close()


def test_single_provider_mode_logs_to_postgres_and_warns(env, monkeypatch, caplog):
    monkeypatch.setattr(deps, "runtime_providers",
                        lambda e: {"deepseek": object(), DEEPSEEK_NOTHINK: object()})
    env = {**env, "EVOWIKI_RUNTIME_PROVIDER": "deepseek"}
    with caplog.at_level(logging.WARNING, logger="evowiki.service"):
        svc = build_services(env, clock=FixedClock(T0))
    try:
        gw = svc.gateway
        assert gw is not None and gw.mode == "record" and gw.config.single_provider
        assert gw._record_max_attempts == 1  # no record-mode retries: 5-min job lease budget
        assert isinstance(gw._log, PgCallLog) and isinstance(gw._attempts, PgAttemptLog)
        assert any("single-provider mode" in r.getMessage() for r in caplog.records)
    finally:
        svc.pool.close()


def test_missing_role_provider_degrades_instead_of_a_half_working_gateway(env, monkeypatch,
                                                                          caplog):
    # default two-provider mode (generator anthropic, verifier deepseek) with only deepseek set
    monkeypatch.setattr(deps, "runtime_providers", lambda e: {"deepseek": object()})
    with caplog.at_level(logging.WARNING, logger="evowiki.service"):
        svc = build_services({**env, "EVOWIKI_RUNTIME_PROVIDER": "anthropic"}, clock=FixedClock(T0))
    try:
        assert svc.gateway is None
        warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("anthropic" in m and "EVOWIKI_RUNTIME_PROVIDER" in m for m in warn)
    finally:
        svc.pool.close()


def test_deepseek_mode_wires_metering_and_both_tiers(env, monkeypatch):
    monkeypatch.setattr(deps, "runtime_providers",
                        lambda e: {"deepseek": object(), DEEPSEEK_NOTHINK: object()})
    env = {**env, "EVOWIKI_RUNTIME_PROVIDER": "deepseek"}
    svc = build_services(env, clock=FixedClock(T0))
    try:
        usage = svc.gateway._usage
        assert isinstance(usage.meter, PgUsageMeter) and usage.tiers is usage.meter
        assert dict(usage.tier_configs) == runtime_tier_configs(env)
    finally:
        svc.pool.close()


def test_anthropic_mode_meters_but_keeps_one_config(env, monkeypatch):
    monkeypatch.setattr(deps, "runtime_providers",
                        lambda e: {"anthropic": object(), "deepseek": object()})
    svc = build_services(env, clock=FixedClock(T0))
    try:
        usage = svc.gateway._usage
        assert isinstance(usage.meter, PgUsageMeter) and usage.tier_configs is None
    finally:
        svc.pool.close()


def test_missing_fast_tier_provider_degrades(env, monkeypatch, caplog):
    monkeypatch.setattr(deps, "runtime_providers", lambda e: {"deepseek": object()})
    env = {**env, "EVOWIKI_RUNTIME_PROVIDER": "deepseek"}
    with caplog.at_level(logging.WARNING, logger="evowiki.service"):
        svc = build_services(env, clock=FixedClock(T0))
    try:
        assert svc.gateway is None
        assert any(DEEPSEEK_NOTHINK in r.getMessage() for r in caplog.records)
    finally:
        svc.pool.close()


def test_gpu_embedder_that_fails_its_check_stops_the_start(env, monkeypatch, caplog):
    """EVOWIKI_EMBED_PROVIDERS=cuda and the session is not on CUDA: logged, raised, pool closed."""
    from evowiki.retrieval import embed

    monkeypatch.setattr(embed, "_SHARED", {})

    def on_cpu(self):
        raise RuntimeError("refusing a CPU embedder")
    monkeypatch.setattr(embed.FastEmbedEmbedder, "check_gpu", on_cpu)
    pools = []
    real_make_pool = deps.make_pool
    monkeypatch.setattr(deps, "make_pool", lambda *a, **kw: pools.append(
        real_make_pool(*a, **kw)) or pools[-1])
    env = {**env, "EVOWIKI_EMBEDDER": "fastembed", "EVOWIKI_EMBED_PROVIDERS": "cuda"}
    with (caplog.at_level(logging.ERROR, logger="evowiki.service"),
          pytest.raises(RuntimeError, match="refusing a CPU embedder")):
        build_services(env, clock=FixedClock(T0))
    assert any("GPU embedder" in r.getMessage() for r in caplog.records)
    assert pools and pools[0].closed
