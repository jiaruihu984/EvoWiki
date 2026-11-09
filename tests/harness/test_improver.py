"""The improver agent (harness RSI phase 2, Task 8): a scripted improver model calls the tools, a
scripted writer replays (the real one-section replay on a `pool` in one test, a fake `replay`
elsewhere) and a scripted judge compares. Nothing reaches a real provider."""

import json
import re
from dataclasses import replace
from datetime import timedelta

import pytest

from evowiki.harness import improver as improver_mod
from evowiki.harness.cases import CaseSet
from evowiki.harness.diff import KeyOp, PackDiff
from evowiki.harness.improver import Candidate, Composition, ImproverDeps, run_improver
from evowiki.harness.improver_prompt import PROMPT_VERSION, PURPOSE
from evowiki.harness.judge import JudgeConfigs
from evowiki.harness.pack import EMPTY
from evowiki.harness.replay import Case, ReplayDeps, ReplayResult
from evowiki.llm.chat import ChatRequest
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl
from evowiki.service.harness_defaults import defaults
from evowiki.store import harness_rsi
from evowiki.store import trace as store_trace
from evowiki.teach.contracts import GateResult, PatchMemoryEntry, Signal
from evowiki.teach.rsi_store import MemoryRecord
from tests.deep.fakes import DeepScript, call, finish, section
from tests.deep.fakes import turn as wturn
from tests.harness.test_judge import DS, judge_gw
from tests.harness.test_replay import World
from tests.runtime.helpers import make_user
from tests.runtime.llm_helpers import MemoryCallLog
from tests.wikiapp.test_goal import NOW

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
PRICES = PriceTable({"scripted:m": Price(1000.0, 2000.0)})
ACC = Account(user_id="u1", group="other")
SIGNALS = (Signal(signal_id="sig1", kind="page_feedback", at=NOW, page_id="p1", section_id="s2",
                  chips=("too_shallow",), text="这里没看懂"),
           Signal(signal_id="sig2", kind="check_unclear", at=NOW, page_id="p1", section_id="s3"))
CMP = re.compile(r"cmp:[0-9a-f]{20}")
PROBE_RUN = re.compile(r"trace:(harness-replay:[^\s｜]+)")
GENERAL = PackDiff(ops=(KeyOp("H4.deep.max_tool_calls", 100),))
SPECIFIC_ITEM = "先给一个能看到结果的小例子，再讲定义。"


class _Meter:
    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        pass


def case(i: int, section_id: str = "s2") -> Case:
    from evowiki.deep.agent import SnapshotSeqs
    return Case(case_id=f"case:run-{i}:{section_id}", run_id=f"run-{i}", page_id="p1",
                section_id=section_id, signal_ids=("sig1",), anchor=False,
                snapshot=SnapshotSeqs(wiki_seq=3, aspect_seq=1, infer_version=1), mode="rewrite",
                instruction="", user_id="u1")


CASES = CaseSet(problem=(case(1), case(2), case(3)), anchors=(), unusable=(("sig2", "反馈没有落在某一段上"),),
                finding="讲得太浅：页一")


# --- the scripted improver --------------------------------------------------------------------

def tc(name: str, i: int, **args) -> dict:
    return {"id": f"{name}-{i}", "name": name, "args": args}


def mturn(*calls: dict, text: str = "") -> dict:
    return {"text": text, "tool_calls": list(calls), "input_tokens": 100, "output_tokens": 50}


class Improver:
    """Answers each improver request with the next step: a turn dict, or a function of the tool
    results the previous turn got (so a step can cite a comparison id it just saw)."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.results: list[list[str]] = []        # tool result texts each request carried back
        self.requests: list[ChatRequest] = []
        self.log = MemoryCallLog()
        self.judges: dict = {}

    def chat(self, model, req: ChatRequest):
        self.requests.append(req)
        last = []
        for m in reversed(req.messages):
            if m.role != "tool":
                break
            last.insert(0, m.text)
        self.results.append(last)
        if not self.steps:
            raise AssertionError("unscripted improver turn")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(last) if callable(step) else step

    def gateway(self) -> Gateway:
        return Gateway(CFG, self.log, mode="record",
                       providers={"scripted": ScriptedChatProvider("scripted", self.chat)},
                       usage=UsageControl(meter=_Meter(), prices=PRICES), clock=lambda: NOW)

    def seen(self) -> list[str]:
        return [t for r in self.results for t in r]


def prefer(word: str):
    """A judge that picks the text containing `word`, quoting its last line."""
    def decide(first, second):
        for side, text in (("甲", first), ("乙", second)):
            if word in text and word not in (second if side == "甲" else first):
                return {"better": side, "quote": text.splitlines()[-1], "reason": "有例子"}
        return {"better": "same", "quote": "", "reason": "差不多"}
    return decide


class FakeWriter:
    """Stands in for `harness.replay.replay`: the text names the candidate's first op value."""

    def __init__(self, provider_of=lambda diff: "anthropic"):
        self.calls: list[tuple[str, PackDiff]] = []
        self.provider_of = provider_of

    def __call__(self, case, candidate, *, base_pack, base_spec, deps):
        self.calls.append((case.case_id, candidate))
        n = len(self.calls)
        if candidate.ops:
            mark = f"候选{candidate.ops[0].value}"
        elif candidate.spec_ops:
            mark = "规则候选"
        else:
            mark = "当前"
        return ReplayResult(case_id=case.case_id, text=f"回忆线索：{mark}\n\n{case.case_id} 的{mark}正文第{n}版。",
                            writer_provider=self.provider_of(candidate),
                            run_id=f"harness-replay:hr{n}:x", cost=0.01)


