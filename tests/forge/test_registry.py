import json
from datetime import UTC, datetime

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.content.models import Lang
from evowiki.forge.models import ForgeDisabled, ForgeNotFound, RunOutcome, SandboxStats
from evowiki.forge.registry import RegistryView, SandboxedTool, view
from evowiki.forge.sandbox import SandboxUnavailable
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user
from tests.forge.fakes import InProcessSandbox

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
TOOL = ("def run(args):\n"
        "    left, right = args['eq'].split('=')\n"
        "    return {'ok': sorted(left) == sorted(right), 'v': VERSION}\n")
SCHEMA = {"type": "object", "description": "判断化学方程式是否配平。只看元素个数。",
          "properties": {"eq": {"type": "string"}}, "required": ["eq"]}
CTX = ToolContext(user_id="u1", session_id="s1", selection_text="secret note")


def _user(conn, uid="u1"):
    create_user(conn, UserRow(user_id=uid, background="none", condition="S2", lang=Lang.EN,
                              created_at=T0))


def _cand(conn, cid, *, name="check_balanced", user="u1", status="built", version="v1",
          schema=SCHEMA):
    files = {"tool.py": f"VERSION = {version!r}\n" + TOOL, "schema.json": json.dumps(schema),
             "test_tool.py": "def test_x():\n    pass\n"}
    store.insert_candidate(conn, store.CandidateRow(
        id=cid, user_id=user, kind="tool", name=name, parent_id=None, spec={"name": name},
        evidence={"citations": ["signal:1"]}, files=files, build={"ok": status != "build_failed"},
        status=status, run_id=None, cost_usd=None, created_at=T0, updated_at=T0))


def _promote(conn, scope, entries):
    return store.append_registry(conn, scope, entries, {"op": "promote", "by": "test"}, T0)


def _run(v: RegistryView, name="check_balanced", eq="H2=H2"):
    [tool] = [t for t in v.tools_for("deep", {name}) if t.spec.name == name]
    return tool.run({"eq": eq}, CTX)


def test_view_pins_the_version(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        _promote(conn, "u1", {"check_balanced": "c1"})
        v = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True)
        _cand(conn, "c2", version="v2")
        _promote(conn, "u1", {"check_balanced": "c2"})
        fresh = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True)
    assert (v.version, fresh.version) == (1, 2)
    assert v.tools["check_balanced"].candidate_id == "c1"
    assert json.loads(_run(v).content)["v"] == "v1"
    assert json.loads(_run(fresh).content)["v"] == "v2"


def test_with_candidate_is_run_scoped(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        v = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True)
        v2 = v.with_candidate("c1")
        fresh = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True)
    assert "check_balanced" in v2.tools and v2.version == v.version == 0
    assert v.tools == {} and fresh.tools == {}
    assert json.loads(_run(v2).content) == {"ok": True, "v": "v1"}


def test_with_candidate_overrides_a_registered_tool(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        _cand(conn, "c2", version="v2")
        _promote(conn, "u1", {"check_balanced": "c1"})
        v2 = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True).with_candidate("c2")
    assert v2.version == 1 and json.loads(_run(v2).content)["v"] == "v2"


@pytest.mark.parametrize("status", ["build_failed", "building"])
def test_with_candidate_refuses_unbuilt(pool, status):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1", status=status)
        v = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True)
        with pytest.raises(ForgeNotFound):
            v.with_candidate("c1")
        with pytest.raises(ForgeNotFound):
            v.with_candidate("nope")


def test_with_candidate_refuses_another_users_candidate(pool):
    with pool.connection() as conn:
        _user(conn)
        _user(conn, "u2")
        _cand(conn, "c1", user="u2")
        _cand(conn, "g1", user=None, name="global_tool")
        v = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True)
        with pytest.raises(ForgeNotFound):
            v.with_candidate("c1")
        assert "global_tool" in v.with_candidate("g1").tools


def test_with_candidate_on_a_disabled_view_fails_closed(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        v = view(conn, "u1", sandbox=InProcessSandbox(), enabled=False)
        with pytest.raises(ForgeDisabled):
            v.with_candidate("c1")


def test_user_scope_shadows_global(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "g1", user=None, version="global")
        _cand(conn, "g2", user=None, name="other_tool", version="other")
        _cand(conn, "c1", version="mine")
        _promote(conn, "global", {"check_balanced": "g1", "other_tool": "g2"})
        _promote(conn, "global", {"check_balanced": "g1", "other_tool": "g2"})
        _promote(conn, "u1", {"check_balanced": "c1"})
        v = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True)
    assert (v.version, v.global_version) == (1, 2)
    assert {n: t.candidate_id for n, t in v.tools.items()} == {
        "check_balanced": "c1", "other_tool": "g2"}
    assert json.loads(_run(v).content)["v"] == "mine"


