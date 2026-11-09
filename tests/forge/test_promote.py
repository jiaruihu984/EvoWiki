import json
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.content.models import Lang
from evowiki.forge import switch, undo
from evowiki.forge.models import ForgeNotFound, PromoteRefused
from evowiki.forge.registry import promote, rollback, view
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user
from tests.forge.fakes import InProcessSandbox

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
SCHEMA = {"type": "object", "description": "判断化学方程式是否配平。只看元素个数。",
          "properties": {"eq": {"type": "string"}}, "required": ["eq"]}
OK = lambda ref: True


def _user(conn, uid="u1"):
    create_user(conn, UserRow(user_id=uid, background="none", condition="S2", lang=Lang.EN,
                              created_at=T0))


def _cand(conn, cid, *, name="check_balanced", user="u1", status="built"):
    files = {"tool.py": "def run(args):\n    return {}\n", "schema.json": json.dumps(SCHEMA),
             "test_tool.py": "def test_x():\n    pass\n"}
    store.insert_candidate(conn, store.CandidateRow(
        id=cid, user_id=user, kind="tool", name=name, parent_id=None, spec={"name": name, "title": "配平检查"},
        evidence={"citations": ["signal:1"], "note": "答案总被判错"}, files=files,
        build={"ok": True}, status=status, run_id=None, cost_usd=0.01, created_at=T0,
        updated_at=T0))


def _p(conn, cid, **kw):
    return promote(conn, cid, gate_ref="gate:1", by="improver", why="好", gate_ok=OK, **kw)