@pytest.fixture(autouse=True)
def nine_probes(monkeypatch):
    """These tests pin the cap's mechanics (refused whole, failed replays count) on 9 replays, the
    cap they were written for; the shipped cap is checked below."""
    monkeypatch.setattr(improver_mod, "MAX_PROBES", 9)


def test_the_shipped_probe_cap_is_one_section():
    from evowiki.harness import anchors
    assert anchors.MAX_PROBES == 3          # the current pack and two candidates on one section


@pytest.fixture
def writer(monkeypatch):
    w = FakeWriter()
    monkeypatch.setattr(improver_mod, "replay", w)
    return w


def deps(pool, imp: Improver, *, judge=None, sources=None, memory=(), failure=None,
         replay_deps=None, base_spec=None) -> ImproverDeps:
    from evowiki.teach.contracts import TeachingSpec
    judges: dict = {}

    def judge_gateway(ref):
        judges.setdefault(ref.model, judge_gw(judge or prefer("候选100"), ref))
        return judges[ref.model]

    d = ImproverDeps(
        pool=pool, gateway=imp.gateway(), account=ACC,
        replay=replay_deps or ReplayDeps(deep=None, repo=None),  # type: ignore[arg-type]
        base_pack=EMPTY, sources=sources or {}, defaults=defaults(),
        base_spec=base_spec or TeachingSpec(version=1, items=()),
        judges=JudgeConfigs(deepseek=DS, anthropic=None), judge_gateway=judge_gateway,
        signals=SIGNALS, memory=tuple(memory), failure=failure,
        trace=store_trace.PgTraceSink(pool, lambda: NOW), now=NOW, round_id="r1")
    imp.judges = judges              # the judge gateways it built, by model
    return d


def test_experience_citations_require_reading_and_snapshot_excludes_future(pool, writer):
    from evowiki.harness.improver import ImproverRun, _ToolError
    from evowiki.harness.snapshot import SnapshotBoundary

    make_user(pool, "u1")
    make_user(pool, "other")
    with pool.connection() as conn:
        older = harness_rsi.add_experience(conn, "u1", {"problem": "orthogonality"}, {}, {},
                    "ineffective", "user", weight=1, now=NOW - timedelta(days=2))
        foreign = harness_rsi.add_experience(conn, "other", {"problem": "orthogonality foreign"}, {}, {},
                    "effective", "user", weight=1, now=NOW - timedelta(days=1))
    with pool.connection() as conn:
        cutoff = conn.execute('select clock_timestamp() as at').fetchone()['at']
    with pool.connection() as conn:
        future = harness_rsi.add_experience(conn, "u1", {"problem": "orthogonality future"}, {}, {},
                    "effective", "user", weight=1, now=NOW - timedelta(days=5))
    state = ImproverRun("u1", CASES, replace(deps(pool, Improver()), snapshot=SnapshotBoundary(cutoff)))
    with pytest.raises(_ToolError):
        state._cited([f"experience:{older}"])
    page = json.loads(state.read_experience({"query": "orthogonality"}))
    assert [entry["id"] for entry in page["items"]] == [f"experience:{older}"]
    assert state._cited([f"experience:{older}"]) == (f"experience:{older}",)
    for unread in (future, foreign):
        with pytest.raises(_ToolError):
            state._cited([f"experience:{unread}"])


def recorded_run(pool, run_id="run-1"):
    sink = store_trace.PgTraceSink(pool, lambda: NOW)
    sink.start(run_id, user_id="u1", agent="deep.write", subject="p1", pack_version=0,
               input={"mode": "rewrite"})
    from evowiki.agent.tools import ToolResult
    sink.tool(run_id, 1, "t1", "read_k", {"concepts": ["c1"]},
              ToolResult(content="c1：学习者还不会先修概念"))
    sink.call(run_id, 2, LLMRequest(purpose="goals.intake", subject="g", prompt_version="v",
                                   system="", prompt="", params={}),
              "Split", None, "分块失败")   # a turn's JSON call (new_goal's split)
    sink.finish(run_id, stop="finished", error=None)


def cite(*extra):
    return lambda last: mturn(tc("compose", 9, decision="a", ops=[], spec_ops=[],
                                 citations=["signal:sig1", "trace:run-1",
                                            *CMP.findall("\n".join(last))[:1], *extra],
                                 rationale="通用候选在第一段赢了"))