def test_tools_for_only_opted_in(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        _promote(conn, "u1", {"check_balanced": "c1"})
        v = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True)
    [tool] = v.tools_for("deep", {"check_balanced", "not_forged"})
    assert isinstance(tool, SandboxedTool) and tool.spec.name == "check_balanced"
    assert v.tools_for("deep", set()) == []


def test_tools_for_refuses_a_builtin_name(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1", name="read_page")
        _promote(conn, "u1", {"read_page": "c1"})
        v = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True)
    with pytest.raises(ValueError, match="read_page"):
        v.tools_for("deep", {"read_page"})
    with pytest.raises(ValueError):
        v.tools_for("nobody", set())


class Recording:
    policy = "read-only"

    def __init__(self, outcome: RunOutcome | Exception):
        self.outcome = outcome
        self.calls: list[tuple[dict, dict]] = []

    def run_tool(self, files, args):
        self.calls.append((dict(files), args))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def run_tests(self, files):
        raise AssertionError("not used")


def _one_tool(pool, sandbox):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        _promote(conn, "u1", {"check_balanced": "c1"})
        v = view(conn, "u1", sandbox=sandbox, enabled=True)
    [tool] = v.tools_for("deep", {"check_balanced"})
    return tool


def test_sandboxed_tool_runs_in_the_sandbox(pool):
    sb = Recording(RunOutcome(ok=True, output={"ok": True, "说明": "配平"},
                              stats=SandboxStats(wall_s=0.1, exit="ok")))
    tool = _one_tool(pool, sb)
    assert tool.spec.name == "check_balanced"
    assert tool.spec.description == SCHEMA["description"]
    assert tool.spec.parameters == SCHEMA
    assert (tool.spec.effect, tool.spec.category) == ("read", "read")
    res = tool.run({"eq": "H2=H2"}, CTX)
    assert res.error is None and json.loads(res.content) == {"ok": True, "说明": "配平"}
    [(files, args)] = sb.calls
    assert args == {"eq": "H2=H2"} and "tool.py" in files
    assert "secret note" not in json.dumps([files, args], ensure_ascii=False)


def test_sandboxed_tool_reports_sandbox_errors(pool):
    sb = Recording(RunOutcome(ok=False, error="ZeroDivisionError: division by zero",
                              stats=SandboxStats(wall_s=0.1, exit="error")))
    res = _one_tool(pool, sb).run({"eq": "x"}, CTX)
    assert res.error and "ZeroDivisionError" in res.error


def test_sandboxed_tool_reports_limits(pool):
    sb = Recording(RunOutcome(ok=False, stats=SandboxStats(wall_s=10.0, exit="timeout")))
    res = _one_tool(pool, sb).run({"eq": "x"}, CTX)
    assert res.error and "超时" in res.error


def test_sandboxed_tool_checks_args_against_the_schema_first(pool):
    sb = Recording(RunOutcome(ok=True, output={}, stats=SandboxStats(wall_s=0.0, exit="ok")))
    res = _one_tool(pool, sb).run({"eq": 3}, CTX)
    assert res.error and "eq" in res.error and sb.calls == []


def test_missing_sandbox_is_an_error_result_never_unsandboxed(pool):
    res = _one_tool(pool, Recording(SandboxUnavailable("no runtime"))).run({"eq": "x"}, CTX)
    assert res.error


def test_disabled_view_is_empty(pool):
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        _promote(conn, "u1", {"check_balanced": "c1"})
        v = view(conn, "u1", sandbox=InProcessSandbox(), enabled=False)
    assert v.tools_for("deep", {"check_balanced"}) == []
    assert v.tool_names("deep") == {}


def test_tool_names(pool):
    schema = {**SCHEMA, "properties": {"eq": {"type": "string"}, "a": {"type": "integer"}}}
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1", schema=schema)
        _promote(conn, "u1", {"check_balanced": "c1"})
        v = view(conn, "u1", sandbox=InProcessSandbox(), enabled=True)
    assert v.tool_names("deep") == {"check_balanced": ("a", "eq")}


def test_view_without_a_sandbox_never_runs_unsandboxed(pool, monkeypatch, tmp_path):
    monkeypatch.setenv("EVOWIKI_WASI_PYTHON", str(tmp_path / "nope"))
    with pool.connection() as conn:
        _user(conn)
        _cand(conn, "c1")
        _promote(conn, "u1", {"check_balanced": "c1"})
        v = view(conn, "u1", enabled=True)
    [tool] = v.tools_for("deep", {"check_balanced"})
    assert tool.run({"eq": "H2=H2"}, CTX).error
