"""One harness round end to end (harness RSI phase 2, Task 9), on a real `pool`: every model is
scripted. Most tests swap the case selection, the improver and the replay for fakes (the judge, the
gate, the stores and the apply path are real); the last one replays for real (the scripted deep
writer of tests/harness/test_replay.py) and checks that the round's cost is the sum of the calls it
made. Nothing reaches a real provider."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.deep.agent import SnapshotSeqs
from evowiki.harness import round as round_mod
from evowiki.harness.anchors import SMOKE_CASES
from evowiki.harness.cases import CaseSet
from evowiki.harness.change_source import undo_change
from evowiki.harness.diff import KeyOp, PackDiff, diff_from_json
from evowiki.harness.improver import Candidate, Composition
from evowiki.harness.judge import JudgeConfigs
from evowiki.harness.replay import Case, ReplayResult
from evowiki.harness.round import (
    MONTH_CAP,
    NO_DEEP,
    NOT_DUE,
    THIN_NOTICE,
    HarnessRounds,
    RoundDeps,
    ledger_key,
    run_round,
)
from evowiki.llm.chat import ChatMessage, ChatRequest
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.scripted import ScriptedChatProvider, ScriptedProvider
from evowiki.llm.usage_control import UsageControl
from evowiki.service.harness_defaults import defaults
from evowiki.store import harness as pack_store
from evowiki.store import harness_rsi
from evowiki.teach.approval import METHOD_KIND, RSI, PendingGone, PendingStale
from evowiki.teach.contracts import RsiSettings, Signal, SpecPatchOp, TeachingSpec
from evowiki.teach.spec_registry import MemoryRegistry
from evowiki.wikiapp.refine_service import DeepService, ReplayFailed
from tests.deep.fakes import DeepScript, call, finish, section, turn
from tests.harness.test_judge import DS, _shown
from tests.runtime.helpers import make_user
from tests.teach.fake_approvals import Effect, FakeApprovals

U = "u1"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
SNAP = SnapshotSeqs(wiki_seq=3, aspect_seq=1, infer_version=1)
SIGNALS = tuple(Signal(signal_id=f"sig{i}", kind="check_unclear" if i % 2 else "page_feedback",
                       at=NOW - timedelta(hours=1), page_id="p1", section_id="s2",
                       text="这里没看懂") for i in range(10))
H4 = PackDiff(ops=(KeyOp("H4.deep.max_tool_calls", 100),))
H1 = PackDiff(ops=(KeyOp("H1.write", "每个符号第一次出现时先用一句话说它是什么。"),))
ITEM = "先给一个能看到结果的小例子，再讲定义。"
REF = ModelRef(provider="scripted", model="m")
PRICES = PriceTable({"scripted:m": Price(1000.0, 2000.0)})


def case(i: int, *, anchor: bool = False, page: str = "p1") -> Case:
    kind = "anchor" if anchor else "case"
    return Case(case_id=f"{kind}:run-{i}:s2", run_id=f"run-{i}", page_id=page, section_id="s2",
                signal_ids=() if anchor else (f"sig{i}",), anchor=anchor, snapshot=SNAP,
                mode="rewrite", instruction="", user_id=U)


def caseset(n: int = 3, anchors: int = 1, finding: str = "讲得太浅：页一") -> CaseSet:
    return CaseSet(problem=tuple(case(i, page=f"p{i}") for i in range(1, n + 1)),
                   anchors=tuple(case(10 + i, anchor=True, page="px") for i in range(anchors)),
                   unusable=(("sig9", "反馈没有落在某一段上"),), finding=finding)


class FakeReplay:
    """Stands in for `harness.replay.replay`: the current pack's text says 现行, a candidate's
    says 候选; every text is about as long (the judge's length rule never fires)."""

    def __init__(self, *, provider=lambda case, diff: "anthropic", fail=(), broken=(),
                 cost=(0.01, 0.02)):
        """`fail`: cases no pack can write; `broken`: cases only the candidate cannot write."""
        self.calls: list[tuple[str, PackDiff]] = []
        self.provider, self.fail, self.broken, self.cost = provider, set(fail), set(broken), cost

    def __call__(self, case, candidate, *, base_pack, base_spec, deps):
        self.calls.append((case.case_id, candidate))
        n = len(self.calls)
        if case.case_id in self.fail or (case.case_id in self.broken
                                         and (candidate.ops or candidate.spec_ops)):
            raise ReplayFailed("no_draft", "没有草稿")
        mark = "候选" if candidate.ops or candidate.spec_ops else "现行"
        return ReplayResult(case_id=case.case_id,
                            text=f"回忆线索：{mark}\n\n{case.case_id} 第{n}版：{mark}写法的正文。",
                            writer_provider=self.provider(case, candidate),
                            run_id=f"harness-replay:hr{n}:x",
                            cost=self.cost[1] if mark == "候选" else self.cost[0])


def prefer(word: str):
    """A judge reply function: picks the text containing `word`, quoting its last line."""
    def respond(model, req):
        first, second = _shown(req.prompt)
        for side, text, other in (("甲", first, second), ("乙", second, first)):
            if word in text and word not in other:
                out = {"better": side, "quote": text.splitlines()[-1], "reason": "具体"}
                break
        else:
            out = {"better": "same", "quote": "", "reason": "差不多"}
        return json.dumps(out, ensure_ascii=False)
    return respond


class FakeImprover:
    """Stands in for `run_improver`: one priced improver turn through the round's gateway (twice:
    the second is served from the call log), then the given decision."""

    def __init__(self, diff: PackDiff | None, decision: str = "a", rationale: str = "工具次数不够用"):
        self.diff, self.decision, self.rationale = diff, decision, rationale
        self.seen: list = []
        self.cases_seen: list = []

    def __call__(self, user_id, cases, *, deps):
        self.seen.append(deps)
        self.cases_seen.append(cases)
        req = ChatRequest(purpose="rsi.h.improve", prompt_version="improver@v1",
                          subject=deps.round_id, system="s",
                          messages=(ChatMessage(role="user", text="开始"),))
        for _ in range(2):
            _, c = deps.gateway.converse("generator", req, account=deps.account)
        kind = "general" if self.decision == "a" else "specific"
        cands = () if self.diff is None else (Candidate(kind, self.diff, "理由", ("signal:sig1",)),)
        return Composition(candidates=cands, chosen=None if self.decision == "none" else self.diff,
                           decision=self.decision,  # type: ignore[arg-type]
                           citations=() if self.decision == "none" else ("signal:sig1",),
                           rationale=self.rationale, probes=(), comparisons=(),
                           run_id=f"harness:{deps.round_id}:improver", cost=c.cost or 0.0)


class _Meter:
    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        pass


def improver_gateway() -> Gateway:
    turn_ = {"text": "好", "tool_calls": [], "input_tokens": 100, "output_tokens": 50}
    return Gateway(GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True),
                   DeepScript().log, mode="record",
                   providers={"scripted": ScriptedChatProvider("scripted", lambda m, r: turn_)},
                   usage=UsageControl(meter=_Meter(), prices=PRICES), clock=lambda: NOW)


