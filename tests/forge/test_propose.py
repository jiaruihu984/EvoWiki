import json
from datetime import UTC, datetime

import pytest

from evowiki.content.models import Lang
from evowiki.forge import switch
from evowiki.forge.api import ForgeDeps, propose
from evowiki.forge.models import (
    Evidence,
    ForgeDisabled,
    ForgeNotFound,
    ForgeResult,
    PatchRequest,
    ToolExample,
    ToolRequest,
)
from evowiki.forge.sandbox import SandboxUnavailable
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.store import forge as store
from evowiki.store.users import UserRow, create_user
from tests.forge.fakes import InProcessSandbox, ScriptedConverse, accounted_fake

T0 = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
REQ = ToolRequest(name="double", title="翻倍", purpose="把整数 n 翻倍", agent="deep",
                  examples=(ToolExample(args={"n": 2}, expect={"n": 4}),))
EVI = Evidence(citations=("signal:s1",), note="用户反复手算翻倍")
TOOL = "def run(args):\n    return {'n': args['n'] * 2}\n"
BAD_TOOL = "def run(args):\n    return {'n': args['n'] * 3}\n"
TESTS = "import tool\n\ndef test_example():\n    assert tool.run({'n': 2}) == {'n': 4}\n"
SCHEMA = json.dumps({"type": "object", "description": "把 n 翻倍",
                     "properties": {"n": {"type": "integer"}}, "required": ["n"]})


def call(i, name, **args):
    return ToolCall(id=f"t{i}", name=name, args=args)


def turn(*calls):
    return ChatTurn(text="", tool_calls=tuple(calls))


def script(tool=TOOL):
    return ScriptedConverse(
        turn(call(1, "write_file", path="tool.py", content=tool)),
        turn(call(2, "write_file", path="test_tool.py", content=TESTS)),
        turn(call(3, "write_file", path="schema.json", content=SCHEMA)),
        turn(call(4, "submit", summary="写好了")))


class Priced:
    """A scripted converse whose calls carry the given costs (None = no price)."""

    def __init__(self, inner, costs):
        self.inner, self.costs = inner, list(costs)

    def __call__(self, req):
        t, c = self.inner(req)
        return t, c.model_copy(update={"cost": self.costs.pop(0)})


class Trace:
    def __init__(self):
        self.runs = []

    def start(self, run_id, **kw):
        self.runs.append(run_id)

    def model(self, *a, **kw): ...

    def tool(self, *a, **kw): ...

    def finish(self, run_id, *, stop, error): ...


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"fc_{self.n}"


@pytest.fixture
def user(pool):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2",
                                  lang=Lang.EN, created_at=T0))
    return "u1"


def deps(pool, converse, sandbox=None, trace=None):
    return ForgeDeps(pool=pool, converse=accounted_fake(converse), sandbox=sandbox or InProcessSandbox(),
                     trace=trace or Trace(), now=lambda: T0, new_id=Ids())


def row(pool, cid):
    with pool.connection() as conn:
        return store.get_candidate(conn, cid)


def count(pool):
    with pool.connection() as conn:
        return len(store.archive(conn, "u1")) + len(store.archive(conn, None))


def test_happy_path_archives_a_built_candidate(pool, user):
    trace = Trace()
    conv = Priced(script(), [0.01, 0.02, 0.03, 0.04])
    res = propose("tool", REQ, EVI, user_id=user, deps=deps(pool, conv, trace=trace))
    assert isinstance(res, ForgeResult) and res.kind == "tool"
    assert res.build.ok and res.build.examples_passed == 1 and res.build.tests_passed == 1
    assert res.build.run_id == trace.runs[0] == f"forge:{res.candidate_id}"
    assert res.build.cost_usd == pytest.approx(0.10)
    r = row(pool, res.candidate_id)
    assert r.status == "built" and r.user_id == "u1" and r.name == "double"
    assert r.files["tool.py"] == TOOL and "schema.json" in r.files
    assert r.spec["purpose"] == "把整数 n 翻倍"
    assert r.evidence == {"citations": ["signal:s1"], "note": "用户反复手算翻倍"}
    assert r.build["ok"] is True and r.build["run_id"] == r.run_id == res.build.run_id
    assert r.cost_usd == pytest.approx(0.10)


def test_cost_is_none_when_a_call_has_no_price(pool, user):
    conv = Priced(script(), [0.01, None, 0.03, 0.04])
    res = propose("tool", REQ, EVI, user_id=user, deps=deps(pool, conv))
    assert res.build.cost_usd is None and row(pool, res.candidate_id).cost_usd is None


def test_failed_build_is_archived_without_exception(pool, user):
    conv = ScriptedConverse(
        turn(call(1, "write_file", path="tool.py", content=BAD_TOOL)),
        turn(call(2, "write_file", path="test_tool.py", content=TESTS)),
        turn(call(3, "write_file", path="schema.json", content=SCHEMA)),
        turn(call(4, "submit", summary="好了")))
    res = propose("tool", REQ, EVI, user_id=user, deps=deps(pool, conv))
    assert res.build.ok is False and res.build.tests_failed == 1
    r = row(pool, res.candidate_id)
    assert r.status == "build_failed" and r.build["ok"] is False
    assert r.files["tool.py"] == BAD_TOOL


