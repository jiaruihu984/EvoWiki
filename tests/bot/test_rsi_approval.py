"""Bot level `ask` for teaching_params / rsi end to end (teach.approval): a track-A fit that passed
its Gate waits in /wiki/bot/actions with its 改动对比; approve applies it exactly once (the ledger's
effect commits with the RSI write), a second approve is a no-op, a cycle holding the lock answers
409 and leaves the row to approve again; reject closes it; a θ that moved since the proposal fails
it with a reason. Real Postgres, no LLM (track A is a statistical fit; no gateway)."""

import pytest

from evowiki.bot.actions import ActionLedger
from evowiki.bot.change_source import LedgerChangeSource
from evowiki.bot.executors import RsiExecutor
from evowiki.service import rsi_runs
from evowiki.store.db import fetch_all, fetch_one
from evowiki.store.rsi import PgRsiStore
from evowiki.teach import infer_paths as ip
from evowiki.teach import registry
from evowiki.teach.approval import STALE_A, patch_key
from evowiki.teach.contracts import InferParams
from tests.service.conftest import H
from tests.service.conftest import services as _services_fixture
from tests.service.test_rsi_api import changes, self_weight
from tests.service.test_rsi_api import run as rsi_run
from tests.service.test_rsi_api import seed as rsi_seed

services = _services_fixture
SELF = "evidence_weight.self"


@pytest.fixture
def client(services):
    """The service app with the ledger and its RSI executor wired as `build_services` does."""
    from fastapi.testclient import TestClient

    from evowiki.service.app import create_app
    ledger = ActionLedger(services.pool, services.clock, services.new_id, {})
    rsi = RsiExecutor(services.pool,
                      lambda uid, lock: rsi_runs.rsi_decider(services, uid, lock),
                      services.clock, lambda uid, ref: rsi_runs.ledger_undo(services, uid, ref))
    ledger.register("rsi_patch", rsi)
    ledger.register("rsi_method", rsi)
    services.actions = ledger
    services.change_sources = (LedgerChangeSource(ledger),)
    return TestClient(create_app(services))


def _ask(client, uid="u1", **levels):
    s = client.get("/wiki/bot/settings", headers=H(uid)).json()
    r = client.put("/wiki/bot/settings", headers=H(uid),
                   json={**s, "levels": {**s["levels"], **levels}})
    assert r.status_code == 200, r.text


def _proposed(services, client):
    rsi_seed(services, client)
    out = rsi_run(client)                       # `ask` is the default
    assert out["a"].startswith("awaiting_user "), out
    pid = out["a"].split()[1]
    r = client.get("/wiki/bot/actions", params={"status": "proposed"}, headers=H("u1"))
    assert r.status_code == 200, r.text
    (act,) = r.json()
    return pid, act


def _a_versions(services) -> int:
    with services.pool.connection() as conn:
        return fetch_one(conn, "select count(*) as n from rsi_policy_version "
                               "where user_id = 'u1' and track = 'A'")["n"]


def _ledger_rows(services) -> list[dict]:
    with services.pool.connection() as conn:
        return fetch_all(conn, "select * from agent_action where idempotency_key like %s",
                         ("rsi:%",))