class Env:
    def __init__(self, pool, **kw):
        make_user(pool, U)
        self.pool = pool
        self.registry = MemoryRegistry()
        self.registry.seed(U, TeachingSpec(version=1, items=()), NOW - timedelta(days=1))
        self.marks: list[tuple] = []
        self.cases = kw.pop("cases", caseset())
        self.improver = kw.pop("improver", FakeImprover(H4))
        self.replay = kw.pop("replay", FakeReplay())
        self.judge = kw.pop("judge", prefer("候选"))
        self.count = kw.pop("count", 10)
        self.settings = kw.pop("settings", RsiSettings(harness=True, cycle="every_10_feedback"))  # no week cap
        self.ds = DeepScript()
        self.selected = 0
        self.read_since: list = []               # the `since` of every signals(user, since) read
        judge_prov = ScriptedProvider("deepseek", lambda m, r: self.judge(m, r))
        n = [0]

        def ids(prefix):
            n[0] += 1
            return f"{prefix}_{n[0]}"

        def select(conn, user_id, signals, spec, *, now, repo):
            self.selected += 1
            return self.cases

        def mark(user_id, pages, change_id, now):
            self.marks.append((user_id, tuple(pages), change_id))
            return len(pages)

        deep = DeepService(repo=None, jobs=None, assets=None, specs=None,  # type: ignore[arg-type]
                           checker=None, llm=self.ds.llm(), clock=lambda: NOW, new_id=ids)
        self.deps = replace(RoundDeps(
            pool=pool, repo=None, deep=deep, gateway=improver_gateway(),  # type: ignore[arg-type]
            registry=self.registry, settings=lambda uid: self.settings,
            signals=lambda uid, since: self.read_since.append(since) or SIGNALS,
            signals_since=lambda uid, since: self.count,
            defaults=defaults(), judges=JudgeConfigs(deepseek=DS, anthropic=None),
            judge_providers=lambda: {"deepseek": judge_prov}, tz=UTC, new_id=ids,
            select=select, improve=self.improver, replay=self.replay, mark_stale=mark), **kw)

    def run(self, **kw):
        return run_round(U, self.deps, now=kw.pop("now", NOW), **kw)

    def rounds(self):
        with self.pool.connection() as conn:
            return harness_rsi.rounds(conn, U, 20)

    def changes(self):
        with self.pool.connection() as conn:
            return conn.execute("select * from harness_change where user_id = %s "
                                "order by created_at", (U,)).fetchall()

    def lessons(self):
        with self.pool.connection() as conn:
            return harness_rsi.experience(conn, U, query=None, limit=10, now=NOW)

    def pack(self):
        with self.pool.connection() as conn:
            return pack_store.load_pack(conn, U)