def test_examples_are_checked_on_the_host_even_when_tests_pass(pool, user):
    weak_tests = "import tool\n\ndef test_runs():\n    assert tool.run({'n': 1})\n"
    conv = ScriptedConverse(
        turn(call(1, "write_file", path="tool.py", content=BAD_TOOL)),
        turn(call(2, "write_file", path="test_tool.py", content=weak_tests)),
        turn(call(3, "write_file", path="schema.json", content=SCHEMA)),
        turn(call(4, "submit", summary="好了")))
    res = propose("tool", REQ, EVI, user_id=user, deps=deps(pool, conv))
    assert res.build.tests_failed == 0 and res.build.examples_failed == 1
    assert not res.build.ok and row(pool, res.candidate_id).status == "build_failed"


def test_patch_needs_patch_deps(pool, user):
    conv = ScriptedConverse()
    with pytest.raises(TypeError, match="patch_deps"):
        propose("patch", PatchRequest(goal="g", title="改一下", failing_case="f"), EVI,
                user_id=user, deps=deps(pool, conv))
    assert conv.requests == [] and count(pool) == 0


def test_kill_switch_setting_off(pool, user):
    with pool.connection() as conn:
        store.set_setting(conn, user, switch.key("tool"), False, T0)
    conv = script()
    with pytest.raises(ForgeDisabled):
        propose("tool", REQ, EVI, user_id=user, deps=deps(pool, conv))
    assert conv.requests == [] and count(pool) == 0


def test_kill_switch_env_off_wins(pool, user, monkeypatch):
    with pool.connection() as conn:
        store.set_setting(conn, user, switch.key("tool"), True, T0)
    monkeypatch.setenv("EVOWIKI_FORGE", "off")
    conv = script()
    with pytest.raises(ForgeDisabled):
        propose("tool", REQ, EVI, user_id=user, deps=deps(pool, conv))
    assert conv.requests == [] and count(pool) == 0


def test_switch_defaults(pool, user, monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)
    with pool.connection() as conn:
        assert switch.enabled(conn, user, "tool") is True
        assert switch.enabled(conn, user, "patch") is False
        store.set_setting(conn, user, switch.key("tool"), "yes", T0)
        assert switch.enabled(conn, user, "tool") is False      # not a JSON bool → off


def test_unknown_parent(pool, user):
    conv = script()
    with pytest.raises(ForgeNotFound):
        propose("tool", REQ.model_copy(update={"parent": "fc_nope"}), EVI, user_id=user,
                deps=deps(pool, conv))
    assert conv.requests == [] and count(pool) == 0


def test_known_parent_seeds_the_workspace(pool, user):
    first = propose("tool", REQ, EVI, user_id=user, deps=deps(pool, script()))
    conv = ScriptedConverse(turn(call(1, "submit", summary="沿用")))
    d = ForgeDeps(pool=pool, converse=accounted_fake(conv), sandbox=InProcessSandbox(), trace=Trace(),
                  now=lambda: T0, new_id=lambda: "fc_child")
    res = propose("tool", REQ.model_copy(update={"parent": first.candidate_id}), EVI,
                  user_id=user, deps=d)
    assert "- test_tool.py" in conv.requests[0].messages[0].text   # parent's files listed
    r = row(pool, res.candidate_id)
    assert r.parent_id == first.candidate_id and r.files["tool.py"] == TOOL
    assert res.build.ok and r.status == "built"


def test_other_users_parent_is_not_found(pool, user):
    first = propose("tool", REQ, EVI, user_id=user, deps=deps(pool, script()))
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u2", background="none", condition="S2",
                                  lang=Lang.EN, created_at=T0))
    with pytest.raises(ForgeNotFound):
        propose("tool", REQ.model_copy(update={"parent": first.candidate_id}), EVI,
                user_id="u2", deps=deps(pool, script()))


def test_model_error_marks_failed_and_reraises(pool, user):
    conv = ScriptedConverse(turn(call(1, "write_file", path="tool.py", content=TOOL)),
                            RuntimeError("provider down"))
    with pytest.raises(RuntimeError, match="provider down"):
        propose("tool", REQ, EVI, user_id=user, deps=deps(pool, conv))
    r = row(pool, "fc_1")
    assert r.status == "build_failed" and r.build["ok"] is False
    assert "provider down" in r.build["reason"] and r.build["run_id"] == "forge:fc_1"


class NoSandbox(InProcessSandbox):
    def run_tests(self, files):
        raise SandboxUnavailable("no runtime")


def test_missing_sandbox_fails_closed(pool, user):
    with pytest.raises(SandboxUnavailable):
        propose("tool", REQ, EVI, user_id=user, deps=deps(pool, script(), sandbox=NoSandbox()))
    assert row(pool, "fc_1").status == "build_failed"


def test_global_scope_row_has_no_user(pool, user):
    res = propose("tool", REQ.model_copy(update={"scope": "global"}), EVI, user_id=user,
                  deps=deps(pool, script()))
    assert row(pool, res.candidate_id).user_id is None
