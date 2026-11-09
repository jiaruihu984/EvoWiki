"""Task 10: RSI writes its changes into the action ledger, and the ledger's rows join
GET /wiki/rsi/changes (track "G"). Scripted LLM only; real Postgres."""

from datetime import timedelta

import pytest

from evowiki.bot.actions import ActionLedger
from evowiki.bot.change_source import LedgerChangeSource
from evowiki.bot.executors import RsiMirrorExecutor
from evowiki.store.db import fetch_all
from evowiki.teach.contracts import ChangeOut
from tests.runtime.helpers import T0
from tests.service.conftest import H, signup
from tests.service.conftest import services as _services_fixture
from tests.service.test_rsi_api import run as rsi_run
from tests.service.test_rsi_api import seed as rsi_seed
from tests.wikiapp.script import Script

services = _services_fixture


@pytest.fixture
def client(services):
    """The service app with the ledger wired the way `build_services` does it."""
    from fastapi.testclient import TestClient

    from evowiki.service.app import create_app
    ledger = ActionLedger(services.pool, services.clock, services.new_id, {})
    ledger.register("rsi_patch", RsiMirrorExecutor())
    ledger.register("rsi_method", RsiMirrorExecutor())
    services.actions = ledger
    services.change_sources = (LedgerChangeSource(ledger),)
    return TestClient(create_app(services), base_url="http://127.0.0.1")


@pytest.fixture
def wiki(services, client):
    script = Script()
    services.gateway = script.gateway()
    return client, script


def _direct(client):
    """The levels at which an accepted patch applies at once (the default is `ask`)."""
    s = client.get("/wiki/bot/settings", headers=H("u1")).json()
    assert client.put("/wiki/bot/settings", headers=H("u1"), json={
        **s, "levels": {**s["levels"], "teaching_params": "auto_undo", "rsi": "auto_undo"}}).status_code == 200


def rows(services, prefix=""):
    with services.pool.connection() as conn:
        return fetch_all(conn, "select * from agent_action where idempotency_key like %s "
                               "order by created_at, action_id", (prefix + "%",))


def listed(client, uid="u1") -> list[ChangeOut]:
    r = client.get("/wiki/rsi/changes", headers=H(uid))
    assert r.status_code == 200, r.text
    return [ChangeOut.model_validate(c) for c in r.json()]


def test_a_mirror_never_moves_a_row_the_ledger_already_decided(services, client):
    signup(client, "u1")
    ledger = services.actions
    now = services.clock()
    with services.pool.connection() as conn, conn.transaction():
        ledger.record_in(conn, "u1", kind="rsi_patch", scope="teaching_params",
                         source="rsi", summary="s", content={"patch_id": "p"},
                         key="rsi:u1:p", status="approved")
        assert not ledger.set_status_in(conn, "rsi:u1:p", "rejected", at=now,
                                        from_statuses=("proposed",))
        assert ledger.set_status_in(conn, "rsi:u1:p", "executed", at=now,
                                    from_statuses=("approved",))
        assert not ledger.set_status_in(conn, "nope", "undone", at=now, missing_ok=True)
        with pytest.raises(LookupError):
            ledger.set_status_in(conn, "nope", "undone", at=now)


def test_accepted_rsi_patch_has_one_mirror_and_one_listed_row(wiki, services):
    client, _s = wiki
    rsi_seed(services, client)
    _direct(client)
    out = rsi_run(client)
    pid = out["patch_ids"][0]
    [m] = rows(services, "rsi:")
    assert m["idempotency_key"] == f"rsi:u1:{pid}" and m["status"] == "executed"
    assert (m["kind"], m["scope"], m["source"]) == ("rsi_patch", "teaching_params", "rsi")
    assert [c.patch_id for c in listed(client)] == [pid]        # the native row only
    r = client.post(f"/wiki/rsi/patches/{pid}/undo", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert rows(services, "rsi:")[0]["status"] == "undone"
    assert [(c.patch_id, c.status) for c in listed(client)] == [(pid, "rolled_back")]


@pytest.mark.parametrize(("language", "detail"), [
    ("zh", "这项更改现在不能撤销"), ("en", "This change cannot be undone now."),
])
def test_a_bare_rsi_mirror_executor_refuses_without_a_detour(wiki, services, language, detail):
    """Without an RSI undo wired (`RsiExecutor` has one: test_rsi_approval) a mirror refuses
    plainly, never sending the learner to another panel."""
    client, _s = wiki
    rsi_seed(services, client)
    _direct(client)
    rsi_run(client)
    settings = client.get("/wiki/settings", headers=H("u1")).json()
    assert client.put("/wiki/settings", headers=H("u1"),
                      json={**settings, "language": language}).status_code == 200
    [m] = rows(services, "rsi:")
    r = client.post(f"/wiki/bot/actions/{m['action_id']}/undo", headers=H("u1"))
    assert r.status_code == 409
    assert r.json() == {"detail": detail, "code": "action_refused", "params": {}}
    assert rows(services, "rsi:")[0]["status"] == "executed"
    assert client.post(f"/wiki/rsi/patches/{m['action_id']}/undo",
                       headers=H("u1")).status_code == 409


def test_method_change_mirror(client, services):
    from evowiki.store.rsi_layer2 import PgLayer2Store
    from evowiki.teach.contracts import MethodChange
    from evowiki.teach.layer2 import method_registry
    signup(client, "u1")
    l2 = PgLayer2Store(services.pool)
    method_registry.commit(l2, "u1", "A2", fit_model=method_registry.DEFAULT_FIT_MODEL,
                           change_id="mc_1", now=T0 - timedelta(hours=1))
    l2.put_method_change("u1", MethodChange(
        change_id="mc_1", kind="A2", status="applied", summary="换了估计方法", why="猜得更准",
        from_version=1, to_version=2, created_at=T0 - timedelta(hours=1)))
    [m] = rows(services, "rsi-method:")
    assert (m["idempotency_key"], m["kind"], m["status"]) == (
        "rsi-method:u1:mc_1", "rsi_method", "executed")
    assert [c.patch_id for c in listed(client)] == ["mc_1"]
    assert client.post("/wiki/rsi/patches/mc_1/undo", headers=H("u1")).status_code == 200
    assert rows(services, "rsi-method:")[0]["status"] == "undone"


def test_another_users_ledger_row_is_unknown(wiki, services):
    client, _s = wiki
    rsi_seed(services, client)
    rsi_run(client)
    signup(client, "u2")
    [ch] = listed(client)
    assert client.post(f"/wiki/rsi/patches/{ch.patch_id}/undo", headers=H("u2")).status_code == 404
    assert listed(client, "u2") == []