# ---- due -------------------------------------------------------------------------------------

def test_a_round_is_due_at_ten_signals_since_the_last_round(pool):
    env = Env(pool, count=9)
    r = env.run()
    assert (r.round_id, r.status, r.reason) == (None, "skipped", NOT_DUE)
    assert env.rounds() == [] and env.selected == 0
    env.count = 10
    r = env.run()
    assert r.round_id is not None and r.status == "applied"
    seen = []
    env.deps = replace(env.deps, signals_since=lambda uid, since: seen.append(since) or 3)
    assert env.run(now=NOW + timedelta(hours=1)).status == "skipped"
    assert seen == [NOW]                      # counted from the last round's start


def test_harness_off_is_never_due_but_a_manual_round_runs(pool):
    env = Env(pool, settings=RsiSettings(harness=False))
    assert env.run().reason == NOT_DUE and env.rounds() == []
    assert env.run(manual=True).status == "applied"
    env.settings = RsiSettings(harness=True, cycle="manual")
    assert env.run(now=NOW + timedelta(hours=1)).reason == NOT_DUE


# ---- skip reasons ------------------------------------------------------------------------------

def test_no_judge_for_the_writer_family_skips_before_anything_runs(pool):
    env = Env(pool, judges=JudgeConfigs(deepseek=None, anthropic=None))
    r = env.run()
    assert r.status == "skipped" and "DeepSeek" in r.reason
    (row,) = env.rounds()
    assert (row.status, row.reason, row.cost) == ("skipped", r.reason, 0.0)
    assert env.selected == 0 and env.improver.seen == [] and env.replay.calls == []


def test_no_deep_tier_skips(pool):
    env = Env(pool)
    env.deps = replace(env.deps, deep=replace(env.deps.deep, llm=None))
    r = env.run()
    assert (r.status, r.reason) == ("skipped", NO_DEEP)
    for gone in ({"deep": None}, {"gateway": None}):     # the service had none: still recorded
        env.deps = replace(env.deps, **gone)
        r = env.run(manual=True)
        assert (r.status, r.reason) == ("skipped", NO_DEEP) and r.round_id is not None
    assert [x.status for x in env.rounds()] == ["skipped"] * 3


def test_the_month_cap_counts_this_month_and_the_estimate(pool):
    env = Env(pool, settings=RsiSettings(harness=True, harness_month_limit_usd=1.0))
    with pool.connection() as conn:          # two earlier rounds this month: 0.3 + 0.5
        for i, c in enumerate((0.3, 0.5, 0.0)):       # a ~free (thin) round is not averaged
            rid = harness_rsi.start_round(conn, U, finding=None, now=NOW - timedelta(days=2 - i))
            harness_rsi.finish_round(conn, rid, "failed", reason="x", cost=c,
                                     now=NOW - timedelta(days=2 - i))
    r = env.run(manual=True)                 # 0.8 spent + estimate 0.4 >= 1.0
    assert r.status == "skipped"
    assert r.reason == MONTH_CAP.format(spent=0.8, est=0.4, limit=1.0)
    assert env.improver.seen == []
    env.settings = RsiSettings(harness=True, harness_month_limit_usd=1.3)
    assert env.run(manual=True).status == "applied"      # 0.8 + 0.4 < 1.3
    env.settings = RsiSettings(harness=True, cycle="weekly")              # None = no cap
    assert env.run(manual=True).status == "applied"


