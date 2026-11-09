"""Assistant turns as replay cases in a harness round (harness 3b follow-up 3, plan task 4): two
rejected assistant proposals, each on its own traced turn (recorded by the real assistant loop into
the test database), become turn cases next to one deep case; the improver's assistant H2 change is
replayed for real (`replay_assistant.replay_turn`, tools from the cassette) on the round's scripted
gateway, judged with the assistant prompt, gated and applied. Fakes only: every model is scripted,
nothing reaches a real provider."""

import json
import re
from dataclasses import replace
from datetime import timedelta

from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import PURPOSE as TURN
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore
from evowiki.harness import replay as replay_mod
from evowiki.harness import replay_assistant as ra
from evowiki.harness.anchors import LENGTH_RATIO, SMOKE_CASES
from evowiki.harness.cases import ASSISTANT_FINDING, CaseSet, assistant_cases
from evowiki.harness.diff import UNMEASURABLE, KeyOp, PackDiff
from evowiki.harness.improver import ImproverRun
from evowiki.harness.pack import HarnessPack
from evowiki.harness.replay import ReplayDeps
from evowiki.harness.round import (
    NOT_AFFECTED,
    NOT_DUE,
    NOT_SMOKED,
    TURN_MIXED,
    TURN_REPLAY_FAILED,
    run_round,
)
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import Price, PriceTable
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage_control import UsageControl
from evowiki.store import harness as store_harness
from evowiki.store.llm_logs import PgCallLog
from evowiki.store.trace import PgTraceSink
from evowiki.teach.contracts import RsiSettings
from evowiki.wikiapp.refine_service import ReplayFailed
from evowiki.wikiapp.signals import assistant_rejections
from tests.assistant.fakes import DRAFT, INFO, Data, FakeTool, Ids
from tests.assistant.fakes import T0 as TURN_T0
from tests.harness.conftest import _Meter
from tests.harness.test_improver import Improver, deps
from tests.harness.test_judge_assistant import gateway as assistant_judge_gateway
from tests.harness.test_round import NOW, Env, FakeImprover, FakeReplay, U, case
from tests.runtime.helpers import make_user
from tests.runtime.llm_helpers import MemoryCallLog

W = ModelRef(provider="anthropic", model="m")          # a scripted provider under that name
W_CFG = GatewayConfig(generator=W, verifier=W, allow_single_provider=True)
PRICES = PriceTable({"anthropic:m": Price(1000.0, 2000.0)})
GUIDANCE = "先答，再问一个小问题。"
NEW_DOC = "读学习者对这些概念掌握得怎样；回答前先读，再决定从哪里讲起。"
OLD_ANSWER = "z 分数就是一个数离平均值有几个标准差那么远，先记住这个定义再说别的。"
NEW_ANSWER = "先看例子：考了 85 分，平均 75 分，标准差 5 分，z 就是 2。"
H2 = PackDiff(ops=(KeyOp("H2.assistant.read_k", NEW_DOC),))
ONE_DEEP = CaseSet(problem=(case(1),), anchors=(), unusable=(), finding="讲得太浅：页一")
SHOWN = re.compile(r"【甲】\n(.*?)\n\n【乙】\n(.*?)\n\n请判断", re.DOTALL)


