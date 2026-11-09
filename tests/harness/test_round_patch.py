"""Code patches from the improver (harness 3b, Task 10): `propose_patch` is offered only with patch
hooks and the forge's patch switch on; after the round's key path the request is built (the forge's
`propose`, faked here) and evaluated by a `HarnessPatchGate` (its child faked, the baselines and
the judge the round's own fakes), and a pass waits in the forge's review flow
(`awaiting_approval`). Real Postgres with real forge_candidate / forge_patch rows; nothing reaches
a real provider or a sandbox."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from evowiki.forge import switch
from evowiki.forge.models import ForgeResult, PatchBuildReport, PatchRequest, SandboxStats
from evowiki.forge.patch.replay import ReplayResult as ChildResult
from evowiki.harness import round as round_mod
from evowiki.harness.improver import run_improver
from evowiki.harness.improver_prompt import SYSTEM
from evowiki.harness.patching import PatchHooks, PatchProposal
from evowiki.harness.round import HarnessRounds
from evowiki.store import forge as fstore
from evowiki.store import forge_patch as ps
from evowiki.store import harness_rsi
from tests.forge.patch.test_land import seed
from tests.harness.test_improver import CASES, Improver, mturn, tc
from tests.harness.test_improver import deps as improver_deps
from tests.harness.test_round import NOW, Env, FakeImprover, U, caseset, prefer
from tests.runtime.helpers import make_user

CID = "fc_hp1"
REQ = PatchRequest(goal="写页面时漏读前置概念", title="补读前置概念", failing_case="read_k 没被调用")
NONE = mturn(tc("compose", 9, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"],
                rationale="只是看看"))


@pytest.fixture(autouse=True)
def _no_forge_env(monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)


def _switch(pool, on: bool) -> None:
    with pool.connection() as conn:
        switch.set_enabled(conn, None, "patch", on, NOW)


class Forge:
    """The forge side: `propose` inserts real candidate / patch rows (built), `child` answers
    every case with a text the round's judge prefers (`text`), and every hook call is kept."""

    def __init__(self, pool, *, build_ok=True, text="候选写法的正文", child=None,
                 writer="anthropic"):
        self.pool, self.build_ok, self.text, self.writer = pool, build_ok, text, writer
        self.proposed: list = []
        self.child_calls: list = []
        self.child_result = child
        self.gateways: list = []
        self.deps_seen: list = []

    def propose(self, kind, request, evidence, *, user_id, patch_deps):
        self.proposed.append((kind, request, evidence, user_id, patch_deps))
        with self.pool.connection() as conn:
            if self.build_ok:
                seed(conn, CID, title=request.title)
        return ForgeResult(candidate_id=CID, kind="patch", build=PatchBuildReport(
            ok=self.build_ok, tests_passed=3, tests_failed=0, examples_passed=0,
            examples_failed=0, lint_ok=True, schema_ok=True,
            sandbox=SandboxStats(wall_s=1.0, exit="ok"), cost_usd=0.2, diff_lines=12,
            commit="ab" * 20, reason="通过" if self.build_ok else "新测试没有通过"))

    def child(self, cid, cases):
        self.child_calls.append((cid, cases))
        if self.child_result is not None:
            return self.child_result
        outs = tuple({"case_id": e["case_id"], "text": f"{self.text}：{e['case_id']}",
                      "writer_provider": self.writer, "run_id": f"rp_{e['case_id']}"}
                     for e in cases)
        return ChildResult(ok=True, outputs=outs, calls=len(cases), cost_usd=0.0,
                           stats=SandboxStats(wall_s=1.0, exit="ok"), reason="")

    def hooks(self) -> PatchHooks:
        def deps_for(gw, account, pack):
            self.deps_seen.append((gw, account, pack))
            return SimpleNamespace(pool=self.pool)

        def child_for(gw, account):
            self.gateways.append((gw, account))
            return self.child

        def enabled(conn, user_id):
            return switch.enabled(conn, user_id, "patch")

        return PatchHooks(deps_for=deps_for, enabled=enabled, child_for=child_for,
                          main_root=Path("."), propose=self.propose,
                          encode=lambda c, pack: {"case_id": c.case_id})


class PatchImprover(FakeImprover):
    """FakeImprover that also asks for a code patch."""

    def __call__(self, user_id, cases, *, deps):
        comp = super().__call__(user_id, cases, deps=deps)
        return replace(comp, patch=PatchProposal(REQ, ("signal:sig1",)))


def env_with(pool, forge: Forge, *, diff=None, decision="none", **kw) -> Env:
    env = Env(pool, improver=PatchImprover(diff, decision=decision), **kw)
    env.deps = replace(env.deps, patch=forge.hooks())
    return env


def _round(env):
    (row,) = [r for r in env.rounds()]
    return row


def _patch_row(pool):
    with pool.connection() as conn:
        return ps.get_patch(conn, CID), fstore.get_candidate(conn, CID)


# ---- the improver's tool ---------------------------------------------------------------------