def test_a_replay_by_a_family_without_a_judge_still_applies(pool):
    """The smoke check judges nothing, so the replays' writer family needs no judge."""
    env = Env(pool, replay=FakeReplay(provider=lambda c, d: "deepseek"))   # the fallback wrote
    assert env.run().status == "applied"


# ---- thin ------------------------------------------------------------------------------------

def test_fewer_than_three_problem_cases_is_thin_without_an_improver_run(pool):
    env = Env(pool, cases=caseset(n=2))
    r = env.run()
    assert r.status == "thin" and "2 段" in r.reason
    assert env.improver.seen == [] and env.replay.calls == []
    (row,) = env.rounds()
    assert row.finding == "讲得太浅：页一" and row.cases["unusable"] == [
        {"id": "sig9", "reason": "反馈没有落在某一段上"}]
    assert env.lessons() == []


def test_two_thin_rounds_on_the_same_finding_record_the_notice_once(pool):
    env = Env(pool, cases=caseset(n=1))
    first = env.run(manual=True)
    second = env.run(manual=True, now=NOW + timedelta(hours=1))
    third = env.run(manual=True, now=NOW + timedelta(hours=2))
    assert THIN_NOTICE not in first.reason
    assert second.reason == f"{first.reason}；{THIN_NOTICE}"          # the reason is kept
    assert third.status == "thin" and THIN_NOTICE not in third.reason
    # every thin round read the same signals (since the start: no round consumed them)
    assert env.read_since == [None, None, None]
    assert [r.signal_ids for r in env.rounds()] == [[s.signal_id for s in SIGNALS]] * 3
    env.cases = caseset(n=1, finding="别的问题")
    assert THIN_NOTICE not in env.run(manual=True, now=NOW + timedelta(hours=3)).reason


# ---- which rounds consume their signals --------------------------------------------------------

def test_a_thin_round_leaves_its_signals_for_the_next_manual_round(pool):
    # live round d47077e4: the round after a thin one read no signals and was thin again
    env = Env(pool, cases=caseset(n=1))
    assert env.run().status == "thin"
    env.cases = caseset()
    r = env.run(manual=True, now=NOW + timedelta(hours=1))
    assert r.status == "applied" and env.read_since == [None, None]
    assert env.rounds()[0].signal_ids == [s.signal_id for s in SIGNALS]
    env.run(manual=True, now=NOW + timedelta(hours=2))
    assert env.read_since[-1] == NOW + timedelta(hours=1)      # the applied round consumed them


def test_due_still_counts_from_a_thin_round(pool):
    env = Env(pool, cases=caseset(n=1))
    assert env.run().status == "thin"
    seen = []
    env.deps = replace(env.deps, signals_since=lambda uid, since: seen.append(since) or 3)
    assert env.run(now=NOW + timedelta(hours=1)).reason == NOT_DUE
    assert seen == [NOW]                       # a thin round does not re-trigger on every signal
    env.deps = replace(env.deps, signals_since=lambda uid, since: seen.append(since) or 10)
    assert env.run(now=NOW + timedelta(hours=2)).status == "thin"
    assert seen[-1] == NOW and env.read_since == [None, None]


@pytest.mark.parametrize(("status", "consumes"), [
    ("applied", True), ("asked", True), ("failed", True), ("no_change", True),
    ("thin", False), ("skipped", False), ("error", False), ("running", False)])
def test_only_a_round_that_decided_on_its_evidence_consumes_its_signals(pool, status, consumes):
    env = Env(pool)
    earlier = NOW - timedelta(hours=1)
    with pool.connection() as conn:
        rid = harness_rsi.start_round(conn, U, finding="讲得太浅：页一", now=earlier)
        if status != "running":
            harness_rsi.finish_round(conn, rid, status, reason="x", cost=0.0, now=earlier)
    assert env.run(manual=True).status == "applied"
    assert env.read_since == [earlier if consumes else None]
    assert (status in harness_rsi.CONSUMING) is consumes


@pytest.mark.parametrize("status", ["applied", "asked", "failed", "no_change", "thin", "skipped",
                                    "error"])
