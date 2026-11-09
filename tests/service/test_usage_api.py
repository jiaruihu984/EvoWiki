"""Usage API contract (plan rev 3.2 §9.4)."""
from datetime import UTC, timedelta

import pytest
from fastapi.testclient import TestClient

from evowiki.llm.usage import Account, Usage
from evowiki.service import routes_usage
from evowiki.service.app import create_app
from evowiki.store.usage import PgUsageMeter
from tests.runtime.helpers import T0, FixedClock, make_user
from tests.service.conftest import H

GROUP_KEYS = {"cost_usd", "input_tokens", "output_tokens", "calls", "replayed_calls",
              "unpriced_calls", "cache_read_tokens", "cache_write_tokens"}
GROUPS = {"page", "assistant", "curator", "diagnostic", "review", "other", "proactive", "rsi"}


@pytest.fixture
def usage_client(services, monkeypatch) -> TestClient:
    make_user(services.pool, "u1")
    make_user(services.pool, "u2")
    monkeypatch.setenv("EVOWIKI_USAGE_TZ", "UTC")
    app = create_app(services)
    app.include_router(routes_usage.router)
    return TestClient(app)


def _u(cost, inp=100, out=10, read=0, write=0):
    return Usage(provider="gen", model="g-1", input_tokens=inp, output_tokens=out,
                 cache_read_tokens=read, cache_write_tokens=write, cost_usd=cost)


def test_usage_empty(usage_client):
    r = usage_client.get("/usage", headers=H("u1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"today", "month", "daily_limit_usd", "monthly_limit_usd"}
    for period in ("today", "month"):
        assert body[period]["total_usd"] == 0.0
        assert set(body[period]["by_group"]) == GROUPS
        assert set(body[period]["by_group"]["page"]) == GROUP_KEYS
    assert body["today"]["since"].startswith("2026-10-01T00:00:00")
    assert body["month"]["since"].startswith("2026-10-01T00:00:00")
    assert body["daily_limit_usd"] is None and body["monthly_limit_usd"] is None


def test_usage_by_group_today_and_month(usage_client, services):
    now = T0 + timedelta(days=3)  # 2026-10-04 09:00 UTC
    services.clock = FixedClock(now)
    page = Account(user_id="u1", group="page")
    PgUsageMeter(services.pool, clock=FixedClock(T0 - timedelta(hours=10)), tz=UTC).record(
        page, "p", _u(4.0), replayed=False)  # 2026-09-30: previous month, never counted
    PgUsageMeter(services.pool, clock=FixedClock(now - timedelta(days=1)), tz=UTC).record(
        page, "p", _u(1.0), replayed=False)  # this month, not today
    meter = PgUsageMeter(services.pool, clock=FixedClock(now), tz=UTC)
    meter.record(page, "p", _u(0.5, read=3000, write=200), replayed=False)
    meter.record(page, "p", _u(None, 0, 0, read=9999), replayed=True)  # replays never count
    meter.record(Account(user_id="u1", group="assistant"), "a", _u(0.25), replayed=False)
    meter.record(Account(user_id="u2", group="page"), "p", _u(9.0), replayed=False)
    body = usage_client.get("/usage", headers=H("u1")).json()
    assert body["today"]["since"].startswith("2026-10-04T00:00:00")
    assert body["today"]["total_usd"] == pytest.approx(0.75)
    assert body["month"]["total_usd"] == pytest.approx(1.75)
    today_page = body["today"]["by_group"]["page"]
    assert today_page == {"cost_usd": pytest.approx(0.5), "input_tokens": 100,
                          "output_tokens": 10, "calls": 1, "replayed_calls": 1,
                          "unpriced_calls": 0, "cache_read_tokens": 3000,
                          "cache_write_tokens": 200}
    assert body["month"]["by_group"]["page"]["calls"] == 2


def test_settings_roundtrip(usage_client):
    r = usage_client.get("/usage/settings", headers=H("u1"))
    assert r.status_code == 200
    assert r.json() == {"budget_confirmed": True, "daily_limit_usd": None, "monthly_limit_usd": None}
    r = usage_client.patch("/usage/settings", headers=H("u1"),
                           json={"daily_limit_usd": 1.5})
    assert r.status_code == 200, r.text
    assert r.json()["daily_limit_usd"] == 1.5 and "tiers" not in r.json()
    r = usage_client.patch("/usage/settings", headers=H("u1"), json={"monthly_limit_usd": 30})
    assert r.json()["daily_limit_usd"] == 1.5 and r.json()["monthly_limit_usd"] == 30.0
    r = usage_client.patch("/usage/settings", headers=H("u1"), json={"daily_limit_usd": None})
    assert r.json()["daily_limit_usd"] is None and r.json()["monthly_limit_usd"] == 30.0
    assert usage_client.get("/usage", headers=H("u1")).json()["monthly_limit_usd"] == 30.0
    assert usage_client.get("/usage/settings", headers=H("u2")).json()["daily_limit_usd"] is None


@pytest.mark.parametrize("body", [
    {"daily_limit_usd": -1}, {"monthly_limit_usd": "lots"}, {"tiers": {"nope": "fast"}},
    {"tiers": {"page": "cheap"}}, {"tiers": {"page": "fast"}}, {"budget": 3}, {"daily_limit_usd": True},
    {"daily_limit_usd": "1.5"}, {"tiers": None},
])
def test_bad_settings_are_422(usage_client, body):
    r = usage_client.patch("/usage/settings", headers=H("u1"), json=body)
    assert r.status_code == 422, r.text


def test_user_header_required(usage_client):
    assert usage_client.get("/usage").status_code == 422


def test_budget_patch_preserves_internal_tiers(usage_client, services):
    from evowiki.store.usage import chosen_tiers, update_settings

    with services.pool.connection() as conn:
        update_settings(conn, "u1", now=T0, tiers={"page": "fast"})
    response = usage_client.patch("/usage/settings", headers=H("u1"), json={"daily_limit_usd": 2})
    assert response.status_code == 200 and "tiers" not in response.json()
    with services.pool.connection() as conn:
        assert chosen_tiers(conn, "u1") == {"page": "fast"}