def test_ask_waits_then_approve_applies_once(services, client):
    before = ip.get(InferParams(), SELF)          # nothing live yet: the default θ
    pid, act = _proposed(services, client)
    assert (act["kind"], act["scope"], act["level"], act["source"]) == (
        "rsi_patch", "teaching_params", "ask", "rsi")
    assert "你自己说的话" in act["summary"] and "你比实际" in act["why"]
    assert act["diff"][0] == {"kind": "ctx", "text": "学习状态评估"}
    assert {"kind": "del", "text": "自我评估权重 0.60"} in act["diff"]
    assert client.get("/wiki/bot/badge", headers=H("u1")).json()["pending_actions"] == 1
    assert self_weight(services) == before and _a_versions(services) == 0      # not live
    (row,) = [c for c in changes(client) if c.patch_id == pid]
    assert row.status == "awaiting_user" and not row.can_undo

    r = client.post(f"/wiki/bot/actions/{act['action_id']}/approve", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "executed" and r.json()["detail"] == ""
    n = _a_versions(services)
    assert n >= 1 and self_weight(services) < before
    (row,) = [c for c in changes(client) if c.patch_id == pid]
    assert row.status == "personal_accepted" and row.can_undo
    (led,) = _ledger_rows(services)                    # the proposal row became the mirror
    assert led["idempotency_key"] == patch_key("u1", pid) and led["status"] == "executed"
    assert led["level"] == "ask"
    with services.pool.connection() as conn:
        assert fetch_one(conn, "select count(*) as n from effect_applied where key = %s",
                         (f"action:{patch_key('u1', pid)}",))["n"] == 1
    # approving again is a no-op
    r = client.post(f"/wiki/bot/actions/{act['action_id']}/approve", headers=H("u1"))
    assert r.status_code == 200 and r.json()["status"] == "executed"
    assert _a_versions(services) == n
    assert client.get("/wiki/bot/badge", headers=H("u1")).json()["pending_actions"] == 0
    # undone like any accepted fit, in the RSI list
    assert client.post(f"/wiki/rsi/patches/{pid}/undo", headers=H("u1")).status_code == 200
    assert _ledger_rows(services)[0]["status"] == "undone"


def test_the_ledger_row_undo_undoes_the_rsi_change_itself(services, client):
    """Chat-first: 撤销 on the chat card (the ledger row) undoes the fit, no detour via 参数."""
    pid, act = _proposed(services, client)
    assert client.post(f"/wiki/bot/actions/{act['action_id']}/approve",
                       headers=H("u1")).status_code == 200
    r = client.post(f"/wiki/bot/actions/{act['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "undone"
    (row,) = [c for c in changes(client) if c.patch_id == pid]
    assert row.status == "rolled_back"
    # a second undo is refused with a plain reason, never the RSI list's English detail
    r = client.post(f"/wiki/bot/actions/{act['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 409 and not any(c.isascii() and c.isalpha() for c in r.json()["detail"])


def test_a_running_cycle_answers_409_and_the_row_can_be_approved_again(services, client):
    pid, act = _proposed(services, client)
    with rsi_runs.cycle_lock(services, "u1") as got:
        assert got
        r = client.post(f"/wiki/bot/actions/{act['action_id']}/approve", headers=H("u1"))
        assert r.status_code == 409
    assert _ledger_rows(services)[0]["status"] == "approved"
    assert [c.status for c in changes(client) if c.patch_id == pid] == ["awaiting_user"]
    r = client.post(f"/wiki/bot/actions/{act['action_id']}/approve", headers=H("u1"))
    assert r.status_code == 200 and r.json()["status"] == "executed"


def test_reject_closes_it(services, client):
    before = ip.get(InferParams(), SELF)          # nothing live yet: the default θ
    pid, act = _proposed(services, client)
    r = client.post(f"/wiki/bot/actions/{act['action_id']}/reject", headers=H("u1"))
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    (row,) = [c for c in changes(client) if c.patch_id == pid]
    assert row.status == "rejected" and "还有几项也调了" in row.summary   # at most three moves are named (_a_phrase)
    assert self_weight(services) == before and _a_versions(services) == 0
    assert client.post(f"/wiki/bot/actions/{act['action_id']}/approve",
                       headers=H("u1")).status_code == 409
    # the track is free: the next run proposes again
    assert rsi_run(client)["a"].startswith("awaiting_user ")


def test_a_theta_that_moved_since_the_proposal_fails_with_a_reason(services, client):
    pid, act = _proposed(services, client)
    registry.commit(PgRsiStore(services.pool), "u1", "A", infer=InferParams(), patch_id=None,
                    now=services.clock())                       # e.g. another change: v2
    n = _a_versions(services)
    r = client.post(f"/wiki/bot/actions/{act['action_id']}/approve", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed" and r.json()["detail"] == STALE_A
    assert _a_versions(services) == n
    (row,) = [c for c in changes(client) if c.patch_id == pid]
    assert row.status == "gate_failed" and "还有几项也调了" in row.summary


def test_auto_undo_applies_at_once_as_before(services, client):
    rsi_seed(services, client)
    _ask(client, teaching_params="auto_undo", rsi="auto_undo")
    out = rsi_run(client)
    assert out["a"].startswith("accepted ")
    assert client.get("/wiki/bot/actions", params={"status": "proposed"},
                      headers=H("u1")).json() == []
    (led,) = _ledger_rows(services)
    assert (led["status"], led["level"]) == ("executed", "auto_undo")