def test_each_status_a_round_ends_with_consumes_or_leaves_the_signals(pool, status):
    setup = {"applied": {}, "asked": {"approvals": FakeApprovals(ask=True)},
             "failed": {"replay": FakeReplay(broken={"case:run-1:s2"})},
             "no_change": {"improver": FakeImprover(None, decision="none")},
             "thin": {"cases": caseset(n=1)},
             "skipped": {"judges": JudgeConfigs(deepseek=None, anthropic=None)},
             "error": {"registry": MemoryRegistry()}}[status]     # no teaching spec → error
    env = Env(pool, **{k: v for k, v in setup.items() if k not in ("judges", "registry")})
    if "judges" in setup or "registry" in setup:
        env.deps = replace(env.deps, **{k: v for k, v in setup.items()
                                        if k in ("judges", "registry")})
    assert env.run().status == status
    # then a plain applying round for the same user
    env.deps = replace(env.deps, judges=JudgeConfigs(deepseek=DS, anthropic=None),
                       registry=env.registry, approvals=None, improve=FakeImprover(H4),
                       replay=FakeReplay())
    env.cases, env.judge = caseset(), prefer("候选")
    env.read_since.clear()
    assert env.run(manual=True, now=NOW + timedelta(hours=1)).status == "applied"
    assert env.read_since == [NOW if status in harness_rsi.CONSUMING else None]


def test_cases_no_pack_can_write_are_unusable_and_can_make_the_round_thin(pool):
    env = Env(pool, replay=FakeReplay(fail={"case:run-1:s2", "case:run-2:s2"}))
    r = env.run()
    assert r.status == "thin"
    (row,) = env.rounds()
    reasons = {u["id"]: u["reason"] for u in row.cases["unusable"]}
    assert reasons["case:run-1:s2"].startswith("重放没写出这一段")
    assert reasons["case:run-2:s2"].startswith("重放没写出这一段")
    assert env.pack().version == 0 and env.lessons() == []


# ---- fail / pass -----------------------------------------------------------------------------

def test_a_case_the_candidate_cannot_write_fails_with_an_ineffective_lesson(pool):
    env = Env(pool, replay=FakeReplay(broken={"case:run-2:s2"}))
    r = env.run()
    assert r.status == "failed" and "新设置写不出 1 段" in r.reason
    assert env.pack().version == 0 and env.changes() == [] and env.marks == []
    (lesson,) = env.lessons()
    assert lesson.verdict == "ineffective" and lesson.scope == "general"
    assert lesson.situation == {"agent": "deep", "finding": "讲得太浅：页一",
                                "signal_kinds": ["check_unclear", "page_feedback"]}
    assert [b["id"] for b in lesson.evidence["broken"]] == ["case:run-2:s2"]
    (row,) = env.rounds()
    assert row.decision["gate"]["verdict"] == "fail"
    assert row.decision["gate"]["broken"][0]["reason"].startswith("重放没写出这一段")


def test_a_pass_applies_the_change_marks_the_pages_and_records_everything(pool):
    env = Env(pool, improver=FakeImprover(H1))
    r = env.run()
    assert r.status == "applied" and r.change_id is not None
    pack = env.pack()
    assert pack.version == 1 and pack.values["H1.write"] == H1.ops[0].value
    with pool.connection() as conn:
        (v,) = pack_store.history(conn, U)
    assert v.source == "improver" and v.why == "工具次数不够用"
    (ch,) = env.changes()
    assert (ch["change_id"], ch["status"], ch["version"]) == (r.change_id, "applied", 1)
    assert ch["cost_before"] is None and ch["cost_after"] is None       # not an H4 change
    diff, before, spec_v = diff_from_json(ch["diff"])
    assert diff == H1 and before == {"H1.write": None} and spec_v is None
    assert env.marks == [(U, ("p1", "p2", "p3"), r.change_id)]
    (row,) = env.rounds()
    assert row.status == "applied" and row.improver_run_id == f"harness:{row.round_id}:improver"
    assert row.decision["gate"]["written"] == SMOKE_CASES
    assert row.signal_ids == [s.signal_id for s in SIGNALS]
    assert r.reason == f"已改：新设置写出了 {SMOKE_CASES} 段" and not row.comparisons
    # smoke replays: the composed diff alone on the first SMOKE_CASES cases, nothing judged
    assert env.replay.calls == [(c.case_id, H1) for c in env.cases.problem[:SMOKE_CASES]]
    assert {x["id"]: x["reason"] for x in row.cases["skipped"]} == {
        c.case_id: round_mod.NOT_SMOKED
        for c in (*env.cases.problem[SMOKE_CASES:], *env.cases.anchors)}
    assert env.lessons() == []                               # lessons of applied changes: Task 11