def record_turns(pool, n: int = 2) -> tuple[MemorySessionStore, list[str]]:
    """`n` turns of the real loop for U (H1.assistant set by the user, pack version 1), each
    reading read_k then answering OLD_ANSWER on `W`; then a proposal of each turn rejected."""
    with pool.connection() as conn:
        store_harness.set_value(conn, U, "H1.assistant", GUIDANCE, source="user", why="t",
                                now=TURN_T0)
        pack = store_harness.load_pack(conn, U)
    assert pack.version == 1
    outs = iter([t for _ in range(n) for t in (
        ChatTurn(tool_calls=(ToolCall(id="c1", name="read_k", args={}),),
                 input_tokens=1000, output_tokens=500),
        ChatTurn(text=OLD_ANSWER, input_tokens=1000, output_tokens=500))])
    prov = ScriptedChatProvider("anthropic", lambda m, r: next(outs))
    gw = Gateway(W_CFG, PgCallLog(pool), mode="record", providers={"anthropic": prov},
                 usage=UsageControl(meter=_Meter(), prices=PRICES))
    store = MemorySessionStore()
    store.create(INFO)
    tdeps = TurnDeps(gateway=gw.with_harness(lambda uid: pack),
                     tools=[FakeTool("read_k", "read")], store=store, data=Data(),
                     new_id=Ids(), clock=lambda: TURN_T0,
                     trace=PgTraceSink(pool, clock=lambda: TURN_T0))
    for i in range(n):
        events = list(run_turn(tdeps, INFO, MessageIn(text=f"z 分数怎么理解？（第 {i + 1} 次问）")))
        assert events[-1].type == "done"
    mids = [e.payload["message_id"] for e in store.entries("s1") if e.type == "user"]
    for i, mid in enumerate(mids):
        at = TURN_T0 + timedelta(minutes=i + 1)
        store.append("s1", f"px{i}", "proposal", {"proposal_id": f"pr{i}", "message_id": mid,
                                                  "draft": {**DRAFT, "heading": f"补充{i}"}}, at)
        store.append("s1", f"dx{i}", "decision", {"proposal_id": f"pr{i}", "status": "rejected",
                                                  "applied": [], "detail": ""}, at)
    return store, [f"assistant:s1:{mid}" for mid in mids]


def round_gateway(sent: list):
    """The round's gateway (W): an assistant replay answers NEW_ANSWER when read_k carries
    NEW_DOC, else OLD_ANSWER; the improver's own turn gets a short text."""
    def chat(model, req):
        sent.append(req)
        if req.purpose == TURN:
            doc = next(t.description for t in req.tools if t.name == "read_k")
            return ChatTurn(text=NEW_ANSWER if doc == NEW_DOC else OLD_ANSWER,
                            input_tokens=1000, output_tokens=500)
        return ChatTurn(text="好", input_tokens=100, output_tokens=50)
    return Gateway(W_CFG, MemoryCallLog(), mode="record",
                   providers={"anthropic": ScriptedChatProvider("anthropic", chat)},
                   usage=UsageControl(meter=_Meter(), prices=PRICES))


def pick(first: str, second: str) -> dict:
    for side, text, other in (("甲", first, second), ("乙", second, first)):
        if NEW_ANSWER in text and NEW_ANSWER not in other:
            return {"better": side, "quote": NEW_ANSWER, "reason": "先举例子"}
    return {"better": "same", "quote": "", "reason": "差不多"}


class Judge:
    """The round's judge: a page prompt is a tie; an assistant prompt prefers NEW_ANSWER."""

    def __init__(self):
        self.requests: list = []

    def __call__(self, model, req):
        self.requests.append(req)
        m = SHOWN.search(req.prompt)
        out = ({"better": "same", "quote": "", "reason": "差不多"} if m is None
               else pick(m.group(1), m.group(2)))
        return json.dumps(out, ensure_ascii=False)


def dispatching(fake: FakeReplay):
    """The round's replay step: assistant cases through the real `harness.replay.replay`
    (its dispatch to `replay_turn`), deep cases through the fake writer."""
    def run(case_, candidate, *, base_pack, base_spec, deps):
        if case_.kind == "assistant":
            return replay_mod.replay(case_, candidate, base_pack=base_pack, base_spec=base_spec,
                                     deps=deps)
        return fake(case_, candidate, base_pack=base_pack, base_spec=base_spec, deps=deps)
    return run


def env(pool, sent, judge, *, n: int = 2, cases: CaseSet = ONE_DEEP, diff: PackDiff = H2,
        fake: FakeReplay | None = None, **kw) -> tuple[Env, list[str]]:
    e = Env(pool, cases=cases, improver=FakeImprover(diff, rationale="助手先讲定义，被拒绝了"),
            replay=dispatching(fake or FakeReplay()), judge=judge, gateway=round_gateway(sent))
    store, runs = record_turns(pool, n)
    e.deps = replace(e.deps, sessions=store, **kw)
    return e, runs