def propose_both(i=1):
    return mturn(
        tc("propose", i, kind="general", ops=[{"name": "H4.deep.max_tool_calls", "value": 100}],
           spec_ops=[], rationale="工具次数用完了", citations=["trace:run-1"]),
        tc("propose", i + 1, kind="specific", ops=[],
           spec_ops=[{"op": "add_item", "text": SPECIFIC_ITEM}],
           rationale="这位学习者要例子", citations=["signal:sig1"]))


# --- tests ------------------------------------------------------------------------------------

def test_the_happy_path_reads_proposes_probes_compares_and_composes(pool, writer):
    make_user(pool, "u1")
    recorded_run(pool)
    with pool.connection() as conn:
        harness_rsi.add_experience(conn, "u1", {"agent": "deep", "signal": "too_shallow"},
                                   {"H1.write": "多举例"}, {"cmp": "cmp:old"}, "ineffective",
                                   "user", weight=1.0, now=NOW - timedelta(days=3))
    gate = GateResult(patch_id="pB", channel="B", passed=False, vetoes=("worse",))
    mem = MemoryRecord(user_id="u1", entry=PatchMemoryEntry(
        patch_id="pB", track="B", signature="prereq_omitted|spec_item|*", path="spec_item",
        gate=gate, status="gate_failed", at=NOW - timedelta(days=2)), direction=0,
        from_value=None, to_value=None, counts_at=NOW, repeat_of_rejected=False,
        outcomes_at_close=0)
    imp = Improver(
        mturn(tc("read_signals", 1), tc("read_experience", 1, query="多举例"),
              tc("read_experience", 2, query="prereq_omitted")),
        mturn(tc("read_trace", 1, run_id="run-1"), tc("read_pack", 1, dim="H4")),
        propose_both(),
        mturn(tc("probe", 1, candidate="general", case_ids=[c.case_id for c in CASES.problem]),
              tc("probe", 2, candidate="specific", case_ids=["case:run-1:s2"])),
        mturn(tc("compare", 1, case_id="case:run-1:s2", x="general", y="current")),
        cite())
    d = deps(pool, imp, memory=[mem], failure="prereq_omitted")
    out = run_improver("u1", CASES, deps=d)

    assert isinstance(out, Composition)
    assert out.decision == "a" and out.chosen == GENERAL
    assert [c.kind for c in out.candidates] == ["general", "specific"]
    general, specific = out.candidates
    assert isinstance(general, Candidate) and general.citations == ("trace:run-1",)
    (op,) = specific.diff.spec_ops
    assert op.op == "add_item" and op.item_id and op.text == SPECIFIC_ITEM   # a fresh id
    # reference once per case (3), general on 3, specific on 1 (its reference is cached)
    assert len(writer.calls) == 7 and len(out.probes) == 7
    assert [d_ for cid, d_ in writer.calls if cid == "case:run-1:s2"].count(PackDiff()) == 1
    (cmp,) = out.comparisons
    assert cmp.winner == "a" and cmp.cmp_id in out.citations
    assert set(out.citations) >= {"signal:sig1", "trace:run-1"}
    assert out.run_id == "harness:r1:improver"

    seen = "\n".join(imp.seen())
    assert "sig1" in seen and "太浅" in seen and "反馈没有落在某一段上" in seen   # signals + cases
    assert "多举例" in seen and "ineffective" in seen                                   # a lesson
    assert "prereq_omitted|spec_item|*" in seen                                  # Patch Memory
    assert "学习者还不会先修概念" in seen                                          # the trace
    assert "#2 模型（goals.intake）：分块失败" in seen                             # a call step
    assert ("- H4.deep.max_tool_calls｜写页面：每轮最多调用几次工具｜来源 默认｜现在：（未设，用默认）｜"
            "默认：80") in seen                                                    # pack + default
    # nothing of the anchors or the gate leaks into what the improver sees
    for hidden in ("MAX_PROBES", "WIN_MARGIN", "LAMBDA", "utility", "1.3"):
        assert hidden not in seen and hidden not in imp.requests[0].system
    req = imp.requests[0]
    assert (req.purpose, req.prompt_version, req.subject) == (PURPOSE, PROMPT_VERSION, "r1")
    assert {t.name for t in req.tools} == {"read_signals", "read_trace", "read_pack",
                                           "read_experience", "propose", "probe", "read_probe",
                                           "compare", "compose"}

    # cost = the improver's calls + the judge's calls + the replays, as recorded
    judge_cost = sum(c.cost or 0 for gw in imp.judges.values() for c in gw._log.calls())
    own = sum(c.cost or 0 for c in imp.log.calls())
    assert own > 0
    assert out.cost == pytest.approx(own + judge_cost + 7 * 0.01)

    # the run is traced like any agent run
    with pool.connection() as conn:
        run = store_trace.run(conn, out.run_id)
        steps = store_trace.steps(conn, out.run_id)
    assert run["agent"] == "improver" and run["stop"] == "finished" and run["finished_at"]
    assert run["user_id"] == "u1" and run["subject"] == "r1"
    kinds = [s["kind"] for s in steps]
    assert kinds.count("model") == 6 and kinds.count("tool") == 11
    assert [s["name"] for s in steps if s["kind"] == "tool"][-1] == "compose"


