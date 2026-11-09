"""HarnessPatchGate (harness 3b, Task 9): forge child replays (faked `child`), in-process baselines
(faked `replay`), the round's judge (scripted) and the unmodified gate; the decision is stored as a
`harness_gate` row and a pass promotes through the real `run_patch` to awaiting_approval. Fakes
only: no sandbox, no model calls."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from evowiki.deep.agent import SnapshotSeqs
from evowiki.forge import switch
from evowiki.forge.models import BuildReport, Evidence, ForgeResult, PatchRequest, SandboxStats
from evowiki.forge.patch import replay as forge_replay
from evowiki.forge.patch.replay import ReplayResult as ChildResult
from evowiki.forge.patch.round import run_patch
from evowiki.harness import patch_gate as pg
from evowiki.harness import round as round_mod
from evowiki.harness.evalset_redlines import Regression
from evowiki.harness.judge import Comparison, JudgeUnavailable
from evowiki.harness.pack import HarnessPack
from evowiki.harness.patch_gate import HarnessPatchGate, deep_route_pack, forge_child
from evowiki.harness.replay import Case, ReplayResult
from evowiki.harness.replay_cases import CaseNotEncodable
from evowiki.store import forge as fstore
from evowiki.store import forge_patch as ps
from evowiki.store import harness_rsi
from evowiki.teach.contracts import TeachingSpec
from tests.forge.patch.test_land import seed

T = datetime(2026, 10, 23, 9, tzinfo=UTC)
CID = "fc_p1"
STEP = {"at": "deep.before_write", "kind": "delegate", "tier": "fast", "tools": [],
        "instruction": "先读一下这一页的格子", "output_to": "context.notes", "max_tool_calls": 0}
CHECK = {**STEP, "at": "deep.after_write", "kind": "self_check", "tier": "good",
         "output_to": "context.must_fix"}
ASK = {**STEP, "at": "assistant.before_answer"}
PACK = HarnessPack(3, {"H1.write": "先讲直觉。", "H10.deep.write.thinking": True,
                       "H9.deep.before_write": [STEP], "H11.deep.after_write": [CHECK],
                       "H9.assistant.before_answer": [ASK]})


def case(i: int, anchor: bool = False) -> Case:
    return Case(case_id=f"c{i}", run_id=f"r{i}", page_id="p", section_id=f"s{i}",
                signal_ids=("sig",), anchor=anchor, snapshot=SnapshotSeqs(1, 1, 1),
                mode="rewrite", instruction="", user_id="u1")


CASES = [case(1), case(2), case(3), case(9, anchor=True)]


@pytest.fixture
def round_id(pool):
    with pool.connection() as conn:
        seed(conn, CID)
        switch.set_enabled(conn, None, "patch", True, T)
        return harness_rsi.start_round(conn, "u1", finding=None, now=T)


class World:
    """Scripted arms: `child_out[case_id]` the child's output (default: a text by deepseek);
    `base_writer` the baseline's writer; `winners[case_id]` the judge's verdict (default "a")."""

    def __init__(self) -> None:
        self.child_out: dict[str, dict] = {}
        self.child_result: ChildResult | None = None
        self.base_writer = "deepseek"
        self.winners: dict[str, str] = {}
        self.encoded: list[tuple[Case, HarnessPack]] = []
        self.child_calls: list[tuple[str, list[dict]]] = []
        self.base_packs: list[HarnessPack] = []
        self.judged: list[tuple[str, str, str]] = []
        self.not_encodable: set[str] = set()
        self.judge_missing: set[str] = set()

    def encode(self, c: Case, pack: HarnessPack) -> dict:
        if c.case_id in self.not_encodable:
            raise CaseNotEncodable("这一页对应了资料，回放进程读不到资料，不能回放")
        self.encoded.append((c, pack))
        return {"case_id": c.case_id, "pack": {"version": pack.version,
                                               "values": dict(pack.values)}}

    def child(self, cid: str, cases: list[dict]) -> ChildResult:
        self.child_calls.append((cid, cases))
        if self.child_result is not None:
            return self.child_result
        outs = tuple(self.child_out.get(e["case_id"], {
            "case_id": e["case_id"], "text": f"新的 {e['case_id']}", "writer_provider": "deepseek",
            "cost": None, "run_id": f"rp_{e['case_id']}"}) for e in cases)
        return ChildResult(ok=True, outputs=outs, calls=2 * len(cases), cost_usd=0.3,
                           stats=SandboxStats(wall_s=1.0, exit="ok"), reason="")

    def replay(self, c: Case, diff, *, base_pack, base_spec, deps) -> ReplayResult:
        assert not diff.ops and not diff.spec_ops
        self.base_packs.append(base_pack)
        return ReplayResult(case_id=c.case_id, text=f"旧的 {c.case_id}",
                            writer_provider=self.base_writer, run_id=f"rb_{c.case_id}", cost=0.1)

    def judge(self, c: Case, a: ReplayResult, b: ReplayResult) -> Comparison:
        if c.case_id in self.judge_missing:
            raise JudgeUnavailable("没有可用的评审模型，这一轮跳过")
        self.judged.append((c.case_id, a.run_id, b.run_id))
        w = self.winners.get(c.case_id, "tie" if c.anchor else "a")
        return Comparison(cmp_id=f"cmp:{c.case_id}", case_id=c.case_id, a_id=a.run_id,
                          b_id=b.run_id, winner=w, reason="", quote="", flipped=False,
                          call_ids=("j1", "j2"))

    def gate(self, pool, round_id: str, diff_lines: int = 10) -> HarnessPatchGate:
        return HarnessPatchGate(
            round_id=round_id, user_id="u1", cases=CASES, base_pack=PACK,
            base_spec=TeachingSpec(version=1, items=[]), replay_deps=SimpleNamespace(), child=self.child,
            encode=self.encode, judge=self.judge, judge_cost=lambda cmp: 0.01,
            diff_lines=lambda cid: diff_lines, pool=pool, now=T, replay=self.replay)


@pytest.fixture
def world() -> World:
    return World()


def _row(pool, ref):
    with pool.connection() as conn:
        return harness_rsi.get_gate(conn, "u1", ref)


def test_three_wins_and_an_anchor_tie_pass(pool, round_id, world):
    g = world.gate(pool, round_id, diff_lines=10)
    with pool.connection() as conn:
        ref = g.evaluate(conn, CID, "u1")
    assert ref is not None and ref == g.last.gate_ref
    d = g.last.decision
    assert (d.verdict, d.wins, d.losses, d.ties, d.anchor_losses) == ("pass", 3, 0, 0, 0)
    assert d.utility_delta == pytest.approx(3 - 0.05 * 10)
    assert [c.case_id for c in g.last.comparisons] == ["c1", "c2", "c3", "c9"]
    # orientation: a = the patched (child) text, b = the baseline
    assert world.judged[0] == ("c1", "rp_c1", "rb_c1")
    assert g.last.cost == pytest.approx(0.3 + 4 * 0.1 + 4 * 0.01)
    row = _row(pool, ref)
    assert (row.subject, row.verdict, row.round_id) == (f"patch:{CID}", "pass", round_id)
    assert row.detail["complexity"] == 10 and len(row.detail["comparisons"]) == 4
    assert g.ok(ref) is True
    assert len(world.child_calls) == 1 and world.child_calls[0][0] == CID


def test_complexity_is_the_patch_diff_lines(pool, round_id, world):
    g = world.gate(pool, round_id, diff_lines=200)
    with pool.connection() as conn:
        assert g.evaluate(conn, CID, "u1") is None
    d = g.last.decision
    assert d.verdict == "fail" and d.utility_delta == pytest.approx(3 - 0.05 * 200)
    assert any(r.startswith("净收益") and r.endswith("不大于 0") for r in d.reasons)
    assert g.ok(g.last.gate_ref) is False
    assert _row(pool, g.last.gate_ref).verdict == "fail"


def test_a_child_error_makes_its_case_unusable_and_too_few_is_thin(pool, round_id, world):
    world.child_out["c2"] = {"case_id": "c2", "error": "LookupError: 快照里没有这个页面：p"}
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        assert g.evaluate(conn, CID, "u1") is None
    assert g.last.decision.verdict == "thin"
    # the child's exception class stays out of the reason the user may see
    assert ("c2", round_mod.REPLAY_FAILED.format(e="快照里没有这个页面：p")) in g.last.unusable
    assert [c for c, *_ in world.judged] == ["c1", "c3", "c9"]          # no baseline, no judge
    assert _row(pool, g.last.gate_ref).verdict == "thin"


def test_bad_or_foreign_child_outputs_are_unusable(pool, round_id, world):
    world.child_out["c1"] = {"case_id": "c1", "text": "", "writer_provider": "deepseek",
                             "run_id": "x"}
    world.child_out["c2"] = {"case_id": "cX", "text": "t", "writer_provider": "deepseek",
                             "run_id": "x"}
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        g.evaluate(conn, CID, "u1")
    assert ("c1", pg.BAD_OUTPUT) in g.last.unusable and ("c2", pg.BAD_OUTPUT) in g.last.unusable


def test_sides_written_by_different_providers_are_unusable(pool, round_id, world):
    """The judge is picked for the child's writer (`judge_for` in the round's compare); a
    baseline written by another provider cannot share that judge."""
    world.base_writer = "anthropic"
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        assert g.evaluate(conn, CID, "u1") is None
    assert world.judged == []
    assert {r for _, r in g.last.unusable} == {round_mod.MIXED}
    assert g.last.decision.verdict == "thin"


def test_no_judge_for_a_writer_makes_the_case_unusable(pool, round_id, world):
    world.judge_missing = {"c1"}
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        g.evaluate(conn, CID, "u1")
    assert ("c1", "没有可用的评审模型，这一轮跳过") in g.last.unusable


def test_a_case_that_cannot_be_encoded_is_unusable(pool, round_id, world):
    world.not_encodable = {"c3"}
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        g.evaluate(conn, CID, "u1")
    assert [e["case_id"] for e in world.child_calls[0][1]] == ["c1", "c2", "c9"]
    assert g.last.unusable[0][0] == "c3" and "资料" in g.last.unusable[0][1]
    assert g.last.decision.verdict == "thin"


def test_a_refused_child_run_fails_with_the_forge_reason(pool, round_id, world):
    world.child_result = ChildResult(ok=False, outputs=(), calls=0, cost_usd=None,
                                     stats=SandboxStats(wall_s=0.0, exit="error"),
                                     reason="候选分支不存在：forge/fc_p1")
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        assert g.evaluate(conn, CID, "u1") is None
    assert g.last.decision.verdict == "fail" and g.last.comparisons == ()
    assert g.last.decision.reasons == ("候选分支不存在：forge/fc_p1",)
    assert world.base_packs == [] and world.judged == []
    row = _row(pool, g.last.gate_ref)
    assert row.verdict == "fail" and row.reasons == ["候选分支不存在：forge/fc_p1"]


def test_an_unexpected_error_is_a_recorded_fail(pool, round_id, world):
    def boom(c, a, b):
        raise RuntimeError("judge reply did not parse")
    g = world.gate(pool, round_id)
    g.judge = boom
    with pool.connection() as conn:
        assert g.evaluate(conn, CID, "u1") is None
    assert g.last.decision.verdict == "fail"
    assert g.last.decision.reasons == (pg.ERROR,)
    assert "Error" not in pg.ERROR and g.last.error == "RuntimeError"
    row = _row(pool, g.last.gate_ref)
    assert row.verdict == "fail" and row.detail["error"] == "RuntimeError"
    # the spend before the error is kept: the child (0.3) and the first baseline (0.1)
    assert g.last.cost == pytest.approx(0.4) and row.detail["cost"] == pytest.approx(0.4)


def test_a_lookup_error_while_encoding_makes_the_case_unusable(pool, round_id, world):
    enc = world.encode

    def encode(c, pack):
        if c.case_id == "c2":
            raise LookupError("快照里没有这个页面：p")
        return enc(c, pack)
    g = world.gate(pool, round_id)
    g.encode = encode
    with pool.connection() as conn:
        g.evaluate(conn, CID, "u1")
    assert ("c2", pg.REPLAY_FAILED.format(e="快照里没有这个页面：p")) in g.last.unusable
    assert [e["case_id"] for e in world.child_calls[0][1]] == ["c1", "c3", "c9"]
    assert g.last.error is None


def test_a_failed_baseline_replay_makes_the_case_unusable_and_its_spend_counts(pool, round_id,
                                                                                world):
    from evowiki.wikiapp.refine_service import ReplayFailed
    base = world.replay

    def replay(c, diff, **kw):
        if c.case_id == "c1":
            raise ReplayFailed("no_draft", "没有草稿", cost=0.05)
        return base(c, diff, **kw)
    g = world.gate(pool, round_id)
    g.replay = replay
    with pool.connection() as conn:
        g.evaluate(conn, CID, "u1")
    assert ("c1", pg.REPLAY_FAILED.format(e="no_draft: 没有草稿")) in g.last.unusable
    assert [c for c, *_ in world.judged] == ["c2", "c3", "c9"]
    assert g.last.decision.verdict == "thin"
    assert g.last.cost == pytest.approx(0.3 + 0.05 + 3 * 0.1 + 3 * 0.01)


def test_no_encodable_case_is_thin_without_running_the_child(pool, round_id, world):
    world.not_encodable = {"c1", "c2", "c3", "c9"}
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        assert g.evaluate(conn, CID, "u1") is None
    assert world.child_calls == [] and world.base_packs == []
    assert g.last.decision.verdict == "thin" and g.last.cost == 0.0 and g.last.error is None
    row = _row(pool, g.last.gate_ref)
    assert row.verdict == "thin" and row.detail["note"] == pg.NO_CASES


def test_a_child_output_count_mismatch_is_an_evaluation_error(pool, round_id, world):
    world.child_result = ChildResult(
        ok=True, outputs=({"case_id": "c1", "text": "t", "writer_provider": "deepseek",
                           "run_id": "x"},), calls=2, cost_usd=0.2,
        stats=SandboxStats(wall_s=1.0, exit="ok"), reason="")
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        assert g.evaluate(conn, CID, "u1") is None
    assert g.last.decision.verdict == "fail" and g.last.decision.reasons == (pg.OUTPUT_COUNT,)
    assert g.last.error == "output_count" and g.last.cost == pytest.approx(0.2)
    assert world.base_packs == [] and world.judged == []


def test_a_refused_child_reason_has_no_class_name(pool, round_id, world):
    world.child_result = ChildResult(ok=False, outputs=(), calls=0, cost_usd=None,
                                     stats=SandboxStats(wall_s=0.0, exit="error"),
                                     reason="无法建立回放工作区：OSError")
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        g.evaluate(conn, CID, "u1")
    assert g.last.decision.reasons == ("无法建立回放工作区",)
    assert g.last.error == "child_refused"


def test_ok_only_for_a_pass_this_gate_issued_for_its_candidate(pool, round_id, world):
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        ref = g.evaluate(conn, CID, "u1")
        assert ref is not None and g.ok(ref)
        # a passing gate row this gate did not issue (another subject, or another gate's)
        other = harness_rsi.record_gate(conn, "u1", round_id, "patch:fc_other", "pass", [], {},
                                        now=T)
        same = harness_rsi.record_gate(conn, "u1", round_id, f"patch:{CID}", "pass", [], {},
                                       now=T)
    assert not g.ok(other) and not g.ok(same) and not g.ok("hg_missing")
    g2 = world.gate(pool, round_id)
    assert not g2.ok(ref)                    # issued by another gate object


def test_both_arms_run_the_pack_without_h10_and_hooks_on_the_deep_tier(pool, round_id, world):
    g = world.gate(pool, round_id)
    with pool.connection() as conn:
        g.evaluate(conn, CID, "u1")
    sent = [p for _, p in world.encoded]
    for pack in (*sent, *world.base_packs):
        assert not any(k.startswith("H10.") for k in pack.values)
        assert pack.values["H9.deep.before_write"][0]["tier"] == "deep"
        assert pack.values["H11.deep.after_write"][0]["tier"] == "deep"
        assert pack.values["H9.assistant.before_answer"] == [ASK]        # not a deep hook
        assert pack.values["H1.write"] == "先讲直觉。"
    assert sent[0] == world.base_packs[0]                    # the same pack on both sides
    for enc in world.child_calls[0][1]:
        assert not any(k.startswith("H10.") for k in enc["pack"]["values"])


def test_deep_route_pack_keeps_valid_hook_steps():
    from evowiki.harness.steps import parse_steps
    out = deep_route_pack(PACK)
    assert [s.tier for s in parse_steps("H9.deep.before_write",
                                        out.values["H9.deep.before_write"])] == ["deep"]
    assert PACK.values["H9.deep.before_write"][0]["tier"] == "fast"       # not mutated


def test_the_wording_is_the_rounds():
    assert pg.MIXED == round_mod.MIXED and pg.REPLAY_FAILED == round_mod.REPLAY_FAILED


# ---- through the real run_patch ------------------------------------------------------------

REQ = PatchRequest(title="修好加法", goal="修好加法", failing_case="add(1,2)==3")
EV = Evidence(citations=("trace:r1",), note="答案总被判错")


def _propose(kind, spec, evidence, *, user_id, patch_deps):
    return ForgeResult(candidate_id=CID, kind="patch", build=BuildReport(
        ok=True, tests_passed=1, tests_failed=0, examples_passed=0, examples_failed=0,
        lint_ok=True, schema_ok=True, sandbox=SandboxStats(wall_s=0.1, exit="ok"), reason="ok"))


def _state(pool):
    with pool.connection() as conn:
        return ps.get_patch(conn, CID).state, fstore.get_candidate(conn, CID).status


@pytest.fixture
def _no_forge_env(monkeypatch):
    monkeypatch.delenv("EVOWIKI_FORGE", raising=False)


def test_run_patch_promotes_a_pass_to_awaiting_approval(pool, round_id, world, _no_forge_env):
    g = world.gate(pool, round_id)
    run_patch(REQ, EV, user_id="u1", deps=SimpleNamespace(pool=pool), gate=g, now=T,
              propose_fn=_propose)
    assert g.last.decision.verdict == "pass"
    assert _state(pool) == ("awaiting_approval", "promoted")
    with pool.connection() as conn:
        assert ps.get_patch(conn, CID).gate_ref == g.last.gate_ref


def test_run_patch_leaves_a_failed_patch_built(pool, round_id, world, _no_forge_env):
    g = world.gate(pool, round_id, diff_lines=200)
    run_patch(REQ, EV, user_id="u1", deps=SimpleNamespace(pool=pool), gate=g, now=T,
              propose_fn=_propose)
    assert g.last.decision.verdict == "fail"
    assert _state(pool) == ("built", "built")
    assert _row(pool, g.last.gate_ref).verdict == "fail"       # the decision stays on record


# ---- forge_child -----------------------------------------------------------------------------

def test_forge_child_builds_one_accounted_converse_per_call(monkeypatch, tmp_path):
    from evowiki.forge.binding import accounted
    from evowiki.llm.usage import Account
    seen = []

    def fake_run_replay(cid, *, entry, cases, main_root, converse, runner, python, test_db_url,
                        tmp):
        seen.append((cid, entry, cases, converse))
        return "r"

    monkeypatch.setattr(forge_replay, "run_replay", fake_run_replay)
    gw = SimpleNamespace(salted=lambda salt: SimpleNamespace(salt=salt))
    child = forge_child(gateway=gw, account=Account(user_id="u1", group="rsi"),
                        main_root=tmp_path, runner=object(), python="py", test_db_url="pg://",
                        tmp=tmp_path)
    assert child(CID, [{"case_id": "c1"}]) == "r" and child(CID, []) == "r"
    assert [s[:2] for s in seen] == [(CID, "deep.refine"), (CID, "deep.refine")]
    assert all(accounted(s[3]) for s in seen) and seen[0][3] is not seen[1][3]


@pytest.mark.parametrize("text,out", [
    ("LookupError: 快照里没有这个页面：p", "快照里没有这个页面：p"),
    ("Error: 回放进程退出了", "回放进程退出了"),
    ("无法建立回放工作区：OSError", "无法建立回放工作区"),
    ("重启失败（RuntimeError）", "重启失败"),
    ("写好了（第二段）", "写好了（第二段）"),
    ("ValueError", pg.CHILD_FAILED),
])
def test_plain_drops_class_names_and_keeps_the_reason(text, out):
    assert pg.plain(text) == out


@pytest.mark.parametrize(("regression", "verdict"), [
    (Regression(fixed=("a", "b"), gained=6, p=0.016), "pass"),
    (Regression(broken=("c",)), "fail"),
    (Regression(error="没跑完"), "thin"),
    (Regression(), "thin"),
])
def test_an_assistant_patch_rests_on_real_turns_on_its_own_code(pool, round_id, world, regression, verdict):
    world.not_encodable = {"c1", "c2", "c3", "c9"}          # no page case to replay
    g = replace(world.gate(pool, round_id), evalset=lambda cid: regression)
    with pool.connection() as conn:
        ref = g.evaluate(conn, CID, "u1")
    assert g.last.decision.verdict == verdict and (ref is not None) == (verdict == "pass")
    assert _row(pool, g.last.gate_ref).detail["task"]["outcome"]