def test_an_applied_change_with_spec_ops_goes_through_the_registry_and_undoes(pool):
    diff = PackDiff(ops=H1.ops, spec_ops=(SpecPatchOp(op="add_item", item_id="h-1", text=ITEM),))
    env = Env(pool, improver=FakeImprover(diff, decision="b"))
    r = env.run()
    assert r.status == "applied"
    cur = env.registry.current(U)
    assert cur.version == 2 and [i.text for i in cur.spec.items] == [ITEM]
    assert cur.patch_id.startswith("harness:")
    (ch,) = env.changes()
    assert diff_from_json(ch["diff"])[2] == 1                # the spec version before
    with pool.connection() as conn:                          # the stored shape is what undo needs
        undo_change(conn, U, r.change_id, registry=env.registry, now=NOW)
    assert env.registry.current(U).spec.items == () and env.pack().values == {}


def test_a_failing_pack_write_rolls_the_spec_commit_back(pool, monkeypatch):
    diff = PackDiff(ops=H1.ops, spec_ops=(SpecPatchOp(op="add_item", item_id="h-1", text=ITEM),))
    env = Env(pool, improver=FakeImprover(diff))

    def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(round_mod.pack_store, "set_value", broken)
    r = env.run()
    assert r.status == "error" and "RuntimeError" in r.reason
    cur = env.registry.current(U)
    assert cur.version == 3 and cur.spec.items == ()          # committed, then undone
    assert env.changes() == [] and env.marks == []


def test_a_spec_undo_that_fails_too_names_the_leftover_spec_version(pool, monkeypatch):
    diff = PackDiff(ops=H1.ops, spec_ops=(SpecPatchOp(op="add_item", item_id="h-1", text=ITEM),))
    env = Env(pool, improver=FakeImprover(diff))

    def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(round_mod.pack_store, "set_value", broken)
    monkeypatch.setattr(round_mod.spec_ops, "revert", broken)
    r = env.run()
    assert r.status == "error"
    assert r.reason == round_mod.SPEC_LEFT.format(kind="RuntimeError", version=2)
    assert env.registry.current(U).version == 2                # the ops are still in v2
    (row,) = env.rounds()
    assert row.decision["spec_left_version"] == 2