NO_DEEP = CaseSet(problem=(), anchors=(), unusable=(), finding="")
TWO_DEEP = CaseSet(problem=(case(1), case(2, page="p2")),
                   anchors=(case(10, anchor=True, page="px"),), unusable=(),
                   finding="讲得太浅：页一")
THREE_DEEP = replace(TWO_DEEP, problem=(*TWO_DEEP.problem, case(3, page="p3")))
H4_DEEP = KeyOp("H4.deep.max_tool_calls", 100)


def test_three_rejected_turns_give_an_applied_assistant_h2_change(pool):
    assert len(NEW_ANSWER) <= LENGTH_RATIO * len(OLD_ANSWER)        # the length rule stays out
    sent: list = []
    judge = Judge()
    e, runs = env(pool, sent, judge, n=3, cases=NO_DEEP)
    r = e.run()
    assert r.status == "applied", r.reason
    assert e.pack().values["H2.assistant.read_k"] == NEW_DOC

    (rnd,) = e.rounds()
    turn_ids = [f"turn:{run}" for run in runs]
    problem = rnd.cases["problem"]
    assert [c["case_id"] for c in problem] == turn_ids
    assert [c.get("kind") for c in problem] == ["assistant"] * 3
    assert [c["signal_ids"] for c in problem] == [["arej:pr0"], ["arej:pr1"], ["arej:pr2"]]
    assert rnd.cases["unusable"] == []
    assert rnd.cases["skipped"] == [{"id": turn_ids[2], "reason": NOT_SMOKED}]
    assert rnd.finding == ASSISTANT_FINDING and rnd.cases["agent"] == "assistant"
    assert {"arej:pr0", "arej:pr1", "arej:pr2"} <= set(rnd.signal_ids)
    gate = rnd.decision["gate"]
    assert (gate["verdict"], gate["written"]) == ("pass", SMOKE_CASES)
    assert judge.requests == [] and not rnd.comparisons          # the smoke check judges nothing

    # the first SMOKE_CASES turns replayed once each, under the candidate (NEW_DOC), on the
    # round's gateway; their spend is in the round's cost
    turns = [q for q in sent if q.purpose == TURN]
    docs = [next(t.description for t in q.tools if t.name == "read_k") for q in turns]
    assert len(turns) == SMOKE_CASES and set(docs) == {NEW_DOC}
    assert all(GUIDANCE in q.system for q in turns)
    assert r.cost >= SMOKE_CASES * 2.0          # 1000 in + 500 out at $1000 / $2000 per Mtok

    # the improver saw the turns behind the signals and could replay them; an assistant-only
    # change marks no page for a rewrite
    seen = e.improver.seen[0]
    assert seen.signal_runs == dict(zip(("arej:pr0", "arej:pr1", "arej:pr2"), runs, strict=True))
    assert seen.replay.assistant is not None
    assert e.marks == []


def test_an_assistant_only_diff_does_not_replay_the_deep_cases(pool):
    """#11: deep cases and anchors are listed as skipped (not affected), never replayed; the
    smoke check writes the first turns only."""
    sent: list = []
    fake = FakeReplay()
    judge = Judge()
    e, runs = env(pool, sent, judge, n=3, cases=TWO_DEEP, fake=fake)
    r = e.run()
    assert r.status == "applied", r.reason
    (rnd,) = e.rounds()
    assert [c.get("kind", "deep") for c in rnd.cases["problem"]] == [
        "deep", "assistant", "deep", "assistant", "assistant"]
    assert fake.calls == []                                   # no deep replay at all
    assert rnd.cases["skipped"] == [
        {"id": "case:run-1:s2", "reason": NOT_AFFECTED},
        {"id": "case:run-2:s2", "reason": NOT_AFFECTED},
        {"id": f"turn:{runs[2]}", "reason": NOT_SMOKED},
        {"id": "anchor:run-10:s2", "reason": NOT_AFFECTED}]
    assert len([q for q in sent if q.purpose == TURN]) == SMOKE_CASES
    assert rnd.decision["gate"]["written"] == SMOKE_CASES
    assert rnd.cases["agent"] == "mixed" and e.marks == []