def test_propose_refuses_a_user_key_and_an_h4_raise_over_the_limit(pool, writer):
    make_user(pool, "u1")
    imp = Improver(
        mturn(tc("propose", 1, kind="general", ops=[{"name": "H1.write", "value": "多举例"}],
                 spec_ops=[], rationale="x", citations=["signal:sig1"])),
        mturn(tc("propose", 2, kind="general",
                 ops=[{"name": "H4.deep.max_tool_calls", "value": 150}],
                 spec_ops=[], rationale="x", citations=["signal:sig1"])),
        mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"],
                 rationale="两个候选都不合格")))
    out = run_improver("u1", CASES, deps=deps(pool, imp, sources={"H1.write": "user"}))
    assert "是你自己设的" in imp.results[1][0]
    assert "1.5 倍" in imp.results[2][0]
    assert out.decision == "none" and out.chosen is None and out.candidates == ()
    assert out.rationale == "两个候选都不合格" and writer.calls == []


def test_probe_needs_that_candidate_first(pool, writer):
    make_user(pool, "u1")
    imp = Improver(
        mturn(tc("propose", 1, kind="general",
                 ops=[{"name": "H4.deep.max_tool_calls", "value": 100}], spec_ops=[],
                 rationale="x", citations=["signal:sig1"])),
        mturn(tc("probe", 1, candidate="specific", case_ids=["case:run-1:s2"])),
        mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"],
                 rationale="没比")))
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    assert "还没有" in imp.results[2][0] and writer.calls == []
    assert out.decision == "none"


def test_the_probe_cap_refuses_the_probe_and_the_run_still_compares_and_composes(pool, writer):
    """Round 1a2caec3: 9 replays, specific re-proposed and probed again — the 10th replay request
    is a tool error, not the end of the run; compare + compose still work."""
    make_user(pool, "u1")
    ids = [c.case_id for c in CASES.problem]
    imp = Improver(
        mturn(tc("read_signals", 1)),
        propose_both(2),
        mturn(tc("probe", 1, candidate="general", case_ids=ids),       # 3 references + 3
              tc("probe", 2, candidate="specific", case_ids=ids)),     # + 3 = 9
        mturn(tc("compare", 1, case_id=ids[0], x="specific", y="current")),
        mturn(tc("propose", 4, kind="specific", ops=[],
                 spec_ops=[{"op": "add_item", "text": "先讲定义。"}], rationale="换一条",
                 citations=["signal:sig1"]),
              tc("probe", 3, candidate="specific", case_ids=ids[:1])),  # one more: over the cap
        mturn(tc("compare", 2, case_id=ids[0], x="general", y="current")),
        cite())
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    assert "最多重放 9 次（" in imp.results[1][0] and "已用 0 次，还剩 9 次" in imp.results[1][0]      # read_signals
    assert "已重放 6 次，还剩 3 次" in imp.results[3][0]
    assert "已重放 9 次，还剩 0 次" in imp.results[3][1]
    warn = imp.results[5][0]                                               # the re-propose
    assert "作废" in warn and "3 次重放" in warn and "1 次比较" in warn and "还剩 0 次" in warn
    refused = imp.results[5][1]
    assert "重放次数用完了" in refused and "还剩 0 次" in refused and "compose" in refused
    assert len(writer.calls) == 9 and len(out.probes) == 9
    assert out.decision == "a" and out.chosen == GENERAL and imp.steps == []
    (cmp,) = out.comparisons                         # the specific one was discarded
    assert cmp.cmp_id in out.citations


def test_a_probe_that_would_partly_exceed_the_cap_spends_nothing(pool, writer):
    make_user(pool, "u1")
    ids = [c.case_id for c in CASES.problem]
    imp = Improver(
        propose_both(),
        mturn(tc("probe", 1, candidate="general", case_ids=ids)),      # 6 used, 3 left
        mturn(tc("probe", 2, candidate="specific", case_ids=ids[:2]),  # 2: fits
              tc("probe", 3, candidate="specific", case_ids=ids[2:])), # 1: fits
        mturn(tc("propose", 4, kind="composed", ops=[{"name": "H4.deep.max_tool_calls", "value": 90}],
                 spec_ops=[], rationale="合并", citations=["signal:sig1"]),
              tc("probe", 4, candidate="composed", case_ids=ids[:1])),  # 1 > 0 left
        mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"],
                 rationale="够了")))
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    assert len(writer.calls) == 9
    assert "还剩 0 次" in imp.results[3][1]
    assert "一次也没有重放" in imp.results[4][1]
    assert out.decision == "none" and out.rationale == "够了"