def test_a_failing_round_record_after_the_apply_says_the_change_is_applied(pool, monkeypatch):
    env = Env(pool)
    real = harness_rsi.finish_round
    calls = []

    def flaky(conn, round_id, status, **kw):
        calls.append(status)
        if status == "applied":
            raise RuntimeError("db down")
        return real(conn, round_id, status, **kw)
    monkeypatch.setattr(round_mod.harness_rsi, "finish_round", flaky)
    r = env.run()
    (ch,) = env.changes()
    assert calls == ["applied", "error"] and r.status == "error" and r.change_id == ch["change_id"]
    assert r.reason == round_mod.APPLIED_ERROR.format(cid="h_" + ch["change_id"],
                                                      kind="RuntimeError")
    assert env.rounds()[0].reason == r.reason

    def down(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(round_mod.harness_rsi, "finish_round", down)
    r2 = env.run(manual=True, now=NOW + timedelta(hours=1))    # even that record fails: no raise
    assert r2.status == "error"


def test_a_failing_round_record_after_an_ask_says_the_change_waits(pool, monkeypatch):
    # final review M6: an asked change is not 「已经应用」
    env = Env(pool, approvals=FakeApprovals(ask=True))
    real = harness_rsi.finish_round

    def flaky(conn, round_id, status, **kw):
        if status == "asked":
            raise RuntimeError("db down")
        return real(conn, round_id, status, **kw)
    monkeypatch.setattr(round_mod.harness_rsi, "finish_round", flaky)
    r = env.run()
    (ch,) = env.changes()
    assert ch["status"] == "asked" and r.status == "error"
    assert r.reason == round_mod.ASKED_ERROR.format(cid="h_" + ch["change_id"], kind="RuntimeError")
    assert "等你批准" in r.reason and env.pack().version == 0


def test_a_skip_whose_record_fails_still_returns(pool, monkeypatch):
    # final review M7: the skip path keeps the no-raise contract (POST /wiki/harness/run: no 500)
    env = Env(pool)
    env.deps = replace(env.deps, deep=None)

    def down(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(round_mod.harness_rsi, "finish_round", down)
    r = env.run(manual=True)
    assert (r.status, r.reason, r.change_id) == ("error", NO_DEEP, None)


def test_harness_marks_carry_their_own_reason(pool):
    from tests.harness.test_replay import World

    world = World(pool)
    n = round_mod.mark_pages(world.repo, world.ids, U, [world.pid, "gone"], "c9", NOW)
    assert n == 1
    mark = world.repo.load(U).state.reorder_marks[world.pid]
    assert (mark.job_id, mark.reason) == ("h_c9", "harness")


def test_an_undone_change_clears_only_its_own_marks(pool):
    from evowiki.harness.change_source import clear_marks
    from tests.harness.test_replay import World

    world = World(pool)
    round_mod.mark_pages(world.repo, world.ids, U, [world.pid], "c9", NOW)
    assert clear_marks(world.repo, world.ids, U, "c8", NOW) is False      # not its mark
    assert world.pid in world.repo.load(U).state.reorder_marks
    assert clear_marks(world.repo, world.ids, U, "c9", NOW) is True
    assert world.pid not in world.repo.load(U).state.reorder_marks
    assert clear_marks(world.repo, world.ids, U, "c9", NOW) is False      # nothing left


def test_an_h4_change_carries_the_replay_costs(pool):
    env = Env(pool, replay=FakeReplay(cost=(0.01, 0.03)))
    r = env.run()
    (ch,) = env.changes()
    assert r.status == "applied"
    assert ch["cost_before"] == pytest.approx(0.01) and ch["cost_after"] == pytest.approx(0.03)


def test_the_improver_choosing_none_is_recorded_as_no_change(pool):
    env = Env(pool, improver=FakeImprover(None, decision="none", rationale="证据不够，这一轮不改"))
    r = env.run()
    assert (r.status, r.reason) == ("no_change", "证据不够，这一轮不改")
    assert env.replay.calls == [] and env.changes() == []


def test_a_key_the_user_set_meanwhile_is_not_overwritten(pool):
    env = Env(pool)

    def improve(user_id, cases, *, deps):
        with pool.connection() as conn:                      # the user edits during the round
            pack_store.set_value(conn, U, "H4.deep.max_tool_calls", 90, source="user", why="我改的",
                                 now=NOW)
        return FakeImprover(H4)(user_id, cases, deps=deps)
    env.deps = replace(env.deps, improve=improve)
    r = env.run()
    assert (r.status, r.reason) == ("failed", round_mod.STALE_PACK)
    assert env.pack().values == {"H4.deep.max_tool_calls": 90} and env.changes() == []


# ---- asking the user -------------------------------------------------------------------------

def test_an_ask_level_waits_for_approval_then_applies(pool):
    ap = FakeApprovals(ask=True)
    env = Env(pool, approvals=ap)
    r = env.run()
    assert r.status == "asked" and env.pack().version == 0 and env.marks == []
    (ch,) = env.changes()
    assert ch["status"] == "asked" and ch["cost_before"] == pytest.approx(0.01)
    (p,) = ap.proposals
    assert (p["kind"], p["scope"], p["key"]) == (METHOD_KIND, RSI, ledger_key(U, r.change_id))
    assert p["content"]["change_id"] == "h_" + r.change_id and p["content"]["diff"]
    # the H4 cost change is in the ledger summary too, as a one-section replay mean
    assert "（原来 默认值，重放一节平均约多花 $0.010）" in p["summary"], p["summary"]
    track = HarnessRounds(env.deps)
    assert track.asked_keys(U) == [("h_" + r.change_id, ledger_key(U, r.change_id))]
    effect = Effect()
    track.approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert effect.n == 1 and env.pack().values == {"H4.deep.max_tool_calls": 100}
    (ch,) = env.changes()
    assert (ch["status"], ch["version"]) == ("applied", 1)
    assert env.marks == [(U, ("p1", "p2", "p3"), r.change_id)]
    with pytest.raises(PendingGone):
        track.approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert track.asked_keys(U) == []


def test_an_asked_change_is_stale_when_the_pack_moved_and_can_be_rejected(pool):
    env = Env(pool, approvals=FakeApprovals(ask=True))
    track = HarnessRounds(env.deps)
    first = env.run()
    with pool.connection() as conn:
        pack_store.set_value(conn, U, "H4.deep.max_tool_calls", 90, source="user", why="我改的",
                             now=NOW)
    effect = Effect()
    with pytest.raises(PendingStale):
        track.approve(U, "h_" + first.change_id, now=NOW, on_accept=effect)
    assert effect.n == 0 and env.changes()[0]["status"] == "rejected"
    second = env.run(manual=True, now=NOW + timedelta(hours=1))
    track.reject(U, "h_" + second.change_id, now=NOW)
    assert [c["status"] for c in env.changes()] == ["rejected", "rejected"]
    assert env.pack().values == {"H4.deep.max_tool_calls": 90}
    with pytest.raises(PendingGone):
        track.reject(U, "h_" + second.change_id, now=NOW)


# ---- cost ------------------------------------------------------------------------------------

def test_the_round_cost_is_the_sum_of_the_calls_it_made(pool, monkeypatch):
    """The real sandboxed replay (scripted deep writer): every replay's plan and write calls, the
    improver's call (once: its repeat is served from the log) and the judge's calls. The script
    answers in order, so the smoke replays run one at a time here."""
    from evowiki.harness import replay as replay_mod
    from tests.harness.test_replay import World

    monkeypatch.setattr(replay_mod, "REPLAY_WORKERS", 1)

    world = World(pool)
    ds = DeepScript().plan().write(*_round(1, "现行"))
    rec = world.recorded(ds)
    for i in range(SMOKE_CASES):               # the composed diff on the smoke cases
        ds.plan().write(*_round(20 + i, "候选"))
    cases = CaseSet(problem=tuple(replace(rec, case_id=f"case:{k}", signal_ids=(f"sig{k}",))
                                  for k in range(3)), anchors=(), unusable=(), finding="太浅")
    deps = _world_deps(world, world.service(ds), cases)
    before = len(ds.log.calls())
    r = run_round(U, deps, now=NOW, manual=True)
    made = ds.log.calls()[before:]
    assert r.status == "applied", r.reason
    assert ds.pending() == {}                  # both smoke replays ran for real
    assert sorted({c.purpose for c in made}) == ["deep.write", "rsi.h.improve"]   # no judge
    # the improver ran once; its repeat was a cache hit: a logical call of its own (observability
    # 0090) that costs 0 and names the recorded call, so the round's cost counts the call once
    first, repeat = [c for c in made if c.purpose == "rsi.h.improve"]
    assert first.served_from is None and first.cost
    assert repeat.served_from == first.id and repeat.cost == 0.0
    expected = round(sum(c.cost or 0.0 for c in made), 6)
    assert expected > 0 and r.cost == pytest.approx(expected)
    with pool.connection() as conn:
        assert harness_rsi.last_round(conn, U).cost == pytest.approx(expected)


def _round(i: int, word: str) -> list[dict]:
    body = f"s2 {word}写法的正文：先从印章讲起。"
    return [turn(call("read_spec", i, part="items"), section("s2", i, body=body)), turn(finish(i))]


def _world_deps(world, deep, cases) -> RoundDeps:
    from evowiki.wikiapp.spec_store import RepoSpecRegistry
    from tests.harness.test_replay import log_writer_models
    judge_prov = ScriptedProvider("deepseek", prefer("候选"))
    return RoundDeps(
        pool=world.pool, repo=world.repo, deep=deep, gateway=improver_gateway(),
        registry=RepoSpecRegistry(world.repo, world.ids),
        settings=lambda uid: RsiSettings(harness=True, cycle="weekly"), signals=lambda uid, since: SIGNALS,
        signals_since=lambda uid, since: 10, defaults=defaults(),
        judges=JudgeConfigs(deepseek=DS, anthropic=None),
        judge_providers=lambda: {"deepseek": judge_prov}, tz=UTC, new_id=world.ids,
        select=lambda conn, uid, sigs, spec, *, now, repo: cases, improve=FakeImprover(H1),
        writer_models=log_writer_models(world.pool, deep.llm.log))
