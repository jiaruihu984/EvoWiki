from datetime import UTC, datetime

import pytest

from evowiki.forge import change_source as cs
from evowiki.forge import switch
from evowiki.store import forge_patch as ps
from evowiki.teach.change_source import source_for
from tests.forge.patch.test_land import seed

T = datetime(2026, 10, 23, 9, tzinfo=UTC)


def _src(pool):
    return cs.ForgePatchChangeSource(pool, lambda: T)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)
    monkeypatch.delenv("EVOWIKI_FORGE_OPERATOR", raising=False)


def test_landed_row(pool):
    with pool.connection() as conn:
        seed(conn, state="landed", status="promoted")
    [row] = _src(pool).list("u2")                      # global in effect: every user sees it
    assert row.patch_id == "forgep:p1" and row.track == "H"
    assert row.summary == "修改了代码：修好加法" and row.status == "personal_accepted"
    assert row.can_undo
    assert row.why == "修好加法；答案总被判错；花费 $0.50"
    for word in ("forge", "worktree", "sandbox", "DeepSeek"):
        assert word not in row.summary + row.why


@pytest.mark.parametrize("state,status", [("reverted", "rolled_back"),
                                          ("withdrawn", "rolled_back"),
                                          ("canary_failed", "rolled_back")])
def test_finished_rows_are_rolled_back(pool, state, status):
    with pool.connection() as conn:
        seed(conn, state=state, status="rolled_back")
        ps.set_state(conn, "p1", state, now=T, reason="代码改动导致启动失败，已撤回")
    [row] = _src(pool).list("u1")
    assert row.status == status and row.can_undo is False
    assert "代码改动导致启动失败，已撤回" in row.why


def test_a_patch_the_harness_reverted_as_harmful_carries_its_points(pool):
    """Like an h_ row: outcome harmful + the effect line, so it reads 已自动撤销."""
    from evowiki.store import harness_rsi as hr
    with pool.connection() as conn:
        seed(conn, "fc_p1", state="reverted", status="rolled_back")
        hr.add_patch_observation(conn, "u1", "fc_p1", "harmful", [], now=T)
    [row] = _src(pool).list("u1")
    assert row.status == "rolled_back" and row.outcome == "harmful"
    assert len(row.effect) == 1 and row.effect[0].change_id == "forgep:fc_p1" and row.effect_line
    [other] = _src(pool).list("u2")                 # another user's observation is not theirs
    assert other.outcome != "harmful"


@pytest.mark.parametrize("state", ["built", "awaiting_approval"])
def test_patches_under_review_show_as_awaiting_the_user(pool, state):
    with pool.connection() as conn:
        seed(conn, state=state)
    [row] = _src(pool).list("u2")
    assert row.patch_id == "forgep:p1" and row.status == "awaiting_user"
    assert row.summary == "等你审阅的代码改动：修好加法" and row.can_undo is False
    assert row.target == "代码"


def test_approved_but_not_landed_row(pool):
    with pool.connection() as conn:
        seed(conn, state="queued", status="promoted")
    [row] = _src(pool).list("u1")
    assert row.status == "personal_accepted" and row.can_undo is False
    assert row.summary == "已批准、还没生效的代码改动：修好加法"


def test_approve_and_discard_through_the_source(pool):
    with pool.connection() as conn:
        switch.set_enabled(conn, None, "patch", True, T)
        seed(conn, "a")
        seed(conn, "d", state="awaiting_approval", status="promoted")
        seed(conn, "l", state="landed", status="promoted")
    src = _src(pool)
    out = src.approve("u2", "forgep:a", now=T)
    assert out.status == "personal_accepted" and out.can_undo is False
    out = src.discard("u2", "forgep:d", now=T)
    assert out.status == "rejected" and out.can_undo is False
    assert out.summary == "没采用的代码改动：修好加法"          # its own line, not 修改了代码
    with pool.connection() as conn:
        assert ps.get_patch(conn, "a").state == "queued"
        assert ps.get_patch(conn, "d").state == "withdrawn"
    with pytest.raises(ValueError):
        src.approve("u1", "forgep:l", now=T)
    with pytest.raises(ValueError):
        src.discard("u1", "forgep:a", now=T)
    with pytest.raises(LookupError):
        src.approve("u1", "forgep:nope", now=T)


def test_approve_while_patches_are_off_carries_the_patches_off_code(pool):
    with pool.connection() as conn:
        switch.set_enabled(conn, None, "patch", False, T)
        seed(conn, "a")
    with pytest.raises(cs.Refused) as e:
        _src(pool).approve("u1", "forgep:a", now=T)
    assert e.value.error_code == "patches_off"
    with pool.connection() as conn:
        switch.set_enabled(conn, None, "patch", True, T)
        seed(conn, "l", state="landed", status="promoted")
    with pytest.raises(ValueError) as e2:                  # not under review: no code
        _src(pool).approve("u1", "forgep:l", now=T)
    assert getattr(e2.value, "error_code", None) is None


def test_revert_queued_shows_undone(pool):
    with pool.connection() as conn:
        seed(conn, state="revert_queued", status="promoted")
    [row] = _src(pool).list("u1")
    assert row.status == "rolled_back" and row.can_undo is False


def test_undo_by_any_viewer_and_errors(pool):
    with pool.connection() as conn:
        seed(conn, state="landed", status="promoted")
    src = _src(pool)
    out = src.undo("u2", "forgep:p1", now=T)          # u2 sees the row, so u2 may undo it
    assert out.status == "rolled_back" and out.can_undo is False
    with pool.connection() as conn:
        assert ps.get_patch(conn, "p1").state == "revert_queued"
    with pytest.raises(ValueError, match="撤销过"):
        src.undo("u1", "forgep:p1", now=T)
    with pytest.raises(LookupError):
        src.undo("u1", "forgep:nope", now=T)


def test_prefix_routing_and_sources(pool, monkeypatch):
    tool, patch = cs.ForgeChangeSource(pool, lambda: T), _src(pool)
    assert source_for([tool, patch], "forgep:p1") is patch
    assert source_for([tool, patch], "forge:u1:1") is tool
    monkeypatch.setattr(cs, "PLUGIN_ACCEPTS_H", False)
    assert cs.forge_change_sources(pool, lambda: T) == ()
    monkeypatch.setattr(cs, "PLUGIN_ACCEPTS_H", True)
    assert [type(s) for s in cs.forge_change_sources(pool, lambda: T)] == [
        cs.ForgeChangeSource, cs.ForgePatchChangeSource]