def test_a_partial_over_cap_request_is_refused_whole(pool, writer):
    make_user(pool, "u1")
    cases = CaseSet(problem=tuple(case(i) for i in range(1, 7)), anchors=(), unusable=(),
                    finding="x")
    ids = [c.case_id for c in cases.problem]
    imp = Improver(
        propose_both(),
        mturn(tc("probe", 1, candidate="general", case_ids=ids[:3])),  # 6 used, 3 left
        mturn(tc("probe", 2, candidate="general", case_ids=ids[3:5])), # needs 4 > 3
        mturn(tc("probe", 3, candidate="specific", case_ids=ids[:3])), # 3 fit
        mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"],
                 rationale="够了")))
    out = run_improver("u1", cases, deps=deps(pool, imp))
    refused = imp.results[3][0]
    assert "还剩 3 次" in refused and "要重放 4 次" in refused and "一次也没有重放" in refused
    assert "少挑几段" in refused
    assert "已重放 9 次，还剩 0 次" in imp.results[4][0]
    assert len(writer.calls) == 9 and out.decision == "none" and out.rationale == "够了"


def test_compose_with_a_bogus_or_missing_citation_is_refused(pool, writer):
    make_user(pool, "u1")
    imp = Improver(
        mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[], citations=[],
                 rationale="x")),
        mturn(tc("compose", 2, decision="none", ops=[], spec_ops=[],
                 citations=["signal:sig1", "cmp:0123456789abcdef0123"], rationale="x")),
        mturn(tc("compose", 3, decision="none", ops=[], spec_ops=[],
                 citations=["trace:nope"], rationale="x")),
        mturn(tc("compose", 4, decision="none", ops=[], spec_ops=[],
                 citations=["signal:sig2", "trace:run-2"], rationale="证据不够")))
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    assert "引用" in imp.results[1][0]
    assert "cmp:0123456789abcdef0123" in imp.results[2][0]
    assert "trace:nope" in imp.results[3][0]
    assert out.decision == "none" and out.citations == ("signal:sig2", "trace:run-2")


def test_the_tool_cap_ends_the_run_with_none_and_a_reason(pool, writer):
    make_user(pool, "u1")
    imp = Improver(mturn(*(tc("read_signals", i) for i in range(61))),
                   mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[],
                            citations=["signal:sig1"], rationale="不该走到这里")))
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    assert out.decision == "none" and "工具" in out.rationale and "60" in out.rationale
    assert len(imp.requests) == 1
    with pool.connection() as conn:
        run = store_trace.run(conn, out.run_id)
    assert run["stop"] == "tool_cap"


def test_compare_of_texts_by_different_writers_is_a_tool_error(pool, monkeypatch):
    make_user(pool, "u1")
    w = FakeWriter(provider_of=lambda d: "deepseek" if d.spec_ops else "anthropic")
    monkeypatch.setattr(improver_mod, "replay", w)
    imp = Improver(
        propose_both(),
        mturn(tc("probe", 1, candidate="specific", case_ids=["case:run-1:s2"]),
              tc("probe", 2, candidate="general", case_ids=["case:run-1:s2"])),
        mturn(tc("compare", 1, case_id="case:run-1:s2", x="specific", y="current"),
              tc("compare", 2, case_id="case:run-1:s2", x="general", y="specific")),
        mturn(tc("compare", 3, case_id="case:run-1:s2", x="current", y="general")),
        lambda last: mturn(tc("compose", 1, decision="a", ops=[], spec_ops=[],
                              citations=CMP.findall("\n".join(last)), rationale="通用更好")))
    d = deps(pool, imp)
    out = run_improver("u1", CASES, deps=d)
    assert "不同的写作模型" in imp.results[3][0] and "不同的写作模型" in imp.results[3][1]
    (cmp,) = out.comparisons
    # the non-current side is `a`, whichever order the model named them in
    assert cmp.winner == "a" and out.decision == "a" and out.citations == (cmp.cmp_id,)


def test_compose_merge_uses_the_composed_candidate_and_checks_it(pool, writer):
    make_user(pool, "u1")
    imp = Improver(
        propose_both(),
        mturn(tc("compose", 1, decision="merge", ops=[], spec_ops=[], citations=["signal:sig1"],
                 rationale="合并")),                                     # no composed yet
        mturn(tc("compose", 2, decision="merge",
                 ops=[{"name": "H4.deep.max_tool_calls", "value": 500}], spec_ops=[],
                 citations=["signal:sig1"], rationale="合并")),           # out of bounds
        mturn(tc("compose", 3, decision="merge",
                 ops=[{"name": "H4.deep.max_tool_calls", "value": 100}],
                 spec_ops=[{"op": "add_item", "text": SPECIFIC_ITEM}],
                 citations=["signal:sig1"], rationale="合并两个")))
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    assert imp.results[2][0] and "composed" in imp.results[2][0]
    assert "200" in imp.results[3][0]
    assert out.decision == "merge" and out.chosen is not None
    assert out.chosen.ops == GENERAL.ops and out.chosen.spec_ops[0].item_id


def test_a_model_error_raises_and_finishes_the_trace_failed(pool, writer):
    make_user(pool, "u1")
    imp = Improver(mturn(tc("read_signals", 1)), RuntimeError("provider down"))
    with pytest.raises(RuntimeError, match="provider down"):
        run_improver("u1", CASES, deps=deps(pool, imp))
    with pool.connection() as conn:
        run = store_trace.run(conn, "harness:r1:improver")
    assert run["finished_at"] is not None and run["error"] == "RuntimeError"


# --- with the real one-section replay ---------------------------------------------------------