def _user(pool):
    from evowiki.store.users import get_user
    with pool.connection() as conn:
        known = get_user(conn, "u1") is not None
    if not known:
        make_user(pool, "u1")


def _tools(pool, hooks):
    _user(pool)
    imp = Improver(NONE)
    run_improver("u1", CASES, deps=replace(improver_deps(pool, imp), patch=hooks))
    return [t.name for t in imp.requests[0].tools]


def test_propose_patch_is_not_offered_by_default(pool):
    forge = Forge(pool)
    assert "propose_patch" not in _tools(pool, forge.hooks())     # switch off (default)
    assert "propose_patch" not in _tools(pool, None)              # no hooks
    _switch(pool, True)
    assert "propose_patch" not in _tools(pool, None)


def test_propose_patch_records_one_request_with_checked_citations(pool):
    _switch(pool, True)
    _user(pool)
    args = {"goal": REQ.goal, "title": REQ.title, "failing_case": REQ.failing_case,
            "paths_hint": ["src/evowiki/deep/agent.py"]}
    imp = Improver(
        mturn(tc("propose_patch", 1, **args, citations=["signal:nope"])),
        mturn(tc("propose_patch", 2, **{**args, "title": "x"}, citations=["signal:sig1"])),
        mturn(tc("propose_patch", 3, **args, citations=["signal:sig1"])),
        mturn(tc("propose_patch", 4, **args, citations=["signal:sig1"])),
        NONE)
    comp = run_improver("u1", CASES, deps=replace(improver_deps(pool, imp),
                                                  patch=Forge(pool).hooks()))
    bad_cite, bad_title, ok, again = (r[0] for r in imp.results[1:5])
    assert "不存在" in bad_cite and "参数不合格" in bad_title
    assert ok.startswith("已记下") and "这一轮结束后" in ok
    assert "一轮只能提一个" in again
    assert comp.decision == "none" and comp.patch is not None
    assert comp.patch.request.title == REQ.title
    assert comp.patch.request.goal == args["goal"]
    assert comp.patch.request.failing_case == args["failing_case"]
    assert comp.patch.request.paths_hint == ("src/evowiki/deep/agent.py",)
    assert comp.patch.citations == ("signal:sig1",)
    tool = next(t for t in imp.requests[0].tools if t.name == "propose_patch")
    assert "根因" in tool.description and "相关文件" in tool.description
    for word in ("claude", "deepseek", "sonnet", "opus"):
        assert word not in tool.description.lower()


def test_the_prompt_says_when_to_ask_for_code_changes():
    assert "propose_patch" in SYSTEM and "缺功能或代码有缺陷" in SYSTEM


# ---- the round's step 6b -----------------------------------------------------------------------

def test_a_patch_request_with_the_switch_off_is_not_built(pool):
    forge = Forge(pool)
    env = env_with(pool, forge)
    r = env.run()
    assert r.status == "no_change" and forge.proposed == []
    assert _round(env).decision is not None
    row = _round(env)
    patch = _patch_rec(pool, row.round_id)
    assert patch["status"] == "off" and patch["reason"] == round_mod.PATCH_OFF


def _patch_rec(pool, round_id):
    with pool.connection() as conn:
        return harness_rsi.get_round(conn, U, round_id).decision["patch"]


def test_patch_built_evaluated_and_waiting_for_review(pool):
    _switch(pool, True)
    forge = Forge(pool)
    env = env_with(pool, forge)
    r = env.run()
    assert r.status == "no_change" and r.change_id is None        # the key path's own status
    (kind, req, ev, uid, _deps), = forge.proposed
    assert (kind, req, uid) == ("patch", REQ, U) and ev.citations == ("signal:sig1",)
    # one child run on every problem and anchor case
    (cid, sent), = forge.child_calls
    assert cid == CID and [e["case_id"] for e in sent] == [
        c.case_id for c in (*env.cases.problem, *env.cases.anchors)]
    row, cand = _patch_row(pool)
    assert row.state == "awaiting_approval" and cand.status == "promoted"
    with pool.connection() as conn:
        gate = harness_rsi.get_gate(conn, U, row.gate_ref)
    assert gate.subject == f"patch:{CID}" and gate.verdict == "pass"
    assert gate.round_id == r.round_id and gate.detail["complexity"] == 12
    rec = _patch_rec(pool, r.round_id)
    assert rec["status"] == "asked" and rec["candidate_id"] == CID and rec["build_ok"]
    assert rec["verdict"] == "pass" and rec["gate_ref"] == row.gate_ref
    assert rec["state"] == "awaiting_approval"
    # no harness change, no ledger proposal: the forge's review flow owns it
    assert env.changes() == []
    # the patch's comparisons are the round's, marked
    stored = _round(env).comparisons
    assert len(stored) == 4 and all(c["patch"] and c["final"] is False for c in stored)
    assert env.lessons() == []