def test_a_deep_only_diff_does_not_replay_the_turn_cases(pool):
    sent: list = []
    fake = FakeReplay()
    e, runs = env(pool, sent, Judge(), n=2, cases=THREE_DEEP, fake=fake,
                  diff=PackDiff((H4_DEEP,)))
    e.run()
    (rnd,) = e.rounds()
    turn_ids = [f"turn:{run}" for run in runs]
    assert [c["case_id"] for c in rnd.cases["problem"] if c.get("kind") == "assistant"] == turn_ids
    assert [q for q in sent if q.purpose == TURN] == []       # no turn replayed
    # H4 is a cost key: each smoke case also under the current pack, for the change's cost line
    # (the replays run at once: any order)
    assert sorted((c, not d.ops) for c, d in fake.calls) == [
        ("case:run-1:s2", False), ("case:run-1:s2", True),
        ("case:run-2:s2", False), ("case:run-2:s2", True)]
    skipped = {x["id"]: x["reason"] for x in rnd.cases["skipped"]}
    assert skipped == {**dict.fromkeys(turn_ids, NOT_AFFECTED),
                       "case:run-3:s2": NOT_SMOKED, "anchor:run-10:s2": NOT_SMOKED}


def test_a_mixed_diff_replays_both_kinds(pool):
    sent: list = []
    fake = FakeReplay()
    e, _ = env(pool, sent, Judge(), n=3, cases=TWO_DEEP, fake=fake,
                  diff=PackDiff((H4_DEEP, *H2.ops)))
    e.run()
    (rnd,) = e.rounds()
    # the first SMOKE_CASES cases, one of each kind (deep and turn cases alternate)
    # the H4 cost key: each one under the candidate and the current pack
    assert [c for c, _ in fake.calls] == ["case:run-1:s2", "case:run-1:s2"]
    assert len([q for q in sent if q.purpose == TURN]) == 2
    assert all(x["reason"] == NOT_SMOKED for x in rnd.cases["skipped"])


def test_a_failed_turn_replay_is_unusable_in_plain_words(pool, caplog):
    """#13: the reason speaks of an answer, with no failure code or raw error; the detail is in
    the log."""
    def failing(case_, candidate, *, base_pack, base_spec, deps):
        if case_.kind == "assistant" and candidate.ops:
            raise ReplayFailed("model_mismatch", "the traced model is not configured")
        return FakeReplay()(case_, candidate, base_pack=base_pack, base_spec=base_spec,
                            deps=deps)
    sent: list = []
    e, runs = env(pool, sent, Judge(), n=3, cases=NO_DEEP)
    e.deps = replace(e.deps, replay=failing)
    with caplog.at_level("WARNING", logger="evowiki.harness.round"):
        r = e.run()
    assert r.status == "failed" and TURN_REPLAY_FAILED in r.reason   # the candidate broke them
    (rnd,) = e.rounds()
    assert rnd.decision["gate"]["broken"] == [{"id": f"turn:{run}", "reason": TURN_REPLAY_FAILED}
                                              for run in runs[:SMOKE_CASES]]
    assert "model_mismatch" in caplog.text
    (lesson,) = e.lessons()                   # MF7: situation.agent follows the cases' kinds
    assert lesson.situation["agent"] == "assistant"
    assert lesson.situation["signal_kinds"] == ["assistant_rejected"]
    assert not re.search("[A-Za-z]", TURN_REPLAY_FAILED + TURN_MIXED)


