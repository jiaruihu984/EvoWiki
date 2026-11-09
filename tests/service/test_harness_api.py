"""GET /wiki/harness and /wiki/harness/history, and the defaults listing behind them. Real
Postgres, no LLM."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from evowiki.agent.tools import ToolResult
from evowiki.bot import prompts
from evowiki.bot.actions import ActionLedger
from evowiki.bot.change_source import LedgerChangeSource
from evowiki.bot.executors import PromptOverrideExecutor
from evowiki.harness import round as hround
from evowiki.harness.keys import KEYS
from evowiki.service import routes_harness
from evowiki.service.app import create_app
from evowiki.service.harness_defaults import defaults
from evowiki.store import harness_rsi
from evowiki.store.trace import PgTraceSink
from evowiki.store.usage import usage_tz
from evowiki.teach.contracts import RsiSettings
from tests.service.conftest import H, signup

NEW = "先给一个能算出结果的小例子，再讲定义。"


@pytest.fixture
def client(services) -> TestClient:
    ledger = ActionLedger(services.pool, services.clock, services.new_id, {})
    ledger.register(prompts.KIND, PromptOverrideExecutor(services.pool, services.clock))
    services.actions = ledger
    services.change_sources = (LedgerChangeSource(ledger),)
    c = TestClient(create_app(services), base_url="http://127.0.0.1")
    signup(c, "u1")
    return c


def test_every_key_has_a_default_inside_its_bounds():
    got = defaults()
    assert set(got) == {k.name for k in KEYS}
    for k in KEYS:
        d = got[k.name]
        if k.kind == "int":
            assert isinstance(d, int) and k.lo <= d <= k.hi, k.name
        elif k.dim in ("H1",):
            assert isinstance(d, str) and d.strip(), k.name


def test_empty_user_has_version_zero_and_no_values(client):
    r = client.get("/wiki/harness", headers=H("u1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 0
    assert [k["name"] for k in body["keys"]] == [k.name for k in KEYS]
    assert all(k["value"] is None and k["source"] is None for k in body["keys"])
    assert {k["name"]: k["default"] for k in body["keys"]} == defaults()
    assert client.get("/wiki/harness/history", headers=H("u1")).json() == []


def test_prompt_change_shows_as_user_value_and_history(client):
    assert client.put("/wiki/prompts/write", headers=H("u1"), json={"text": NEW}).status_code == 200
    body = client.get("/wiki/harness", headers=H("u1")).json()
    assert body["version"] == 1
    by = {k["name"]: k for k in body["keys"]}
    assert by["H1.write"]["value"] == NEW and by["H1.write"]["source"] == "user"
    assert by["H1.write"]["default"] != NEW
    assert by["H1.card"]["value"] is None
    hist = client.get("/wiki/harness/history", headers=H("u1")).json()
    assert len(hist) == 1
    assert (hist[0]["version"], hist[0]["key"], hist[0]["before"], hist[0]["after"],
            hist[0]["source"]) == (1, "H1.write", None, NEW, "user")


def test_another_user_sees_nothing_and_limit_is_checked(client):
    client.put("/wiki/prompts/write", headers=H("u1"), json={"text": NEW})
    signup(client, "u2")
    assert client.get("/wiki/harness", headers=H("u2")).json()["version"] == 0
    assert client.get("/wiki/harness/history", headers=H("u2")).json() == []
    assert client.get("/wiki/harness/history?limit=0", headers=H("u1")).status_code == 422


def test_trace_is_returned_to_its_user_only(client, services):
    signup(client, "u2")
    sink = PgTraceSink(services.pool, services.clock)
    sink.start("r1", user_id="u1", agent="deep.write", subject="p1", pack_version=0,
               input={"page_id": "p1"})
    sink.tool("r1", 1, "c1", "read_page", {"p": 1}, ToolResult(content="页面内容"))
    sink.finish("r1", stop="finished", error=None)
    got = client.get("/wiki/traces/r1", headers=H("u1"))
    assert got.status_code == 200
    body = got.json()
    assert body["run"]["agent"] == "deep.write" and body["run"]["input"] == {"page_id": "p1"}
    assert [s["kind"] for s in body["steps"]] == ["tool"]
    assert body["steps"][0]["result"]["content"] == "页面内容"
    assert client.get("/wiki/traces/r1", headers=H("u2")).status_code == 404
    assert client.get("/wiki/traces/nope", headers=H("u1")).status_code == 404


# --- rounds, experience, month cost, manual run (Task 12) ----------------------------------

def _round(pool, uid, started, status="applied", cost=0.5, with_change=False):
    with pool.connection() as conn:
        rid = harness_rsi.start_round(conn, uid, finding="f", now=started)
        harness_rsi.finish_round(conn, rid, status, reason="原因", signal_ids=["s1"], cases=[{"c": 1}],
                                 candidates=[], comparisons=[{"quote": "q"}], decision={"d": 1},
                                 diff={"x": 1}, cost=cost, improver_run_id=None, now=started)
        cid = (harness_rsi.record_change(conn, uid, rid, {"x": 1}, "applied", now=started)
               if with_change else None)
    return rid, cid


def test_rounds_list_shape_projection_and_change_id(client, services):
    signup(client, "u2")
    now = services.clock()
    r1, c1 = _round(services.pool, "u1", now - timedelta(hours=1), cost=0.5, with_change=True)
    r2, _ = _round(services.pool, "u1", now - timedelta(hours=2), status="thin", cost=0.25)
    _round(services.pool, "u2", now - timedelta(hours=1), cost=9.0)
    client.put("/wiki/rsi/settings", headers=H("u1"), json={"harness_month_limit_usd": 4.0})
    body = client.get("/wiki/harness/rounds?limit=10", headers=H("u1")).json()
    assert [r["round_id"] for r in body["rounds"]] == [r1, r2]
    assert set(body["rounds"][0]) == {"round_id", "started_at", "status", "reason", "cost",
                                      "change_id", "change_status"}
    assert body["rounds"][0]["change_id"] == c1 and body["rounds"][1]["change_id"] is None
    assert body["rounds"][0]["change_status"] == "applied"
    assert body["rounds"][1]["change_status"] is None
    # rolled back later: the round (written once) still says applied, its change says rolled_back
    with services.pool.connection() as conn:
        harness_rsi.settle_change(conn, c1, "rolled_back", now=now)
    body = client.get("/wiki/harness/rounds?limit=10", headers=H("u1")).json()
    assert (body["rounds"][0]["status"], body["rounds"][0]["change_status"]) == ("applied",
                                                                                "rolled_back")
    detail = client.get(f"/wiki/harness/rounds/{r1}", headers=H("u1")).json()
    assert (detail["change_id"], detail["change_status"]) == (c1, "rolled_back")
    assert body["month_cost"] == pytest.approx(0.75)
    assert body["month_limit"] == 4.0
    local = now.astimezone(usage_tz())
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elapsed = max((local - first).total_seconds() / 86400, 1.0)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    days = (nxt - first).days
    assert body["month_projection"] == pytest.approx(0.75 / elapsed * days)


def test_rounds_limit_and_no_limit_setting(client, services):
    now = services.clock()
    for i in range(3):
        _round(services.pool, "u1", now - timedelta(hours=i + 1))
    body = client.get("/wiki/harness/rounds?limit=2", headers=H("u1")).json()
    assert len(body["rounds"]) == 2 and body["month_limit"] is None


def test_round_detail_and_other_users_round_is_404(client, services):
    signup(client, "u2")
    rid, _ = _round(services.pool, "u1", services.clock() - timedelta(hours=1))
    other, _ = _round(services.pool, "u2", services.clock() - timedelta(hours=1))
    body = client.get(f"/wiki/harness/rounds/{rid}", headers=H("u1")).json()
    assert body["round_id"] == rid and body["cases"] == [{"c": 1}]
    assert body["comparisons"] == [{"quote": "q"}]
    assert body["decision"] == {"d": 1, "expectation": {
        "feedback": ["s1"], "cases": [{"c": 1}], "citations": [], "rationale": "原因"}}
    assert body["diff"] == {"x": 1}
    assert client.get(f"/wiki/harness/rounds/{other}", headers=H("u1")).status_code == 404
    assert client.get("/wiki/harness/rounds/nope", headers=H("u1")).status_code == 404


def test_experience_lessons(client, services):
    signup(client, "u2")
    now = services.clock()
    with services.pool.connection() as conn:
        harness_rsi.add_experience(conn, "u1", {"k": "v"}, {"c": 1}, {"e": 1}, "effective", "user",
                                   weight=1.0, now=now)
        harness_rsi.add_experience(conn, "u2", {"k": "v"}, {"c": 1}, {"e": 1}, "harmful", "user",
                                   weight=1.0, now=now)
    got = client.get("/wiki/harness/experience?limit=5", headers=H("u1")).json()
    assert len(got) == 1 and got[0]["verdict"] == "effective" and got[0]["situation"] == {"k": "v"}


def test_run_manual_returns_the_round(client, services, monkeypatch):
    seen = {}

    def fake_run(uid, deps, *, now, manual=False):
        seen.update(uid=uid, manual=manual, deps=deps)
        return hround.HRound(round_id="r1", status="no_change", reason="没有改动", cost=0.0,
                             change_id=None)
    monkeypatch.setattr(routes_harness, "run_round", fake_run)
    monkeypatch.setattr(routes_harness, "_harness", lambda svc: hround.HarnessRounds("DEPS"))
    r = client.post("/wiki/harness/run", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert r.json() == {"round_id": "r1", "status": "no_change", "reason": "没有改动", "cost": 0.0,
                        "change_id": None}
    assert seen == {"uid": "u1", "manual": True, "deps": "DEPS"}


def test_run_while_cycle_lock_held_is_409(client, services, monkeypatch):
    from evowiki.service.rsi_runs import cycle_lock
    monkeypatch.setattr(routes_harness, "_harness", lambda svc: hround.HarnessRounds("DEPS"))
    monkeypatch.setattr(routes_harness, "run_round", lambda *a, **k: pytest.fail("ran"))
    with cycle_lock(services, "u1") as got:
        assert got
        r = client.post("/wiki/harness/run", headers=H("u1"))
    assert r.status_code == 409 and r.json()["detail"]


def test_run_without_deep_or_gateway_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(routes_harness, "_harness", lambda svc: None)
    r = client.post("/wiki/harness/run", headers=H("u1"))
    assert r.status_code == 503 and r.json()["detail"]


def test_harness_settings_round_trip(client):
    r = client.put("/wiki/rsi/settings", headers=H("u1"),
                   json={"harness": True, "harness_month_limit_usd": 3.5})
    assert r.status_code == 200, r.text
    got = RsiSettings.model_validate(client.get("/wiki/rsi/settings", headers=H("u1")).json())
    assert got.harness is True and got.harness_month_limit_usd == 3.5
    assert client.put("/wiki/rsi/settings", headers=H("u1"),
                      json={"harness_month_limit_usd": -1}).status_code == 422


def test_trace_by_otel_id_is_its_users_and_hidden_while_a_forget_is_open(client, services):
    from evowiki.obs.spans import trace_id
    with services.pool.connection() as conn:
        conn.execute("insert into assistant_session (session_id, user_id, created_at) "
                     "values ('s1', 'u1', %s)", (services.clock(),))
    signup(client, "u2")
    sink = PgTraceSink(services.pool, services.clock)
    sink.start("assistant:s1:m1", user_id="u1", agent="assistant", subject="s1", pack_version=0,
               input={"session_id": "s1"})
    sink.finish("assistant:s1:m1", stop="finished", error=None)
    tid = trace_id("assistant:s1:m1")
    got = client.get(f"/wiki/traces/otel/{tid}", headers=H("u1"))
    assert got.status_code == 200 and got.json()["run"]["run_id"] == "assistant:s1:m1"
    assert client.get(f"/wiki/traces/otel/{tid}", headers=H("u2")).status_code == 404
    assert client.get("/wiki/traces/otel/" + "0" * 32, headers=H("u1")).status_code == 404
    with services.pool.connection() as conn:
        conn.execute("select evowiki_forget_request('u1', 's1', null, 'api:test')")
    assert client.get(f"/wiki/traces/otel/{tid}", headers=H("u1")).status_code == 404
    assert client.get("/wiki/traces/assistant:s1:m1", headers=H("u1")).status_code == 404