def test_the_coder_and_the_child_run_on_the_rounds_recording_gateway(pool):
    _switch(pool, True)
    forge = Forge(pool)
    env = env_with(pool, forge)
    env.run()
    ((coder_gw, account, pack),) = forge.deps_seen
    ((child_gw, child_account),) = forge.gateways
    for gw in (coder_gw, child_gw):
        assert isinstance(gw._log, round_mod._RecordingLog)
    assert coder_gw._log is child_gw._log
    assert account.group == "rsi" and account.user_id == U and child_account == account
    assert pack == env.pack()


def test_a_patch_after_an_applied_key_change(pool):
    _switch(pool, True)
    forge = Forge(pool)
    env = env_with(pool, forge, diff=round_mod.PackDiff(
        ops=(round_mod.KeyOp("H4.deep.max_tool_calls", 100),)), decision="a")
    r = env.run()
    assert r.status == "applied" and r.change_id is not None
    assert _patch_rec(pool, r.round_id)["status"] == "asked"
    assert _patch_row(pool)[0].state == "awaiting_approval"


def test_a_thin_key_path_runs_no_patch(pool):
    _switch(pool, True)
    forge = Forge(pool)
    env = env_with(pool, forge, cases=caseset(n=2))
    r = env.run()
    assert r.status == "thin" and forge.proposed == []


def test_a_failed_patch_with_a_loss_stays_built_and_writes_a_lesson(pool):
    _switch(pool, True)
    forge = Forge(pool, text="写法的正文")
    env = env_with(pool, forge)
    env.judge = prefer("现行")              # the baseline wins every case
    r = env.run()
    assert r.status == "no_change"
    rec = _patch_rec(pool, r.round_id)
    assert rec["status"] == "failed" and rec["verdict"] == "fail" and rec["losses"] == 3
    row, cand = _patch_row(pool)
    assert row.state == "built" and cand.status == "built"
    (lesson,) = env.lessons()
    assert lesson.verdict == "ineffective" and lesson.change["patch"]["candidate_id"] == CID
    assert lesson.evidence["gate_ref"] == rec["gate_ref"] and lesson.evidence["losses"] == 3


def test_a_build_failure_is_recorded_and_not_evaluated(pool):
    _switch(pool, True)
    forge = Forge(pool, build_ok=False)
    env = env_with(pool, forge)
    r = env.run()
    rec = _patch_rec(pool, r.round_id)
    assert rec["status"] == "build_failed" and rec["reason"] == "新测试没有通过"
    assert forge.child_calls == [] and env.lessons() == []


def test_an_evaluation_error_is_an_error_without_a_lesson(pool):
    _switch(pool, True)
    forge = Forge(pool, child=ChildResult(ok=False, outputs=(), calls=0, cost_usd=None,
                                          stats=SandboxStats(wall_s=0.0, exit="error"),
                                          reason="沙箱不可用：pywin32 missing"))
    env = env_with(pool, forge)
    r = env.run()
    assert r.status == "no_change"
    rec = _patch_rec(pool, r.round_id)
    assert rec["status"] == "error" and rec["verdict"] == "fail"
    assert rec["error"] == "child_refused" and env.lessons() == []
    assert _patch_row(pool)[0].state == "built"


def test_a_raising_forge_leaves_the_round_its_own_status(pool):
    _switch(pool, True)
    forge = Forge(pool)

    def boom(*a, **kw):
        raise RuntimeError("worktree add failed")
    forge.propose = boom
    env = env_with(pool, forge)
    r = env.run()
    assert r.status == "no_change"
    rec = _patch_rec(pool, r.round_id)
    assert rec["status"] == "error" and rec["reason"] == round_mod.PATCH_ERROR
    assert "Error" not in rec["reason"]


def test_observation_rows_of_patches_use_the_forge_prefix():
    from evowiki.harness.observe import ObserveResult
    row = round_mod.observation_row(ObserveResult(CID, "harmful", kind="patch"))
    assert row["change_id"] == f"forgep:{CID}"
    assert round_mod.observation_row(ObserveResult("abc", "helpful"))["change_id"] == "h_abc"


def test_harness_rounds_observe_sees_patches(pool):
    env = Env(pool)
    assert HarnessRounds(env.deps).observe(U, now=NOW + timedelta(days=1)) == []


def test_a_pass_the_forge_does_not_promote_says_so_in_plain_chinese(pool):
    _switch(pool, True)
    forge = Forge(pool)
    child = forge.child

    def child_then_switch_off(cid, cases):
        _switch(pool, False)                  # turned off while the gate was running
        return child(cid, cases)
    forge.child = child_then_switch_off
    env = env_with(pool, forge)
    r = env.run()
    assert r.status == "no_change"
    rec = _patch_rec(pool, r.round_id)
    assert rec["verdict"] == "pass" and rec["status"] == "error"
    assert rec["reason"] == f"{round_mod.PATCH_NOT_PROMOTED}：代码修改的开关已经关了"
    assert rec["error"] == "PromoteRefused" and rec["state"] == "built"
    assert env.lessons() == []
