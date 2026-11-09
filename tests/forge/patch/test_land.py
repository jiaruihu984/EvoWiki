from datetime import UTC, datetime

import pytest

from evowiki.content.models import Lang
from evowiki.forge import switch
from evowiki.forge.models import ForgeNotFound, PromoteRefused
from evowiki.forge.patch import land
from evowiki.store import forge as fstore
from evowiki.store import forge_patch as ps
from evowiki.store.users import UserRow, create_user, get_user

T = datetime(2026, 10, 23, 9, tzinfo=UTC)


def seed(conn, cid="p1", *, user="u1", state="built", status="built", goal="修好加法。其他",
         note="答案总被判错", cost=0.5, title="修好加法"):
    for u in sorted({user, "u1", "u2"}):
        if get_user(conn, u) is None:
            create_user(conn, UserRow(user_id=u, background="none", condition="S2", lang=Lang.EN,
                                      created_at=T))
    fstore.insert_candidate(conn, fstore.CandidateRow(
        id=cid, user_id=user, kind="patch", name=None, parent_id=None,
        spec={"goal": goal, "title": title}, evidence={"note": note}, files={}, build={"ok": True},
        status=status, run_id=None, cost_usd=cost, created_at=T, updated_at=T))
    ps.insert_patch(conn, ps.PatchRow(
        candidate_id=cid, user_id=user, title=title, base_commit="b", commit="c",
        branch=f"forge/{cid}", state=state, live_commit=None, revert_commit=None, reason="",
        gate_ref=None, cost_usd=cost, created_at=T, updated_at=T))


def promote(conn, cid="p1", ok=True, ref="g1"):
    return land.promote_patch(conn, cid, gate_ref=ref, gate_ok=lambda r: ok, by="round", why="w",
                              now=T)


@pytest.fixture
def conn(pool, monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)
    monkeypatch.delenv("EVOWIKI_FORGE_OPERATOR", raising=False)
    with pool.connection() as c:
        switch.set_enabled(c, None, "patch", True, T)
        yield c


def test_promote_refused_without_gate(conn):
    seed(conn)
    with pytest.raises(PromoteRefused):
        promote(conn, ok=False)
    with pytest.raises(PromoteRefused):
        promote(conn, ref=" ")
    assert ps.get_patch(conn, "p1").state == "built"
    assert fstore.get_candidate(conn, "p1").status == "built"


def test_promote_refused_wrong_state_or_switch(conn):
    seed(conn, "p2", state="landed", status="promoted")
    with pytest.raises(PromoteRefused):
        promote(conn, "p2")
    seed(conn, "p3")
    switch.set_enabled(conn, None, "patch", False, T)
    with pytest.raises(PromoteRefused):
        promote(conn, "p3")
    with pytest.raises(ForgeNotFound):
        promote(conn, "nope")


def test_promote_waits_for_the_users_review(conn):
    seed(conn)
    assert promote(conn) == "awaiting_approval"
    assert ps.get_patch(conn, "p1").state == "awaiting_approval"
    assert ps.get_patch(conn, "p1").gate_ref == "g1"


def test_patches_always_need_approval_and_it_cannot_be_turned_off(conn):
    """Review mode (ruling 2026-10-23): a code patch never lands without the user's approval; a
    stored `approve.patch = false` (old auto-apply setting) changes nothing."""
    assert land.needs_approval(conn, "u1") is True
    assert land.needs_approval(conn, None) is True
    seed(conn)
    fstore.set_setting(conn, "u1", "approve.patch", False, T)
    fstore.set_setting(conn, "global", "approve.patch", False, T)
    assert land.needs_approval(conn, "u1") is True
    assert promote(conn) == "awaiting_approval"


@pytest.mark.parametrize("state", ["built", "awaiting_approval"])
def test_approve_queues_a_reviewed_patch(conn, state):
    seed(conn, state=state, status="built" if state == "built" else "promoted")
    land.approve(conn, "p1", by="u2", now=T)              # any user who sees the row
    assert ps.get_patch(conn, "p1").state == "queued"
    assert fstore.get_candidate(conn, "p1").status == "promoted"
    with pytest.raises(PromoteRefused):
        land.approve(conn, "p1", by="u1", now=T)
    with pytest.raises(ForgeNotFound):
        land.approve(conn, "nope", by="u1", now=T)


def test_approve_refused_when_patches_are_switched_off(conn, monkeypatch):
    seed(conn)
    switch.set_enabled(conn, None, "patch", False, T)
    with pytest.raises(PromoteRefused):
        land.approve(conn, "p1", by="u1", now=T)
    switch.set_enabled(conn, None, "patch", True, T)
    monkeypatch.setenv("EVOWIKI_FORGE", "off")
    with pytest.raises(PromoteRefused):
        land.approve(conn, "p1", by="u1", now=T)
    assert ps.get_patch(conn, "p1").state == "built"


@pytest.mark.parametrize("state", ["built", "awaiting_approval"])
def test_discard_withdraws_a_patch_under_review(conn, state):
    seed(conn, state=state)
    land.discard(conn, "p1", by="u2", now=T)
    assert ps.get_patch(conn, "p1").state == "withdrawn"
    assert ps.get_patch(conn, "p1").reason == land.DISCARDED
    assert fstore.get_candidate(conn, "p1").status == "withdrawn"
    with pytest.raises(PromoteRefused):
        land.discard(conn, "p1", by="u1", now=T)
    with pytest.raises(PromoteRefused):
        land.approve(conn, "p1", by="u1", now=T)
    with pytest.raises(ForgeNotFound):
        land.discard(conn, "nope", by="u1", now=T)


def test_discard_refused_once_queued_or_landed(conn):
    seed(conn, "q", state="queued", status="promoted")
    seed(conn, "l", state="landed", status="promoted")
    for cid in ("q", "l"):
        with pytest.raises(PromoteRefused):
            land.discard(conn, cid, by="u1", now=T)


def test_any_user_who_sees_the_row_can_undo(conn):
    seed(conn, state="landed", status="promoted")
    land.revert_patch(conn, "u2", "p1", by="user", now=T)   # not the requester, not the operator
    assert ps.get_patch(conn, "p1").state == "revert_queued"
    with pytest.raises(ValueError):
        land.revert_patch(conn, "u2", "p1", by="user", now=T)
    with pytest.raises(ForgeNotFound):
        land.revert_patch(conn, "u1", "zzz", by="user", now=T)


def test_revert_patch_by_requester(conn):
    seed(conn, state="landed", status="promoted")
    land.revert_patch(conn, "u1", "p1", by="user", now=T)
    assert ps.get_patch(conn, "p1").state == "revert_queued"


def test_revert_all_writes_one_request(conn):
    assert land.revert_all(conn, by="op", now=T) == 0
    assert ps.take_live_requests(conn) == []
    seed(conn, "a", state="landed", status="promoted")
    seed(conn, "b", state="landed", status="promoted")
    seed(conn, "c", state="reverted", status="rolled_back")
    assert land.revert_all(conn, by="op", now=T) == 2
    reqs = ps.take_live_requests(conn)
    assert [r["op"] for r in reqs] == ["revert_all"] and reqs[0]["by_user"] == "op"
