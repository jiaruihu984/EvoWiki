import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from evowiki.content.models import Lang
from evowiki.forge import optin, registry, undo
from evowiki.forge.models import ForgeForbidden, ForgeNotFound, ToolExample, ToolRequest
from evowiki.forge.registry import view
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user
from tests.forge.fakes import InProcessSandbox

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
SCHEMA = {"type": "object", "description": "把数翻倍。",
          "properties": {"x": {"type": "number"}}, "required": ["x"]}


def _cand(conn, cid, *, name="double", user=None):
    files = {"tool.py": "def run(args):\n    return {'y': args['x'] * 2}\n",
             "schema.json": json.dumps(SCHEMA), "test_tool.py": "def test_x():\n    pass\n"}
    store.insert_candidate(conn, store.CandidateRow(
        id=cid, user_id=user, kind="tool", name=name, parent_id=None,
        spec={"name": name, "title": "翻倍"}, evidence={"note": "常要翻倍"}, files=files,
        build={"ok": True}, status="built", run_id=None, cost_usd=0.01, created_at=T0,
        updated_at=T0))


@pytest.fixture
def users(pool):
    with pool.connection() as conn:
        for uid in ("u1", "u2"):
            create_user(conn, UserRow(user_id=uid, background="none", condition="S2",
                                      lang=Lang.EN, created_at=T0))


@pytest.fixture
def promoted_global_double(pool, users):
    with pool.connection() as conn:
        _cand(conn, "g1")
        v = registry.promote(conn, "g1", gate_ref="gate:1", by="round", why="好",
                             gate_ok=lambda ref: True, now=T0)
    return registry.change_id("global", v)


def _tools(conn, uid):
    return view(conn, uid, enabled=True, sandbox=InProcessSandbox()).tools


# --- opt-in --------------------------------------------------------------------------------------

def test_default_off(pool, users):
    with pool.connection() as conn:
        assert optin.opted_in(conn, "u1", "deep", ["double", "request_tool"], {}) == frozenset()


def test_local_then_global_then_pack(pool, users):
    with pool.connection() as conn:
        optin.set_local(conn, None, "deep", "double", True, T0)
        assert optin.opted_in(conn, "u1", "deep", ["double"], {}) == {"double"}
        optin.set_local(conn, "u1", "deep", "double", False, T0)
        assert optin.opted_in(conn, "u1", "deep", ["double"], {}) == frozenset()
        assert optin.opted_in(conn, "u1", "deep", ["double"],
                              {"H3.deep.double": True}) == {"double"}
        # pack off beats a local on
        optin.set_local(conn, "u1", "deep", "double", True, T0)
        assert optin.opted_in(conn, "u1", "deep", ["double"],
                              {"H3.deep.double": False}) == frozenset()
        # other agents are separate
        assert optin.opted_in(conn, "u1", "assistant", ["double"], {}) == frozenset()


def test_non_bool_is_off(pool, users):
    with pool.connection() as conn:
        store.set_setting(conn, "u1", "opt_in.deep.double", "yes", T0)
        optin.set_local(conn, None, "deep", "double", True, T0)
        assert optin.opted_in(conn, "u1", "deep", ["double"], {}) == frozenset()
        assert optin.opted_in(conn, "u2", "deep", ["double"], {"H3.deep.double": 1}) == frozenset()
        # an unset pack key (None) falls through to the stored settings
        assert optin.opted_in(conn, "u2", "deep", ["double"],
                              {"H3.deep.double": None}) == {"double"}


def test_request_tool_on(pool, users):
    with pool.connection() as conn:
        assert optin.request_tool_on(conn, "u1", "assistant", {}) is False
        optin.set_local(conn, "u1", "assistant", optin.META, True, T0)
        assert optin.request_tool_on(conn, "u1", "assistant", {}) is True
        assert optin.request_tool_on(conn, "u1", "assistant",
                                     {"H3.assistant.request_tool": False}) is False