def test_promote_adds_entry_and_marks_promoted(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        assert _p(conn, "c1") == 1
        assert store.registry_head(conn, "u1") == (1, {"check_balanced": "c1"})
        assert store.get_candidate(conn, "c1").status == "promoted"
        change = store.registry_history(conn, "u1")[0].change
        assert change["gate_ref"] == "gate:1" and change["by"] == "improver"


def test_global_candidate_promotes_into_global_scope(pool):
    with pool.connection() as conn:
        _cand(conn, "g1", user=None)
        assert _p(conn, "g1") == 1
        assert store.registry_head(conn, "global")[1] == {"check_balanced": "g1"}


@pytest.mark.parametrize("status,ref,ok", [("built", "gate:1", False), ("build_failed", "gate:1", True),
                                           ("built", "", True), ("built", "  ", True),
                                           ("promoted", "gate:1", True)])
def test_promote_refused(pool, status, ref, ok):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1", status=status)
        with pytest.raises(PromoteRefused):
            promote(conn, "c1", gate_ref=ref, by="x", why="y", gate_ok=lambda r: ok)
        assert store.registry_head(conn, "u1")[0] == 0
        assert store.get_candidate(conn, "c1").status == status


def test_promote_unknown_candidate(pool):
    with pool.connection() as conn, pytest.raises(ForgeNotFound):
        _p(conn, "nope")


def test_same_name_replaces_entry(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        _cand(conn, "c2")
        _p(conn, "c1")
        assert _p(conn, "c2") == 2
        assert store.registry_head(conn, "u1")[1] == {"check_balanced": "c2"}
        assert store.get_candidate(conn, "c1").status == "promoted"


def test_rollback_restores_previous_entries(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        _cand(conn, "c2")
        _p(conn, "c1")
        _p(conn, "c2")
        assert rollback(conn, "u1", "forge:u1:2", by="user") == 3
        assert store.registry_head(conn, "u1") == (3, {"check_balanced": "c1"})
        assert store.get_candidate(conn, "c2").status == "rolled_back"
        assert rollback(conn, "u1", "forge:u1:2", by="user") is None      # already undone
        assert store.registry_head(conn, "u1")[0] == 3
        assert rollback(conn, "u1", "forge:u1:1", by="user") == 4
        assert store.registry_head(conn, "u1") == (4, {})


def test_rollback_unknown_or_foreign_change(pool):
    with pool.connection() as conn:
        _user(conn)
        _user(conn, "u2")
        _cand(conn, "c1")
        _p(conn, "c1")
        for bad in ("forge:u1:9", "forge:u2:1", "nonsense", "forge:u1:x"):
            with pytest.raises(ForgeNotFound):
                rollback(conn, "u1", bad, by="user")
        rollback(conn, "u1", "forge:u1:1", by="user")
        with pytest.raises(ForgeNotFound):                                # a rollback row itself
            rollback(conn, "u1", "forge:u1:2", by="user")


def test_kill_switch_hides_and_restores_tools(pool):
    with pool.connection() as conn:
        _user(conn)
        _user(conn, "u2")
        _cand(conn, "c1")
        _p(conn, "c1")
        assert switch.enabled(conn, "u1", "tool")
        switch.set_enabled(conn, "u1", "tool", False)
        assert not switch.enabled(conn, "u1", "tool") and switch.enabled(conn, "u2", "tool")
        off = view(conn, "u1", sandbox=InProcessSandbox(), enabled=switch.enabled(conn, "u1", "tool"))
        assert off.tools_for("deep", {"check_balanced"}) == [] and not off.tool_names("deep")
        switch.set_enabled(conn, "u1", "tool", True)
        on = view(conn, "u1", sandbox=InProcessSandbox(), enabled=switch.enabled(conn, "u1", "tool"))
        assert [t.spec.name for t in on.tools_for("deep", {"check_balanced"})] == ["check_balanced"]
        switch.set_enabled(conn, None, "tool", False)                     # global off wins
        assert not switch.enabled(conn, "u2", "tool") and not switch.enabled(conn, "u1", "tool")
        assert not switch.enabled(conn, "u1", "patch")                  # L2 default off


def test_undo_change_rows_and_undo(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        _cand(conn, "c2", name="other")
        _cand(conn, "g1", name="shared", user=None)
        _p(conn, "c1", now=T0)
        _p(conn, "c2", now=T0 + timedelta(minutes=1))
        _p(conn, "g1", now=T0 + timedelta(minutes=2))
        rows = undo.changes(conn, "u1")
        assert [r["change_id"] for r in rows] == ["forge:global:1", "forge:u1:2", "forge:u1:1"]
        r = rows[2]
        assert r.pop("at") == T0 and r.pop("undone") is False
        assert r == {"change_id": "forge:u1:1", "text": "新增了工具「配平检查」",
                     "why": "判断化学方程式是否配平；答案总被判错", "cost_usd": 0.01, "can_undo": True}
        assert undo.undo(conn, "u1", "forge:u1:2") == 3
        assert {r["change_id"]: r["can_undo"] for r in undo.changes(conn, "u1")}["forge:u1:2"] is False
        assert store.registry_head(conn, "u1")[1] == {"check_balanced": "c1"}
        with pytest.raises(ForgeNotFound):
            undo.undo(conn, "u1", "forge:u1:77")
        assert all("forge" not in x["text"] for x in rows)


def test_rollback_touches_only_that_change(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "a", name="tool_a")
        _cand(conn, "b", name="tool_b")
        _p(conn, "a")
        _p(conn, "b")
        assert undo.undo(conn, "u1", "forge:u1:1") == 3                    # undo A
        assert store.registry_head(conn, "u1") == (3, {"tool_b": "b"})   # B still live
        assert store.get_candidate(conn, "a").status == "rolled_back"
        assert store.get_candidate(conn, "b").status == "promoted"
        assert undo.undo(conn, "u1", "forge:u1:1") is None                 # twice: no-op
        assert store.registry_head(conn, "u1") == (3, {"tool_b": "b"})


def test_rollback_of_a_replaced_promote_keeps_the_later_one(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        _cand(conn, "c2")
        _p(conn, "c1")
        _p(conn, "c2")
        rollback(conn, "u1", "forge:u1:1", by="user")                     # c1 already replaced
        assert store.registry_head(conn, "u1")[1] == {"check_balanced": "c2"}
        rollback(conn, "u1", "forge:u1:2", by="user")                     # c1 was undone: drop
        assert store.registry_head(conn, "u1")[1] == {}