def test_an_unusable_turn_is_listed_with_its_reason_and_takes_no_slot(pool):
    """The pack history disagrees with the traced system prompt (the recorded guidance is not
    the one the turn was sent with): each rejection of that turn is unusable, said in plain
    words; the round is thin on the one deep case, with no improver run and no replay."""
    sent: list = []

    def wrong_history(gw):
        pg = ra.pg_deps(pool, gw)
        return ra.AssistantReplayDeps(gateway=gw, turn=pg.turn,
                                      pack_at=lambda uid, v: HarnessPack(v, {
                                          "H1.assistant": "别的写法。"}))
    e, _ = env(pool, sent, Judge(), assistant_replay=wrong_history)
    r = e.run()
    assert r.status == "thin" and "只有 1 段" in r.reason
    (rnd,) = e.rounds()
    assert [c["case_id"] for c in rnd.cases["problem"]] == ["case:run-1:s2"]
    assert rnd.cases["unusable"] == [{"id": "arej:pr0", "reason": ra.SYSTEM_MISMATCH},
                                     {"id": "arej:pr1", "reason": ra.SYSTEM_MISMATCH}]
    assert e.improver.seen == [] and sent == []


def test_an_assistant_key_the_replay_cannot_measure_is_refused_at_propose(pool):
    make_user(pool, "u1")
    from tests.harness.test_improver import CASES
    run = ImproverRun("u1", CASES, deps(pool, Improver()))

    def propose(name, value):
        return run.guard("propose", lambda: run.propose({
            "kind": "specific", "ops": [{"name": name, "value": value}], "spec_ops": [],
            "rationale": "助手回答被拒绝", "citations": ["signal:sig1"]}))

    for name, value, why in (
            ("H5.assistant.top_k_pages", 3, UNMEASURABLE),
            ("H5.assistant.context_budget", 4000, UNMEASURABLE),
            ("H10.assistant.turn.tier", "good", UNMEASURABLE),
            ("H6.keep_budget", 2000, UNMEASURABLE),
            ("H3.assistant.web_search", True, "不能自动打开")):
        res = propose(name, value)
        assert res.error is not None and why in res.content, name
    for name, value in (("H2.assistant.read_k", NEW_DOC), ("H3.assistant.web_search", False),
                        ("H5.assistant.read_keep_calls", 3)):
        assert propose(name, value).error is None, name


def test_the_improvers_probe_and_compare_replay_and_judge_a_turn_case(pool):
    make_user(pool, U)
    store, runs = record_turns(pool)
    rejected = assistant_rejections(store, None, U)
    turns, bad = assistant_cases(rejected, U, check=lambda c: None)
    assert bad == () and [t.run_id for t in turns] == runs
    sent: list = []
    with pool.connection() as conn:
        pack = store_harness.load_pack(conn, U)
    jgw = assistant_judge_gateway(pick)
    d = replace(deps(pool, Improver()),
                replay=ReplayDeps(deep=None, repo=None,  # type: ignore[arg-type]
                                  assistant=ra.pg_deps(pool, round_gateway(sent))),
                base_pack=pack, sources={"H1.assistant": "user"},
                signals=tuple(s for s, _ in rejected),
                signal_runs={s.signal_id: run for s, run in rejected},
                judge_gateway=lambda ref: jgw)
    run = ImproverRun(U, CaseSet(problem=turns, anchors=(), unusable=(),
                                 finding=ASSISTANT_FINDING), d)
    seen = run.read_signals({})
    assert f"signal:arej:pr0｜assistant_rejected｜助手的一次回答 trace:{runs[0]}" in seen
    assert f"- {turns[0].case_id}｜problem｜助手的一次回答｜执行记录 trace:{runs[0]}" in seen
    for kind, op in (("general", {"name": "H2.assistant.read_k", "value": NEW_DOC}),
                     ("specific", {"name": "H4.assistant.max_tool_calls", "value": 3})):
        run.propose({"kind": kind, "ops": [op], "spec_ops": [], "rationale": "被拒绝了",
                     "citations": ["signal:arej:pr0"]})
    out = run.probe({"candidate": "general", "case_ids": [turns[0].case_id]})
    assert NEW_ANSWER in out and OLD_ANSWER in out and "写作模型 anthropic" in out
    res = run.compare({"case_id": turns[0].case_id, "x": "general", "y": "current"})
    # the assistant judge prompt (the test gateway answers "same" to a page prompt)
    assert "结论：general" in res and NEW_ANSWER in res