def test_optin_refuses_global_user_and_bad_input(pool, users):
    with pool.connection() as conn:
        with pytest.raises(ValueError):
            optin.opted_in(conn, "global", "deep", ["double"], {})
        with pytest.raises(ValueError):
            optin.set_local(conn, "global", "deep", "double", True, T0)
        with pytest.raises(ValueError):
            optin.set_local(conn, "u1", "nope", "double", True, T0)
        with pytest.raises(TypeError):
            optin.set_local(conn, "u1", "deep", "double", "yes", T0)    # type: ignore[arg-type]


# --- masks and global rollback -------------------------------------------------------------------

def test_user_undo_of_global_change_masks_only_that_user(pool, users, promoted_global_double):
    cid = promoted_global_double          # change id "forge:global:1"
    assert cid == "forge:global:1"
    with pool.connection() as conn:
        undo.undo(conn, "u1", cid, operator=False)
        assert "double" not in _tools(conn, "u1")
        assert "double" in _tools(conn, "u2")
        row = next(r for r in undo.changes(conn, "u1") if r["change_id"] == cid)
        assert row["can_undo"] is False and "已为你撤下" in row["why"]
        row2 = next(r for r in undo.changes(conn, "u2") if r["change_id"] == cid)
        assert row2["can_undo"] is True and "已为你撤下" not in row2["why"]
        assert store.registry_head(conn, "global")[1] == {"double": "g1"}
        assert store.get_candidate(conn, "g1").status == "promoted"
        assert undo.undo(conn, "u1", cid, operator=False) is None          # twice: no-op


def test_newer_global_promote_shows_again(pool, users, promoted_global_double):
    with pool.connection() as conn:
        undo.undo(conn, "u1", promoted_global_double, operator=False)
        _cand(conn, "g2")
        registry.promote(conn, "g2", gate_ref="gate:2", by="round", why="好",
                         gate_ok=lambda ref: True, now=T0)
        assert _tools(conn, "u1")["double"].candidate_id == "g2"


def test_user_undo_of_unknown_global_change(pool, users, promoted_global_double):
    with pool.connection() as conn, pytest.raises(ForgeNotFound):
        undo.undo(conn, "u1", "forge:global:9", operator=False)


def test_global_rollback_needs_operator(pool, users, promoted_global_double):
    with pool.connection() as conn, pytest.raises(ForgeForbidden):
        registry.rollback(conn, "u1", promoted_global_double, by="user")
    with pool.connection() as conn:
        assert store.registry_head(conn, "global")[1] == {"double": "g1"}
        assert registry.rollback(conn, "u1", promoted_global_double, by="observe", operator=True)
        assert "double" not in _tools(conn, "u2")


def test_operator_env(pool, users, promoted_global_double, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FORGE_OPERATOR", "u1")
    with pool.connection() as conn:
        undo.undo(conn, "u1", promoted_global_double)
        assert "double" not in _tools(conn, "u2")


def test_operator_env_other_user_only_masks(pool, users, promoted_global_double, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FORGE_OPERATOR", "u2")
    with pool.connection() as conn:
        undo.undo(conn, "u1", promoted_global_double)
        assert "double" not in _tools(conn, "u1")
        assert "double" in _tools(conn, "u2")


# --- reserved names ------------------------------------------------------------------------------

def test_reserved_names_refused(pool, users):
    assert registry.RESERVED == frozenset({"call_tool", "find_tool", "request_tool"})
    with pytest.raises(ValidationError):
        ToolRequest(name="find_tool", title="找工具", purpose="x", agent="deep",
                    examples=(ToolExample(args={}, expect={}),))
    with pool.connection() as conn:
        _cand(conn, "r1", name="call_tool", user="u1")
        store.append_registry(conn, "u1", {"call_tool": "r1"}, {"op": "promote", "by": "t"}, T0)
        v = view(conn, "u1", enabled=True, sandbox=InProcessSandbox())
    with pytest.raises(ValueError):
        v.tools_for("deep", {"call_tool"})
    with pytest.raises(ValueError):
        v.tool_names("deep")
