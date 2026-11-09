"""L2 code patches in review mode over HTTP (ruling 2026-10-23): a built patch shows in the change
list as awaiting the user's review; POST /wiki/rsi/patches/{id}/approve queues it for landing,
/discard drops it, /undo reverts a landed one. Any user who sees the row may decide or undo it."""

from datetime import UTC, datetime

import pytest

from evowiki.forge import change_source as cs
from evowiki.forge import switch
from evowiki.store import forge_patch as ps
from tests.forge.patch.test_land import seed
from tests.service.conftest import H, signup

T = datetime(2026, 10, 23, 9, tzinfo=UTC)


@pytest.fixture
def review(services, client, monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)
    monkeypatch.delenv("EVOWIKI_FORGE_OPERATOR", raising=False)
    signup(client, "u1")
    signup(client, "u2")
    with services.pool.connection() as conn:
        switch.set_enabled(conn, None, "patch", True, T)
        # New accounts opt in per user; a global grant alone is insufficient.
        switch.set_enabled(conn, "u1", "patch", True, T)
        switch.set_enabled(conn, "u2", "patch", True, T)
        seed(conn, "a")
        seed(conn, "d")
        seed(conn, "l", state="landed", status="promoted")
    services.change_sources = (cs.ForgePatchChangeSource(services.pool, services.clock),)
    return client, services


def _row(client, uid, pid):
    rows = client.get("/wiki/rsi/changes", headers=H(uid)).json()
    return next(r for r in rows if r["patch_id"] == pid)


def test_review_rows_approve_discard_and_undo(review):
    client, services = review
    assert _row(client, "u2", "forgep:a")["status"] == "awaiting_user"
    r = client.post("/wiki/rsi/patches/forgep:a/approve", headers=H("u2"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "personal_accepted" and r.json()["can_undo"] is False
    r = client.post("/wiki/rsi/patches/forgep:d/discard", headers=H("u2"))
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert r.json()["summary"].startswith("没采用的代码改动：")
    r = client.post("/wiki/rsi/patches/forgep:l/undo", headers=H("u2"))
    assert r.status_code == 200 and r.json()["status"] == "rolled_back"
    with services.pool.connection() as conn:
        assert [ps.get_patch(conn, c).state for c in "adl"] == [
            "queued", "withdrawn", "revert_queued"]


def test_approve_while_patches_are_off_is_a_409_with_code_patches_off(review):
    client, services = review
    with services.pool.connection() as conn:
        switch.set_enabled(conn, None, "patch", False, T)
    r = client.post("/wiki/rsi/patches/forgep:a/approve", headers=H("u1"))
    assert r.status_code == 409 and r.json()["code"] == "patches_off"
    assert _row(client, "u1", "forgep:a")["status"] == "awaiting_user"
    # discard still works while patches are off
    assert client.post("/wiki/rsi/patches/forgep:a/discard", headers=H("u1")).status_code == 200


def test_decide_errors(review):
    client, _ = review
    r = client.post("/wiki/rsi/patches/forgep:l/approve", headers=H("u1"))
    assert r.status_code == 409 and r.json()["code"] == "NotDecidable"
    assert client.post("/wiki/rsi/patches/forgep:l/approve", headers=H("u1")).status_code == 409
    assert client.post("/wiki/rsi/patches/forgep:l/discard", headers=H("u1")).status_code == 409
    assert client.post("/wiki/rsi/patches/forgep:zz/approve", headers=H("u1")).status_code == 404
    # ids no source decides (A / B patches decide through the Bot ledger)
    assert client.post("/wiki/rsi/patches/pb_1/approve", headers=H("u1")).status_code == 404