def test_turn_cases_share_the_cap_and_anchors_stay_only_with_a_deep_case():
    from evowiki.agent.trace import NotReplayable
    from evowiki.harness.cases import TURN_ATTACHMENTS, TURN_INCOMPLETE, with_assistant
    from evowiki.teach.contracts import Signal

    def sig(i):
        return Signal(signal_id=f"arej:p{i}", kind="assistant_rejected", at=TURN_T0, text="拒绝")

    def check(c):
        if c.run_id == "assistant:s1:bad":
            raise NotReplayable("attachments")
        if c.run_id == "assistant:s1:gone":
            raise LookupError("no model step")

    rejected = [(sig(1), "assistant:s1:a"), (sig(2), "assistant:s1:b"), (sig(3), "assistant:s1:b"),
                (sig(4), "assistant:s1:c"), (sig(5), "assistant:s1:bad"),
                (sig(6), "assistant:s1:gone")]
    turns, bad = assistant_cases(rejected, U, check=check)
    assert [t.run_id for t in turns] == ["assistant:s1:b", "assistant:s1:a", "assistant:s1:c"]
    assert turns[0].signal_ids == ("arej:p2", "arej:p3") and turns[0].kind == "assistant"
    assert bad == (("arej:p5", TURN_ATTACHMENTS), ("arej:p6", TURN_INCOMPLETE))

    deep = CaseSet(problem=tuple(case(i) for i in range(1, 6)),
                   anchors=(case(10, anchor=True, page="px"),),
                   unusable=(("exemplar:px", "没有"), ("sig9", "没有")), finding="F")
    out = with_assistant(deep, turns, bad)
    assert [c.kind for c in out.problem] == ["deep", "assistant", "deep", "assistant", "deep"]
    assert out.anchors == deep.anchors and out.finding == f"F；{ASSISTANT_FINDING}"
    assert out.unusable == (*deep.unusable, *bad)

    only = with_assistant(replace(deep, problem=(), finding=""), turns, ())
    assert [c.run_id for c in only.problem] == [t.run_id for t in turns]
    assert only.anchors == () and only.unusable == (("sig9", "没有"),)
    assert only.finding == ASSISTANT_FINDING
    assert with_assistant(deep, (), ()) is deep


def test_assistant_rejections_alone_can_make_a_round_due(pool):
    """MF5: `due` counts the assistant rejections since the last round next to the page signals:
    8 page signals and two rejected turns reach the weekly round's 10; without the session store
    (8), or once those rejections are older than the last round, it is not due."""
    sent: list = []
    e, _ = env(pool, sent, Judge())
    e.count = 8
    e.settings = RsiSettings(harness=True, cycle="weekly")
    without = replace(e.deps, sessions=None)
    assert run_round(U, without, now=NOW).reason == NOT_DUE and e.rounds() == []
    r = e.run()
    assert r.round_id is not None, r.reason
    assert e.run(now=NOW + timedelta(days=8)).reason == NOT_DUE


# ---- 👎 ratings (助手回答评价) ----

def rated_only(store: MemorySessionStore, down: int) -> MemorySessionStore:
    """A copy of `store` without its proposals / decisions (no rejection left), the first `down`
    answers rated 👎 (the first one changed from 👍), the rest 👍."""
    out = MemorySessionStore()
    out.create(INFO)
    turn = ""
    for e in store.entries("s1"):
        if e.type in ("proposal", "decision"):
            continue
        out.append("s1", e.entry_id, e.type, e.payload, e.at)
        if e.type == "user":
            turn = e.payload["message_id"]
        if e.type == "assistant":
            i = len([x for x in out.entries("s1") if x.type == "assistant"])
            pay = {"message_id": e.payload["message_id"], "turn_id": turn, "chips": [], "note": ""}
            at = TURN_T0 + timedelta(minutes=i)
            if i == 1:
                out.append("s1", f"up{i}", "rating", {**pay, "value": "up"}, at)
            out.append("s1", f"rt{i}", "rating",
                       {**pay, "value": "down" if i <= down else "up",
                        "chips": ["太啰嗦"] if i <= down else []}, at + timedelta(seconds=1))
    return out


