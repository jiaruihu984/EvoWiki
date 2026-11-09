"""GET /wiki/harness shows the structural keys (H3 / H8 / H9 / H10 / H11) with approval and
options; GET /wiki/harness/structure lists the static allow-lists. Real Postgres, no LLM."""

import pytest
from fastapi.testclient import TestClient

from evowiki.deep.store import PgLibrary
from evowiki.harness import keys as hk
from evowiki.service.app import create_app
from evowiki.service.harness_defaults import defaults
from evowiki.store import harness as store
from tests.deep.fakes import component
from tests.service.conftest import H, signup

STEP = {"at": "deep.before_write", "kind": "delegate", "tier": "fast", "tools": ["read_k"],
        "instruction": "先读一遍知识点", "output_to": "context.notes", "max_tool_calls": 3}


@pytest.fixture
def client(services) -> TestClient:
    c = TestClient(create_app(services), base_url="http://127.0.0.1")
    signup(c, "u1")
    return c


def _by(client):
    return {k["name"]: k for k in client.get("/wiki/harness", headers=H("u1")).json()["keys"]}


def test_structural_keys_have_defaults_and_approval(client):
    by = _by(client)
    assert set(by) == {k.name for k in hk.KEYS}
    assert by["H3.deep.read_k"]["default"] is True and by["H3.deep.read_k"]["approval"] == "ask"
    assert by["H9.deep.before_write"]["default"] == []
    assert by["H11.deep.after_write"]["approval"] == "ask"
    assert by["H8.deep.spec.plan"]["default"] == ["items"]
    assert by["H8.deep.spec.write"]["default"] == ["items", "exemplar"]
    assert by["H8.deep.spec.write"]["options"] == ["items", "exemplar"]
    assert by["H8.deep.spec.write"]["approval"] == "auto"
    assert by["H8.deep.evoui_examples"]["default"] == []
    assert by["H10.deep.write.tier"]["default"] == "deep"
    assert by["H10.deep.write.tier"]["options"] == ["deep", "good"]
    assert by["H10.assistant.turn.tier"]["default"] in ("fast", "good")
    assert by["H10.bot.plan.tier"]["options"] == ["fast", "good"]
    assert by["H10.deep.plan.thinking"]["default"] is None
    assert by["H4.deep.max_rewrites"]["approval"] == "auto"
    assert {k: v["default"] for k, v in by.items()} == defaults()


def test_stored_h9_value_comes_back_as_a_list(client, services):
    with services.pool.connection() as conn:
        store.set_value(conn, "u1", "H9.deep.before_write", [STEP], source="user", why="t",
                        now=services.clock())
    k = _by(client)["H9.deep.before_write"]
    assert k["value"] == [STEP] and k["source"] == "user"


def test_structure_lists_allow_lists(client, services):
    s = client.get("/wiki/harness/structure", headers=H("u1")).json()
    tools = {(t["agent"], t["tool"]): t["locked"] for t in s["tools"]}
    assert tools[("deep", "finish")] is True
    assert tools[("deep", "read_spec")] is False
    assert tools[("bot", "suggest")] is True
    assert [h["at"] for h in s["hooks"]] == [
        "deep.before_write", "deep.after_write", "assistant.before_answer"]
    assert s["hooks"][1]["kinds"] == ["self_check"] and s["hooks"][1]["output_to"] == "context.must_fix"
    assert [x["step"] for x in s["steps"]] == [
        "deep.plan", "deep.write", "assistant.turn", "bot.plan", "forge.code"]
    assert s["steps"][3] == {"step": "bot.plan", "purpose": "bot_plan", "tiers": ["fast", "good"]}
    assert s["spec_parts"] == ["items", "exemplar"]
    assert s["hook_max_tool_calls"] == hk.HOOK_MAX_TOOL_CALLS
    assert isinstance(s["components"], list)


def test_structure_components_come_from_the_library(client, services, pool):
    with pool.connection() as conn:
        conn.execute("delete from component_library")
    services.library = PgLibrary(pool)
    services.library.put(component("zeta-demo"))
    services.library.put(component("pair-demo"))
    s = client.get("/wiki/harness/structure", headers=H("u1")).json()
    assert sorted(s["components"]) == ["pair-demo", "zeta-demo"]
    services.library = None
    assert client.get("/wiki/harness/structure", headers=H("u1")).json()["components"] == []


def test_structure_needs_a_known_user(client):
    assert client.get("/wiki/harness/structure", headers=H("nobody")).status_code in (401, 403, 404)