def _round(i: int, body: str) -> list[dict]:
    return [wturn(call("read_spec", i, part="items"), section("s2", i, body=body)),
            wturn(finish(i))]


def test_probes_run_the_real_sandboxed_replay(pool):
    world = World(pool)
    ds = DeepScript().plan().write(*_round(1, "录下来的正文。"))
    rec = world.recorded(ds)
    rec = replace(rec, case_id=f"case:{rec.run_id}:s2")
    ds.plan().write(*_round(2, "现在的正文。")).plan().write(*_round(3, "通用候选写的正文。")) \
        .plan().write(*_round(4, "针对性候选写的正文。"))
    cases = CaseSet(problem=(rec,), anchors=(), unusable=(), finding="讲得太浅")
    imp = Improver(
        mturn(tc("propose", 1, kind="general",
                 ops=[{"name": "H2.deep.write_section", "value": "写一段正文；先举例。"}],
                 spec_ops=[], rationale="工具说明", citations=[f"trace:{rec.run_id}"]),
              tc("propose", 2, kind="specific", ops=[{"name": "H1.write", "value": "先举例。"}],
                 spec_ops=[], rationale="写法", citations=["signal:sig1"])),
        mturn(tc("probe", 1, candidate="general", case_ids=[rec.case_id]),
              tc("probe", 2, candidate="specific", case_ids=[rec.case_id])),
        lambda last: mturn(tc("read_trace", 1, run_id=PROBE_RUN.findall("\n".join(last))[0])),
        mturn(tc("compare", 1, case_id=rec.case_id, x="general", y="specific")),
        lambda last: mturn(tc("compose", 1, decision="a", ops=[], spec_ops=[],
                              citations=CMP.findall("\n".join(last)), rationale="通用更好")))
    d = deps(pool, imp, judge=prefer("通用候选"),
             replay_deps=ReplayDeps(deep=world.service(ds), repo=world.repo),
             base_spec=world.spec(rec))
    out = run_improver("u1", cases, deps=d)
    assert ds.pending() == {}
    texts = {p.run_id: p.text for p in out.probes}
    assert len(texts) == 3 and all(r.startswith(store_trace.REPLAY_RUN_PREFIX) for r in texts)
    assert any("通用候选写的正文" in t for t in texts.values())
    seen = "\n".join(imp.seen())
    assert "现在的正文" in seen and "针对性候选写的正文" in seen
    # a probe's trace is written under the case's user, so the improver can read it
    (read,) = imp.results[3]
    assert "没有这条执行记录" not in read and "deep.write" in read
    (cmp,) = out.comparisons
    assert cmp.winner == "a" and out.decision == "a"
    assert out.cost == pytest.approx(
        sum(c.cost or 0 for c in imp.log.calls()) + sum(p.cost for p in out.probes)
        + sum(c.cost or 0 for gw in imp.judges.values() for c in gw._log.calls()))
    assert json.dumps(out.chosen.ops[0].value, ensure_ascii=False) == '"写一段正文；先举例。"'


# --- fix round 1 ------------------------------------------------------------------------------

def test_a_failed_replay_counts_costs_and_is_not_retried(pool, monkeypatch):
    from evowiki.wikiapp.refine_service import ReplayFailed
    make_user(pool, "u1")
    inner = FakeWriter()

    def flaky(case, candidate, **kw):
        if case.case_id == "case:run-2:s2" and candidate == PackDiff():
            raise ReplayFailed("no_draft", "草稿里没有这一段：s2", cost=0.02,
                               run_ids=("harness-replay:hrF:deep",))
        return inner(case, candidate, **kw)

    monkeypatch.setattr(improver_mod, "replay", flaky)
    imp = Improver(
        propose_both(),
        mturn(tc("probe", 1, candidate="general", case_ids=["case:run-1:s2", "case:run-2:s2"])),
        mturn(tc("probe", 2, candidate="specific", case_ids=["case:run-2:s2"])),
        mturn(tc("probe", 3, candidate="specific", case_ids=["case:run-1:s2"])),
        mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[],
                 citations=["trace:harness-replay:hrF:deep"], rationale="第二段写不出来")))
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    first = imp.results[2][0]
    assert "没写出这一段" in first and "trace:harness-replay:hrF:deep" in first
    assert "已重放 3 次" in first      # c1 reference + general, c2 reference (failed); c2 general skipped
    assert "失败过" in imp.results[3][0]        # the failed pair is not retried
    assert "已重放 4 次" in imp.results[4][0]
    assert len(inner.calls) == 3 and len(out.probes) == 3
    assert out.citations == ("trace:harness-replay:hrF:deep",)   # its runs are citable
    own = sum(c.cost or 0 for c in imp.log.calls())
    assert out.cost == pytest.approx(own + 3 * 0.01 + 0.02)