def test_a_turn_rejected_and_rated_down_is_one_case_and_a_rated_turn_is_a_case():
    from evowiki.harness.judge import case_inputs
    from evowiki.wikiapp.signals import assistant_feedback
    from tests.assistant.fakes import run as one_turn
    store = one_turn(MemorySessionStore())                 # turn m1: answer a1, proposal rejected
    store.append("s1", "u3", "user", {"message_id": "m3", "text": "再问"}, TURN_T0)
    store.append("s1", "a3", "assistant", {"message_id": "a3", "text": "答三"}, TURN_T0)
    for i, (mid, turn) in enumerate((("a1", "m1"), ("a3", "m3"))):
        store.append("s1", f"r{i}", "rating", {"message_id": mid, "turn_id": turn,
                                               "value": "down", "chips": ["不对"], "note": "错"},
                     TURN_T0 + timedelta(minutes=5))
    feedback = assistant_feedback(store, None, "u1")
    turns, bad = assistant_cases(feedback, "u1", check=lambda c: None)
    assert bad == ()
    assert [(t.run_id, t.signal_ids) for t in turns] == [
        ("assistant:s1:m1", ("arej:pr1", "arat:a1")), ("assistant:s1:m3", ("arat:a3",))]
    text, more, kind = case_inputs(turns[1], [s for s, _ in feedback])
    assert (text, more, kind) == ("觉得这次回答不好：不对。补充：错", False, "assistant")


def test_due_counts_down_ratings_since_the_last_round():
    from types import SimpleNamespace

    from evowiki.harness.round import _due_count
    from tests.assistant.fakes import run as one_turn
    store = one_turn(MemorySessionStore())                  # one rejection (arej:pr1)
    deps_ = SimpleNamespace(signals_since=lambda uid, since: 2, sessions=store, ledger=None)
    assert _due_count("u1", deps_, None) == 3
    store.append("s1", "r1", "rating", {"message_id": "a1", "turn_id": "m1", "value": "down",
                                        "chips": [], "note": ""}, TURN_T0 + timedelta(hours=1))
    assert _due_count("u1", deps_, None) == 3          # turn m1 rejected and 👎: counted once
    store.append("s1", "u3", "user", {"message_id": "m3", "text": "再问"}, TURN_T0)
    store.append("s1", "a3", "assistant", {"message_id": "a3", "text": "答三"}, TURN_T0)
    store.append("s1", "r3", "rating", {"message_id": "a3", "turn_id": "m3", "value": "down",
                                        "chips": [], "note": ""}, TURN_T0 + timedelta(hours=1))
    assert _due_count("u1", deps_, None) == 4          # a 👎 turn with no rejection adds one
    since = TURN_T0 + timedelta(minutes=30)            # the rejection is older: both 👎 are new
    assert _due_count("u1", deps_, since) == 4
    store.append("s1", "r2", "rating", {"message_id": "a1", "turn_id": "m1", "value": "up",
                                        "chips": [], "note": ""}, TURN_T0 + timedelta(hours=2))
    assert _due_count("u1", deps_, None) == 4                  # m1 turned 👍: still its rejection
    no_store = SimpleNamespace(signals_since=lambda uid, since: 2, sessions=None, ledger=None)
    assert _due_count("u1", no_store, None) == 2


def test_turns_rated_down_alone_give_an_applied_assistant_change(pool):
    sent: list = []
    e, runs = env(pool, sent, Judge(), n=3, cases=NO_DEEP)
    store = rated_only(e.deps.sessions, down=3)
    e.deps = replace(e.deps, sessions=store)
    r = e.run()
    assert r.status == "applied", r.reason
    (rnd,) = e.rounds()
    answers = [x.payload["message_id"] for x in store.entries("s1") if x.type == "assistant"]
    assert [c["case_id"] for c in rnd.cases["problem"]] == [f"turn:{run}" for run in runs]
    assert [c["signal_ids"] for c in rnd.cases["problem"]] == [[f"arat:{a}"] for a in answers]
    assert {f"arat:{a}" for a in answers} <= set(rnd.signal_ids)
