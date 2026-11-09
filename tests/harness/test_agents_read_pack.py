"""The three agents read their limits, context sizes, compaction policy and tool texts from the
user's harness pack (Task 5): an empty pack sends exactly today's requests, a set value takes
effect, a bad one fails the run, and agent code reads the pack only through `harness.apply`.
Scripted providers only: no paid API."""

from dataclasses import replace
from pathlib import Path

import pytest

from evowiki.agent import durable
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore
from evowiki.deep.agent import RefineFailed
from evowiki.deep.writer import PageWriter, WriteFailed
from evowiki.harness import apply as harness_apply
from evowiki.harness.apply import pack_for_run
from evowiki.harness.pack import EMPTY, HarnessPack
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.wikiapp.repo import MemoryRepo
from tests.assistant.fakes import T0, Data, Ids, tools
from tests.assistant.test_loop import CFG, INFO
from tests.deep.fakes import DeepScript, FakeChecker, call, library, turn
from tests.deep.test_writer import outline_input, write_all
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids as WikiIds
from tests.wikiapp.test_page import organized

SRC = Path(__file__).resolve().parents[2] / "src" / "evowiki"
NO_SOURCE = object()          # the gateway is built without a harness source


def dumps(requests):
    return [r.model_dump() for r in requests]


def harness(pack):
    return {} if pack is NO_SOURCE else {"harness": lambda uid: pack}


# ---- deep page writer ----

def deep_run(pack, script):
    env = (MemoryRepo(["u1"]), Script(), WikiIds())
    inp = outline_input(env, organized(env))
    ds = script(DeepScript().plan())
    llm = ds.llm() if pack is NO_SOURCE else replace(ds.llm(), harness=lambda uid: pack)
    w = PageWriter(llm=llm, checker=FakeChecker(), library=library())
    try:
        w.write(inp)
        failed = None
    except WriteFailed as exc:
        failed = exc
    return ds, failed


def test_deep_empty_pack_sends_identical_requests():
    plain, f1 = deep_run(NO_SOURCE, lambda ds: ds.write(*write_all()))
    empty, f2 = deep_run(EMPTY, lambda ds: ds.write(*write_all()))
    assert f1 is None and f2 is None
    assert dumps(plain.requests) == dumps(empty.requests) and len(plain.requests) == 5


def test_deep_pack_caps_tools_rewrites_and_rewords_a_tool():
    pack = HarnessPack(1, {"H4.deep.max_tool_calls": 10, "H4.deep.max_rewrites": 0,
                           "H2.deep.read_spec": "新说明"})
    reads = [turn(call("read_spec", i, part="items")) for i in range(12)]
    ds, failed = deep_run(pack, lambda s: s.write(*reads))
    assert failed is not None and failed.failure == "no_draft"      # one round only
    assert ds.count("deep.write") == 10                              # stopped at the 10th call
    write = next(r for r in ds.requests if r.purpose == "deep.write")
    assert next(t for t in write.tools if t.name == "read_spec").description == "新说明"


def test_deep_bad_pack_is_the_refines_normal_failure():
    """A pack naming a tool the writer never has fails the refine closed, as a RefineFailed
    (`internal`, the reason kept), never an uncaught HarnessInvalid; no write call is made."""
    ds, failed = deep_run(HarnessPack(1, {"H2.deep.call_tool": "新说明"}), lambda s: s)
    assert isinstance(failed, RefineFailed) and failed.failure == "internal"
    assert "HarnessInvalid" in failed.detail and "没有这个设置" in failed.detail
    assert ds.count("deep.write") == 0


def test_deep_pack_naming_a_forged_tool_this_run_lacks_is_skipped():
    """A pattern-valid unknown name may be a forged tool (P3): accepted, and skipped at apply when
    this run does not offer it; the refine runs as with an empty pack."""
    pack = HarnessPack(1, {"H2.deep.nope": "新说明"})
    plain, f1 = deep_run(EMPTY, lambda ds: ds.write(*write_all()))
    ds, f2 = deep_run(pack, lambda s: s.write(*write_all()))
    assert f1 is None and f2 is None
    assert dumps(plain.requests) == dumps(ds.requests)


# ---- reading assistant ----

def assistant_run(tmp_path, pack, script):
    outs = iter(script)
    prov = ScriptedChatProvider("scripted", lambda m, r: next(outs))
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"scripted": prov}, **harness(pack))
    store = MemorySessionStore()
    store.create(INFO)
    ts = tools()
    deps = TurnDeps(gateway=gw, tools=ts, store=store, data=Data(), new_id=Ids(), clock=lambda: T0)
    events = list(run_turn(deps, INFO, MessageIn(text="z 分数是什么？")))
    return prov, events, ts, store


def read_k_then_answer():
    return [ChatTurn(tool_calls=(ToolCall(id="c1", name="read_k", args={}),)),
            ChatTurn(tool_calls=(ToolCall(id="c2", name="read_k", args={}),)),
            ChatTurn(text="答")]


