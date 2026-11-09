import json
from datetime import UTC, datetime

import pytest

from evowiki.content.models import Lang
from evowiki.forge import change_source as cs
from evowiki.forge.registry import promote, view
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user
from evowiki.teach.change_source import source_for
from tests.forge.fakes import InProcessSandbox

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
SCHEMA = {"type": "object", "description": "判断化学方程式是否配平。只看元素个数。",
          "properties": {"eq": {"type": "string"}}, "required": ["eq"]}


def _setup(pool, cost=0.01, user="u1"):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2", lang=Lang.EN,
                                  created_at=T0))
        files = {"tool.py": "def run(args):\n    return {}\n", "schema.json": json.dumps(SCHEMA),
                 "test_tool.py": "def test_x():\n    pass\n"}
        store.insert_candidate(conn, store.CandidateRow(
            id="c1", user_id=user, kind="tool", name="check_balanced", parent_id=None,
            spec={"name": "check_balanced", "title": "配平检查"},
            evidence={"note": "答案总被判错"}, files=files, build={"ok": True}, status="built",
            run_id=None, cost_usd=cost, created_at=T0, updated_at=T0))
        promote(conn, "c1", gate_ref="gate:1", by="improver", why="好",
                gate_ok=lambda r: True, now=T0)


def _src(pool):
    return cs.ForgeChangeSource(pool, lambda: T0)


def test_row_and_undo(pool):
    _setup(pool)
    src = _src(pool)
    [row] = src.list("u1")
    assert row.track == "H" and row.summary == "新增了工具「配平检查」" and row.can_undo
    assert row.patch_id == "forge:u1:1" and row.status == "personal_accepted"
    assert "答案总被判错" in row.why and "花费 $0.01" in row.why and "forge" not in row.summary
    out = src.undo("u1", "forge:u1:1", now=T0)
    assert out.status == "rolled_back" and out.can_undo is False
    with pool.connection() as conn:
        assert "check_balanced" not in view(conn, "u1", enabled=True, sandbox=InProcessSandbox()).tools
    with pytest.raises(ValueError):
        src.undo("u1", "forge:u1:1", now=T0)
    with pytest.raises(LookupError):
        src.undo("u1", "forge:u1:77", now=T0)


def test_unknown_cost(pool):
    _setup(pool, cost=None)
    assert "花费未知" in _src(pool).list("u1")[0].why


def test_global_masking_undo(pool):
    _setup(pool, user=None)
    out = _src(pool).undo("u1", "forge:global:1", now=T0)
    assert out.can_undo is False and out.status == "rolled_back" and "已为你撤下" in out.why


def test_gated_by_plugin_flag(pool, monkeypatch):
    assert cs.PLUGIN_ACCEPTS_H is True            # harness phase 2 merged: the plugin shows track H
    monkeypatch.setattr(cs, "PLUGIN_ACCEPTS_H", False)
    assert cs.forge_change_sources(pool, lambda: T0) == ()
    monkeypatch.setattr(cs, "PLUGIN_ACCEPTS_H", True)
    sources = cs.forge_change_sources(pool, lambda: T0)
    assert len(sources) == 2                      # tools, and L2 patches (`forgep:`)
    assert source_for(sources, "forge:u1:3") is sources[0]
    assert source_for(sources, "forgep:c1") is sources[1]
    assert source_for(sources, "h_1") is None