def test_failed_replays_count_against_the_cap(pool, monkeypatch):
    from evowiki.wikiapp.refine_service import ReplayFailed
    make_user(pool, "u1")
    cases = CaseSet(problem=tuple(case(i) for i in range(1, 11)), anchors=(), unusable=(),
                    finding="x")

    def broken(case, candidate, **kw):
        raise ReplayFailed("no_draft", "写不出来", cost=0.01)

    monkeypatch.setattr(improver_mod, "replay", broken)
    ids = [c.case_id for c in cases.problem]
    imp = Improver(
        propose_both(),
        mturn(tc("probe", 1, candidate="general", case_ids=ids[:4])),    # 4 failed references
        mturn(tc("probe", 2, candidate="general", case_ids=ids[4:8])),   # 4 + 8 > 9
        mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[],
                 citations=["signal:sig1"], rationale="写不出来")))
    out = run_improver("u1", cases, deps=deps(pool, imp))
    refused = imp.results[3][0]
    assert "已用 4 次，还剩 5 次" in refused and "一次也没有重放" in refused
    assert out.decision == "none" and out.rationale == "写不出来" and imp.steps == []
    assert out.probes == () and out.cost == pytest.approx(
        sum(c.cost or 0 for c in imp.log.calls()) + 4 * 0.01)


def test_an_unexpected_replay_error_stops_the_run_and_raises(pool, monkeypatch):
    make_user(pool, "u1")

    def down(case, candidate, **kw):
        raise ConnectionError("db down")

    monkeypatch.setattr(improver_mod, "replay", down)
    imp = Improver(propose_both(),
                   mturn(tc("probe", 1, candidate="general", case_ids=["case:run-1:s2"])),
                   mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[],
                            citations=["signal:sig1"], rationale="不该走到这里")))
    with pytest.raises(ConnectionError, match="db down"):
        run_improver("u1", CASES, deps=deps(pool, imp))
    assert len(imp.steps) == 1                    # no model call after the failing tool
    with pool.connection() as conn:
        run = store_trace.run(conn, "harness:r1:improver")
    assert run["error"] == "ConnectionError"


def test_no_compliant_judge_raises_judge_unavailable(pool, writer):
    from evowiki.harness.judge import JudgeUnavailable
    make_user(pool, "u1")
    imp = Improver(propose_both(),
                   mturn(tc("probe", 1, candidate="general", case_ids=["case:run-1:s2"])),
                   mturn(tc("compare", 1, case_id="case:run-1:s2", x="general", y="current")),
                   mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[],
                            citations=["signal:sig1"], rationale="不该走到这里")))
    d = replace(deps(pool, imp), judges=JudgeConfigs(deepseek=None, anthropic=None))
    with pytest.raises(JudgeUnavailable):
        run_improver("u1", CASES, deps=d)
    assert len(imp.steps) == 1


def test_reproposing_a_kind_drops_its_comparisons(pool, writer):
    make_user(pool, "u1")
    imp = Improver(
        propose_both(),
        mturn(tc("probe", 1, candidate="general", case_ids=["case:run-1:s2"])),
        mturn(tc("compare", 1, case_id="case:run-1:s2", x="general", y="current")),
        lambda last: mturn(
            tc("propose", 5, kind="general",
               ops=[{"name": "H4.deep.max_tool_calls", "value": 110}],
               spec_ops=[], rationale="换一个", citations=["signal:sig1"]),
            tc("compose", 1, decision="a", ops=[], spec_ops=[],
               citations=CMP.findall("\n".join(last)), rationale="旧的比较")),
        mturn(tc("compose", 2, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"],
                 rationale="重新来")))
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    assert "不存在" in imp.results[4][1]
    assert out.comparisons == () and out.decision == "none"
    assert out.candidates[0].diff.ops == (KeyOp("H4.deep.max_tool_calls", 110),)


def test_read_probe_gives_the_whole_text_with_an_offset(pool, monkeypatch):
    make_user(pool, "u1")
    long = "很长的正文。" * 1500                     # 9000 chars

    def wordy(case, candidate, **kw):
        return ReplayResult(case_id=case.case_id, text=long + ("候选" if candidate.ops else ""),
                            writer_provider="anthropic",
                            run_id=f"harness-replay:{len(candidate.ops)}:x", cost=0.0)

    monkeypatch.setattr(improver_mod, "replay", wordy)
    imp = Improver(
        propose_both(),
        mturn(tc("probe", 1, candidate="general", case_ids=["case:run-1:s2"])),
        mturn(tc("read_probe", 1, case_id="case:run-1:s2", kind="general"),
              tc("read_probe", 2, case_id="case:run-1:s2", kind="general", offset=8000),
              tc("read_probe", 3, case_id="case:run-1:s2", kind="specific")),
        mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"],
                 rationale="x")))
    run_improver("u1", CASES, deps=deps(pool, imp))
    assert "read_probe" in imp.results[2][0] and "截断" in imp.results[2][0]
    first, second, missing = imp.results[3]
    assert first.startswith(long[:8000]) and "offset=8000" in first
    assert second == long[8000:] + "候选"
    assert "还没有重放" in missing