def test_assistant_empty_pack_sends_identical_requests(tmp_path):
    plain, e1, _, _ = assistant_run(tmp_path / "a", NO_SOURCE, read_k_then_answer())
    empty, e2, _, _ = assistant_run(tmp_path / "b", EMPTY, read_k_then_answer())
    assert e1[-1].type == e2[-1].type == "done"
    assert dumps(plain.chat_calls) == dumps(empty.chat_calls) and len(plain.chat_calls) == 3


def test_assistant_pack_forces_the_answer_after_one_call_and_rewords_a_tool(tmp_path):
    pack = HarnessPack(1, {"H4.assistant.max_tool_calls": 1, "H2.assistant.read_k": "新说明"})
    prov, events, ts, _ = assistant_run(
        tmp_path, pack, [ChatTurn(tool_calls=(ToolCall(id="c1", name="read_k", args={}),)),
                         ChatTurn(text="只能这样答")])
    assert events[-1].type == "done" and events[-1].tool_calls == 1
    assert [r.tool_choice for r in prov.chat_calls] == ["auto", "none"]
    assert ts[0].calls == [{}]                          # the original tool ran
    assert next(t for t in prov.chat_calls[0].tools if t.name == "read_k").description == "新说明"


def test_assistant_pack_top_k_zero_drops_the_related_pages(tmp_path):
    header = "他的 Wiki 里相关的页面"
    plain, _, _, _ = assistant_run(tmp_path / "a", NO_SOURCE, [ChatTurn(text="答")])
    assert header in plain.chat_calls[0].messages[-1].text
    pack = HarnessPack(1, {"H5.assistant.top_k_pages": 0})
    prov, events, _, _ = assistant_run(tmp_path / "b", pack, [ChatTurn(text="答")])
    assert events[-1].type == "done" and header not in prov.chat_calls[0].messages[-1].text


def test_assistant_contradictory_compaction_fails_the_turn_closed(tmp_path):
    pack = HarnessPack(1, {"H6.keep_budget": 50000})        # over the default history_budget
    prov, events, _, store = assistant_run(tmp_path, pack, [ChatTurn(text="答")])
    assert [e.type for e in events] == ["error"]
    assert "压缩设置互相矛盾" in events[0].detail
    assert prov.chat_calls == [] and store.entries(INFO.session_id) == []   # nothing logged


# ---- one read per run, recorded inside a workflow ----

class Source:
    def __init__(self, *packs):
        self.packs, self.reads = list(packs), 0

    def __call__(self, uid):
        self.reads += 1
        return self.packs.pop(0)


def test_pack_for_run_outside_a_workflow_is_a_plain_read(tmp_path):
    src = Source(HarnessPack(3, {"H4.bot.max_tool_calls": 2}))
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", harness=src)
    assert pack_for_run(gw, "u1") == HarnessPack(3, {"H4.bot.max_tool_calls": 2})
    assert src.reads == 1


def test_pack_for_run_in_a_workflow_is_a_recorded_step(tmp_path, monkeypatch):
    """`virtual_workflow` checkpoints nothing, so the recovery is played by a recorder standing
    in for `durable.step`: the second read in the same run replays the first step's value."""
    src = Source(HarnessPack(3, {"H4.bot.max_tool_calls": 2}),
                 HarnessPack(4, {"H4.bot.max_tool_calls": 7}))
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", harness=src)
    recorded: dict[str, object] = {}
    real = durable.step

    def replaying(name, fn):
        if name not in recorded:
            recorded[name] = real(name, fn)
        return recorded[name]

    monkeypatch.setattr(durable, "step", replaying)
    with durable.virtual_workflow("wf1"):
        first = pack_for_run(gw, "u1")
        again = pack_for_run(gw, "u1")
    assert list(recorded) == ["harness.pack"]
    assert recorded["harness.pack"] == {"version": 3, "values": {"H4.bot.max_tool_calls": 2}}
    assert first == again == HarnessPack(3, {"H4.bot.max_tool_calls": 2}) and src.reads == 1


def test_pack_for_run_step_value_is_json(tmp_path):
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                 harness=lambda uid: HarnessPack(2, {"H2.bot.suggest": "新说明"}))
    with durable.virtual_workflow("wf2"):
        pack = pack_for_run(gw, "u1")
    assert pack == HarnessPack(2, {"H2.bot.suggest": "新说明"})
    assert harness_apply._dump(pack) == {"version": 2, "values": {"H2.bot.suggest": "新说明"}}


# ---- narrow accessors ----

@pytest.mark.parametrize("package", ["deep", "assistant", "bot"])
def test_agent_code_reads_the_pack_only_through_apply(package):
    bad = [f"{p.relative_to(SRC)}: {needle}" for p in (SRC / package).rglob("*.py")
           for needle in ("pack.int(", "pack.text(", "pack.get(", "pack.values",
                          "pack.with_prefix(", "pack.flag(", "pack.choice(", "pack.items(")
           if needle in p.read_text(encoding="utf-8")]
    assert bad == []
