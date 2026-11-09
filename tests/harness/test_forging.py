"""Forged tools from the harness improver (harness 3b, Task 7): the improver's `forge_tool` (offered
only with forge hooks and the forge's tool switch on; one per run; a built candidate joins the
forged tools read_pack lists), the forged-name check on proposed diffs, `forging.promote_for` /
`undo_tool` / `harness_promotes` on real forge rows, and diff.refuse's fail-closed pairing.
The forge's `propose` is faked (it inserts a real built candidate row); a scripted improver model;
nothing reaches a real provider or a sandbox."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from evowiki.forge import limits, registry, switch
from evowiki.forge.models import (
    BuildReport,
    ForgeDisabled,
    ForgeResult,
    PromoteRefused,
    SandboxStats,
)
from evowiki.harness import forging, keys
from evowiki.harness.diff import KeyOp, PackDiff, refuse
from evowiki.harness.forging import ForgeHooks, build_hooks, harness_promotes, promote_for
from evowiki.harness.improver import FORGE_ONCE, FORGE_UNKNOWN, ImproverRun, run_improver
from evowiki.harness.pack import EMPTY, HarnessPack
from evowiki.harness.replay import ReplayDeps, replay
from evowiki.llm.usage import Account
from evowiki.store import forge as fstore
from evowiki.store import harness_rsi
from tests.harness.test_improver import CASES, Improver, deps, mturn, tc
from tests.runtime.helpers import make_user

U = "u1"
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
CID = "fc_t1"
SCHEMA = {"type": "object", "description": "判断两个化学式是否等价。只看元素个数。",
          "properties": {"a": {"type": "string", "description": "第一个式子"},
                         "b": {"type": "string", "description": "第二个式子"}},
          "required": ["a", "b"]}
FORGE_ARGS = {"name": "t1", "title": "等价检查", "does": "写页面时检查两个式子是不是同一个意思",
              "purpose": "判断两个式子是否等价",
              "examples": [{"args": {"a": "H2O", "b": "OH2"}, "expect": {"same": True}}],
              "citations": ["signal:sig1"]}
NONE = mturn(tc("compose", 99, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"],
                rationale="只是看看"))


@pytest.fixture(autouse=True)
def _no_forge_env(monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)


def ftc(i: int, **over) -> dict:
    """A forge_tool call (`tc` takes the tool's name as its own `name`)."""
    return {"id": f"forge_tool-{i}", "name": "forge_tool", "args": {**FORGE_ARGS, **over}}


def insert_candidate(conn, cid=CID, *, name="t1", user=U, status="built", schema=SCHEMA,
                     agent="deep"):
    files = {"tool.py": "def run(args):\n    return {}\n", "schema.json": json.dumps(schema),
             "test_tool.py": "def test_x():\n    pass\n"}
    fstore.insert_candidate(conn, fstore.CandidateRow(
        id=cid, user_id=user, kind="tool", name=name, parent_id=None,
        spec={"name": name, "title": "等价检查", "does": "写页面时检查两个式子是不是同一个意思",
              "agent": agent},
        evidence={"citations": ["signal:sig1"], "note": ""}, files=files, build={"ok": True},
        status=status, run_id=None, cost_usd=0.03, created_at=NOW, updated_at=NOW))


class FakeForge:
    """The forge side: `propose` inserts a real built candidate row (or a failed build) and
    returns its report; it calls the converse it was given once when `converse` (so the call goes
    through the caller's gateway). Every deps_for call is kept."""

    def __init__(self, pool, *, ok=True, cid=CID, converse=False, error=None):
        self.pool, self.ok, self.cid, self.converse, self.error = pool, ok, cid, converse, error
        self.proposed: list = []
        self.bound: list = []

    def propose(self, kind, req, evidence, *, user_id, deps):
        self.proposed.append((kind, req, evidence, user_id, deps))
        if self.error is not None:
            raise self.error
        if self.converse:
            from evowiki.llm.chat import ChatMessage, ChatRequest
            deps.converse(ChatRequest(purpose="forge.tool", prompt_version="forge-tool@v2",
                                      subject=req.name, system="s",
                                      messages=(ChatMessage(role="user", text="造"),)))
        with self.pool.connection() as conn:
            insert_candidate(conn, self.cid, name=req.name,
                             status="built" if self.ok else "build_failed")
        return ForgeResult(candidate_id=self.cid, kind="tool", build=BuildReport(
            ok=self.ok, tests_passed=3, tests_failed=0, examples_passed=1, examples_failed=0,
            lint_ok=True, schema_ok=True, sandbox=SandboxStats(wall_s=1.0, exit="ok"),
            cost_usd=0.03, reason="通过" if self.ok else "例子没通过"))

    def hooks(self, pool) -> ForgeHooks:
        runtime = SimpleNamespace(pool=pool, sandbox=None, trace=None)
        real = build_hooks(runtime)                    # the service's names / docs / switch

        def deps_for(gw, account, pack):
            self.bound.append((gw, account, pack))
            return real.deps_for(gw, account, pack)
        return replace(real, deps_for=deps_for, propose=self.propose)


def _user(pool):
    from evowiki.store.users import get_user
    with pool.connection() as conn:
        known = get_user(conn, U) is not None
    if not known:
        make_user(pool, U)


def _run(pool, forge: FakeForge | None, *steps, hooks=None):
    _user(pool)
    imp = Improver(*steps)
    h = hooks if hooks is not None else (forge.hooks(pool) if forge is not None else None)
    comp = run_improver(U, CASES, deps=replace(deps(pool, imp), forge=h))
    return imp, comp


# ---- offered or not ----------------------------------------------------------------------------

def test_forge_tool_is_offered_only_with_hooks_and_the_switch_on(pool):
    imp, _ = _run(pool, None, NONE)
    assert "forge_tool" not in [t.name for t in imp.requests[0].tools]
    imp, _ = _run(pool, FakeForge(pool), NONE)
    tool = next(t for t in imp.requests[0].tools if t.name == "forge_tool")
    for word in ("claude", "deepseek", "sonnet", "opus"):
        assert word not in tool.description.lower()
    with pool.connection() as conn:
        switch.set_enabled(conn, U, "tool", False, NOW)
    imp, _ = _run(pool, FakeForge(pool), NONE)
    assert "forge_tool" not in [t.name for t in imp.requests[0].tools]


def test_an_unreadable_registry_offers_no_forge_tool(pool):
    def broken(conn, user_id, agent):
        raise LookupError("registry entry points at a missing candidate")
    hooks = replace(FakeForge(pool).hooks(pool), read=broken)
    imp, _ = _run(pool, None, NONE, hooks=hooks)
    assert "forge_tool" not in [t.name for t in imp.requests[0].tools]


# ---- the tool ----------------------------------------------------------------------------------

def test_forge_tool_builds_once_and_the_built_tool_can_be_proposed(pool):
    forge = FakeForge(pool)
    pair = [{"name": "H3.deep.t1", "value": True},
            {"name": "H12.tool.deep.t1", "value": {"candidate_id": CID}}]
    imp, comp = _run(
        pool, forge,
        mturn(ftc(1)),
        mturn(ftc(2, name="t2")),
        mturn(tc("read_pack", 3, dim="H12"), tc("read_pack", 4, dim="H2")),
        mturn(tc("propose", 5, kind="general", ops=[{"name": "H3.deep.zz_unknown", "value": True},
                                                     {"name": "H12.tool.deep.zz_unknown",
                                                      "value": {"candidate_id": CID}}],
                 rationale="试试", citations=["signal:sig1"])),
        mturn(tc("propose", 6, kind="general",
                 ops=[{"name": "H3.deep.t1", "value": True},
                      {"name": "H12.tool.deep.t1", "value": {"candidate_id": "fc_other"}}],
                 rationale="试试", citations=["signal:sig1"])),
        mturn(tc("propose", 7, kind="general", ops=pair, rationale="逐项检查等价",
                 citations=["signal:sig1"])),
        mturn(tc("compose", 8, decision="merge", ops=pair, spec_ops=[],
                 citations=["signal:sig1"], rationale="用新工具检查等价")))
    built, again = imp.results[1][0], imp.results[2][0]
    assert f"候选 {CID}" in built and "造好了" in built and "测试通过 3/3" in built
    assert "例子通过 1/1" in built and "$0.030" in built
    assert ('H3.deep.t1=true 和 H12.tool.deep.t1={"candidate_id": "fc_t1"}') in built
    assert FORGE_ONCE in again and len(forge.proposed) == 1
    kind, req, evidence, user_id, fdeps = forge.proposed[0]
    assert (kind, req.agent, req.scope, req.name, user_id) == ("tool", "deep", "user", "t1", U)
    assert evidence.citations == ("signal:sig1",) and "讲得太浅" in evidence.note
    assert fdeps.pack is EMPTY                      # the base pack lowers the coder's cap
    _, account, _ = forge.bound[0]
    assert account == deps(pool, Improver()).account     # no binding: the improver's own
    h12, h2 = imp.results[3]
    assert "- H12.tool.deep.t1｜" in h12
    assert "- H2.deep.t1｜自己造的工具的说明｜" in h2 and SCHEMA["description"] in h2
    assert "- H2.deep.t1.a｜自己造的工具的参数说明｜" in h2 and "第一个式子" in h2
    unknown, other, ok = imp.results[4][0], imp.results[5][0], imp.results[6][0]
    assert f"{FORGE_UNKNOWN}：zz_unknown" in unknown
    assert "只能写这一轮造好的工具候选" in other
    assert ok.startswith("已记录")
    assert comp.decision == "merge" and comp.chosen is not None
    assert {o.name for o in comp.chosen.ops} == {"H3.deep.t1", "H12.tool.deep.t1"}
    assert comp.cost >= 0.03                      # the build's cost is the improver's too


def test_a_failed_build_is_not_proposable(pool):
    forge = FakeForge(pool, ok=False)
    imp, _ = _run(
        pool, forge,
        mturn(ftc(1)),
        mturn(tc("propose", 2, kind="general",
                 ops=[{"name": "H3.deep.t1", "value": True},
                      {"name": "H12.tool.deep.t1", "value": {"candidate_id": CID}}],
                 rationale="试试", citations=["signal:sig1"])),
        NONE)
    assert "没造成：例子没通过" in imp.results[1][0] and "要用它" not in imp.results[1][0]
    assert f"{FORGE_UNKNOWN}：t1" in imp.results[2][0]          # a failed build is no tool


def test_forge_tool_refusals_are_tool_errors(pool):
    forge = FakeForge(pool, error=ForgeDisabled("off"))
    imp, comp = _run(
        pool, forge,
        mturn(ftc(1, name="read_k")),           # a built-in
        mturn(ftc(2, citations=["signal:nope"])),
        mturn(ftc(3)),                                 # forge off
        NONE)
    assert "已有的工具" in imp.results[1][0]
    assert "不存在" in imp.results[2][0]
    assert "现在不能造工具" in imp.results[3][0]
    assert comp.decision == "none"


def test_a_registry_tool_is_known_and_can_be_switched_off(pool):
    _user(pool)
    with pool.connection() as conn:
        insert_candidate(conn, "fc_old", name="old_tool")
        registry.promote(conn, "fc_old", gate_ref="hg_x", by="user", why="好",
                         gate_ok=lambda ref: True, now=NOW)
    imp, comp = _run(
        pool, FakeForge(pool),
        # the H3 listing is longer than one read (CLIP): its second page too
        mturn(tc("read_pack", 1, dim="H3"), tc("read_pack", 2, dim="H3", offset=8000),
              tc("read_pack", 3, dim="H2")),
        mturn(tc("compose", 4, decision="merge", ops=[{"name": "H3.deep.old_tool", "value": False}],
                 spec_ops=[], citations=["signal:sig1"], rationale="这个工具没用")))
    h3a, h3b, h2 = imp.results[1]
    h3 = h3a + h3b
    assert "- H3.deep.old_tool｜" in h3
    assert "- H2.deep.old_tool｜自己造的工具的说明｜" in h2 and SCHEMA["description"] in h2
    assert comp.decision == "merge"


# ---- promote / undo on the forge registry -------------------------------------------------------

def _gate(conn, verdict="pass", subject=f"tool:{CID}", user=U):
    rid = harness_rsi.start_round(conn, user, finding=None, now=NOW)
    return rid, harness_rsi.record_gate(conn, user, rid, subject, verdict, [], {}, now=NOW)


PAIR = PackDiff((KeyOp("H3.deep.t1", True), KeyOp("H12.tool.deep.t1", {"candidate_id": CID})))


def test_promote_for_promotes_with_a_passing_gate_and_records_the_version(pool):
    make_user(pool, U)
    with pool.connection() as conn:
        insert_candidate(conn)
        assert promote_for(conn, U, "r0", PackDiff((KeyOp("H1.write", "x"),)), "", why="w",
                           now=NOW) == PackDiff((KeyOp("H1.write", "x"),))     # no tool: as is
        rid, ref = _gate(conn)
        out = promote_for(conn, U, rid, PAIR, ref, why="更准", now=NOW)
        assert out.ops[0] == PAIR.ops[0]
        assert out.ops[1].value == {"candidate_id": CID, "registry_version": 1}
        assert fstore.get_candidate(conn, CID).status == "promoted"
        change = fstore.registry_history(conn, U)[0].change
        assert change["by"] == f"harness:{rid}" and change["gate_ref"] == ref
        assert harness_promotes(conn, U) == frozenset({f"forge:{U}:1"})
        forging.undo_tool(conn, U, out.ops[1].value)
        assert fstore.get_candidate(conn, CID).status == "rolled_back"
        assert fstore.registry_head(conn, U)[1] == {}
        forging.undo_tool(conn, U, out.ops[1].value)          # already undone: nothing to do
        with pytest.raises(ValueError, match="找不到"):
            forging.undo_tool(conn, U, {"candidate_id": CID, "registry_version": 9})
        with pytest.raises(ValueError, match="找不到"):
            forging.undo_tool(conn, U, {"candidate_id": "fc_zz", "registry_version": 1})


@pytest.mark.parametrize("verdict,subject,owner,name,agent", [
    ("fail", f"tool:{CID}", U, "t1", "deep"),          # the gate did not pass
    ("pass", "diff", U, "t1", "deep"),                 # a gate on something else
    ("pass", "tool:fc_zz", U, "t1", "deep"),
    ("pass", f"tool:{CID}", "u2", "t1", "deep"),       # another user's candidate
    ("pass", f"tool:{CID}", U, "t9", "deep"),          # the op names another tool
    ("pass", f"tool:{CID}", U, "t1", "assistant"),     # a tool built for another agent (M4)
])
def test_promote_for_refuses(pool, verdict, subject, owner, name, agent):
    make_user(pool, U)
    make_user(pool, "u2")
    with pool.connection() as conn:
        insert_candidate(conn, user=owner, name=name, agent=agent)
        _, ref = _gate(conn, verdict, subject)
        with pytest.raises(PromoteRefused):
            promote_for(conn, U, "r1", PAIR, ref, why="w", now=NOW)
        assert fstore.get_candidate(conn, CID).status == "built"
        assert fstore.registry_head(conn, U)[0] == 0


# ---- diff.refuse: forged pairing fails closed (Task 3 review) ----------------------------------

D = {"H4.deep.max_tool_calls": 60}


def _r(*ops, pack=EMPTY):
    return refuse(PackDiff(tuple(ops)), pack, {}, defaults=D)


def test_an_opt_in_with_a_none_or_empty_h12_is_refused():
    assert "要和它的 H12 候选一起改" in _r(KeyOp("H3.deep.t1", True), KeyOp("H12.tool.deep.t1", None))
    assert _r(KeyOp("H3.deep.t1", True), KeyOp("H12.tool.deep.t1", {})) is not None
    assert _r(KeyOp("H3.deep.t1", True),
              KeyOp("H12.tool.deep.t1", {"candidate_id": "fc_a"})) is None


def test_dropping_the_h12_record_while_the_opt_in_stays_on_is_refused():
    on = HarnessPack(3, {"H3.deep.t1": True,
                         "H12.tool.deep.t1": {"candidate_id": "fc_a", "registry_version": 1}})
    assert "同时关掉 H3.deep.t1" in _r(KeyOp("H12.tool.deep.t1", None), pack=on)
    assert _r(KeyOp("H12.tool.deep.t1", None), KeyOp("H3.deep.t1", False), pack=on) is None
    assert _r(KeyOp("H12.tool.deep.t1", None), KeyOp("H3.deep.t1", None), pack=on) is None
    assert _r(KeyOp("H12.tool.deep.t1", None)) is None              # the opt-in is not on


# ---- forge anchors mirrored in keys (Task 1 review) --------------------------------------------

def test_the_forge_caps_keys_mirror_are_the_forges():
    assert keys.FORGE_CAP == limits.CODER_MAX_TOOL_CALLS
    assert keys.FORGE_PATCH_CAP == limits.L2_MAX_TOOL_CALLS


def test_the_tool_coder_cap_is_lowered_by_the_pack():
    from evowiki.forge.coder import LIMITS, limits_for
    assert limits_for(None) is LIMITS
    assert limits_for(EMPTY).max_tool_calls == limits.CODER_MAX_TOOL_CALLS
    assert limits_for(HarnessPack(1, {"H4.forge.max_tool_calls": 7})).max_tool_calls == 7
    assert limits_for(HarnessPack(1, {"H4.forge.patch.max_tool_calls": 7})).max_tool_calls == \
        limits.CODER_MAX_TOOL_CALLS


# ---- the replay overlay (Task 6 target) --------------------------------------------------------

@dataclass(frozen=True)
class _Deep:
    seen: list
    repo: object = None

    def replay_section(self, user_id, **kw):
        self.seen.append(kw)
        return SimpleNamespace(body="正文", cue=None, writer_provider="anthropic", run_id="r",
                               cost=0.0)


def test_replay_passes_the_forged_candidate_to_the_writer():
    from tests.harness.test_round import case
    seen: list = []
    from evowiki.teach.contracts import TeachingSpec
    replay(case(1), PAIR, base_pack=EMPTY, base_spec=TeachingSpec(version=1, items=()),
           deps=ReplayDeps(deep=_Deep(seen), repo=None))  # type: ignore[arg-type]
    assert seen[0].get("forge_candidate") == CID


def test_forge_binding_is_the_rounds(pool):
    """`ImproverDeps.forge_binding` wins over the improver's own gateway / account."""
    _user(pool)
    forge = FakeForge(pool)
    imp = Improver()
    d = deps(pool, imp)
    acc = Account(user_id=U, group="rsi", tier_pin="good")
    run = ImproverRun(U, CASES, replace(d, forge=forge.hooks(pool),
                                        forge_binding=lambda: (d.gateway, acc)))
    run.forge_tool(FORGE_ARGS)
    assert forge.bound[0][1] == acc