def test_read_pack_shows_the_real_h2_default_descriptions(pool, writer):
    make_user(pool, "u1")
    imp = Improver(mturn(tc("read_pack", 1, dim="H2")),
                   mturn(tc("compose", 1, decision="none", ops=[], spec_ops=[],
                            citations=["signal:sig1"], rationale="x")))
    run_improver("u1", CASES, deps=deps(pool, imp))
    (text,) = imp.results[1]
    assert "见执行记录" not in text
    assert "- H2.deep.write_section｜工具说明｜来源 默认｜现在：（未设，用默认）｜默认：\"写一段" in text
    assert "H2.deep.write_section.cue｜参数说明" in text and "复习题，只有一个问号" in text


def test_run_evals_shows_each_case_against_the_current_pack_and_why_it_failed(pool, writer):
    from dataclasses import replace
    make_user(pool, "u1")
    imp = Improver(mturn(tc("run_evals", 1, candidate="current")),
                   mturn(tc("propose", 2, kind="general", ops=[{"name": "H4.deep.max_tool_calls", "value": 30}], spec_ops=[],
                            rationale="x", citations=["signal:sig1"])),
                   mturn(tc("run_evals", 3, candidate="general")),
                   mturn(tc("compose", 4, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"], rationale="x")))
    calls = []

    def evals(diff):
        calls.append(diff)
        ok = bool(diff.ops)
        return {"cases": {"act-quiz": {"trials": [ok, ok], "say": ["考考我"],
                                      "failures": [] if ok else [{"basis": "tool_called: 要 open_review", "answer": "题目在这"}]}},
                "cost": {}}
    run_improver("u1", CASES, deps=replace(deps(pool, imp), evals=evals))
    first, second = imp.results[1][0], imp.results[3][0]
    assert "act-quiz｜××" in first and "他说：考考我" in first and "tool_called: 要 open_review" in first
    assert "act-quiz｜TT（××）" in second
    assert len(calls) == 2                  # the reference runs once


def test_compose_with_an_unmeasured_candidate_gets_the_board_and_does_not_end_the_run(pool, writer):
    from dataclasses import replace
    make_user(pool, "u1")
    prop = {"ops": [{"name": "H4.deep.max_tool_calls", "value": 30}], "spec_ops": [], "rationale": "x", "citations": ["signal:sig1"]}
    imp = Improver(mturn(tc("propose", 1, kind="general", **prop), tc("propose", 2, kind="specific", **{**prop, "ops": [{"name": "H4.deep.max_tool_calls", "value": 31}]})),
                   mturn(tc("compose", 3, decision="a", ops=[], spec_ops=[], citations=["signal:sig1"], rationale="x")),
                   mturn(tc("compose", 4, decision="b", ops=[], spec_ops=[], citations=["signal:sig1"], rationale="x")))
    calls = []

    def evals(diff):
        calls.append(diff)
        good = bool(diff.ops) and diff.ops[0].value == 31
        return {"cases": {"act-quiz": {"trials": [good, good], "say": ["考考我"],
                                      "failures": [] if good else [{"basis": "没调工具", "answer": "题目"}]}},
                "cost": {}}
    comp = run_improver("u1", CASES, deps=replace(deps(pool, imp), evals=evals))
    assert len(calls) == 3                  # current and both candidates, once each
    assert comp.decision == "b"
    board = imp.results[2][0]
    assert "act-quiz｜××｜××｜TT" in board and "没调工具" in board and "还没有交卷" in board


def test_run_evals_is_only_offered_with_the_evaluation_set(pool, writer):
    make_user(pool, "u1")
    from evowiki.harness.improver import ImproverRun
    assert "run_evals" not in [t.spec.name for t in ImproverRun("u1", CASES, deps(pool, Improver())).tools]


def test_said_is_what_he_typed_not_the_context_pack():
    from evowiki.harness.improver import _said
    steps = [{"kind": "tool"}, {"kind": "model", "messages": [{"role": "user", "text": "图谱里有很多点\n\n他问：\n出道题考我"}]}]
    assert _said(steps) == "出道题考我"


def test_closed_steps_are_neither_listed_nor_writable(pool, writer):
    # constraint 2 (2026-10-28): the improver never sees a closed H1 step (the evidence: feedback)
    # as changeable, and propose and compose refuse them even when it names them
    make_user(pool, "u1")
    imp = Improver(
        mturn(tc("read_pack", 1, dim="H1")),
        mturn(tc("propose", 1, kind="general", ops=[{"name": "H1.feedback", "value": "新的更好"}],
                 spec_ops=[], rationale="x", citations=["signal:sig1"])),
        propose_both(3),
        mturn(tc("compose", 1, decision="merge",
                 ops=[{"name": "H1.feedback", "value": "都不算矛盾"}], spec_ops=[],
                 citations=["signal:sig1"], rationale="x")),
        mturn(tc("compose", 2, decision="none", ops=[], spec_ops=[], citations=["signal:sig1"],
                 rationale="x")))
    out = run_improver("u1", CASES, deps=deps(pool, imp))
    listing = imp.results[1][0]
    assert "- H1.write｜" in listing
    assert "- H1.feedback｜" not in listing
    assert "只能由你在 Prompt 设置里改" in imp.results[2][0]
    assert "只能由你在 Prompt 设置里改" in imp.results[4][0]
    assert out.decision == "none"
