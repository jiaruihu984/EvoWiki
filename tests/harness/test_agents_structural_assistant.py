"""The assistant turn reads the structural pack keys (phase 3a Task 7): H3 tool
toggles, H10 tier pin / thinking, H9 before-answer delegate notes. An empty pack sends exactly
today's requests on today's models. Scripted providers only: no paid API."""

from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import NOTES_HEAD, TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore
from evowiki.harness.pack import EMPTY, HarnessPack
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway, ProviderUnavailableError
from evowiki.llm.log import CallLog
from evowiki.llm.models import THINKING_PARAM, GatewayConfig, ModelRef
from evowiki.llm.prices import PriceTable
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage_control import UsageControl
from tests.agent.test_loop_trace import Recorder
from tests.assistant.fakes import T0, Data, Ids, tools
from tests.assistant.test_loop import INFO, SUMMARY
from tests.harness.conftest import _Meter

NO_SOURCE = object()

FAST = GatewayConfig(generator=ModelRef(provider="scripted", model="m-fast"),
                     verifier=ModelRef(provider="scripted", model="m-fast"),
                     allow_single_provider=True)
GOOD = GatewayConfig(generator=ModelRef(provider="scripted", model="m-good"),
                     verifier=ModelRef(provider="scripted", model="m-good"),
                     allow_single_provider=True)


class Chosen:
    """The user's explicit group tier (engine panel); None = no choice."""

    def __init__(self, tier=None):
        self.tier = tier

    def chosen_tier(self, account):
        return self.tier


def dumps(requests):
    return [r.model_dump() for r in requests]


def gateway(tmp_path, pack, script, *, chosen=None):
    outs = iter(script)
    models: list[str] = []

    def answer(model, req):
        models.append(model)
        out = next(outs)
        if isinstance(out, Exception):
            raise out
        return out

    prov = ScriptedChatProvider("scripted", answer)
    usage = UsageControl(meter=_Meter(), prices=PriceTable({}),
                         tier_configs={"fast": FAST, "good": GOOD}, tiers=Chosen(chosen))
    extra = {} if pack is NO_SOURCE else {"harness": lambda uid: pack}
    gw = Gateway(GOOD, CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"scripted": prov}, usage=usage, **extra)
    return gw, prov, models


def assistant_run(tmp_path, pack, script, *, chosen=None, trace=None):
    gw, prov, models = gateway(tmp_path, pack, script, chosen=chosen)
    store = MemorySessionStore()
    store.create(INFO)
    ts = tools()
    kw = {} if trace is None else {"trace": trace}
    deps = TurnDeps(gateway=gw, tools=ts, store=store, data=Data(), new_id=Ids(), clock=lambda: T0, **kw)
    events = list(run_turn(deps, INFO, MessageIn(text="z 分数是什么？")))
    return prov, models, events, store, ts


def read_k_then_answer():
    return [ChatTurn(tool_calls=(ToolCall(id="c1", name="read_k", args={}),)),
            ChatTurn(text="答")]


def delegate(**kw):
    return {"at": "assistant.before_answer", "kind": "delegate", "tier": "fast",
            "tools": ["read_k"], "instruction": "先读他的 K", "output_to": "context.notes",
            "max_tool_calls": 2, **kw}


# ---- (a) empty pack ----

def logged_calls(tmp_path):
    """The gateway's call log, without what differs between any two runs (id, time, latency)."""
    return [c.model_dump(exclude={"id", "ts", "latency"})
            for c in CallLog(tmp_path / "c.jsonl").calls()]


def test_assistant_empty_pack_is_identical(tmp_path):
    p1, m1, e1, s1, _ = assistant_run(tmp_path / "a", NO_SOURCE, read_k_then_answer())
    p2, m2, e2, s2, _ = assistant_run(tmp_path / "b", EMPTY, read_k_then_answer())
    assert e1[-1].type == e2[-1].type == "done"
    assert [e.model_dump() for e in e1] == [e.model_dump() for e in e2]
    sid = INFO.session_id
    assert ([(e.type, e.payload) for e in s1.entries(sid)]
            == [(e.type, e.payload) for e in s2.entries(sid)])
    c1, c2 = logged_calls(tmp_path / "a"), logged_calls(tmp_path / "b")
    assert c1 == c2 and len(c1) == 2
    assert dumps(p1.chat_calls) == dumps(p2.chat_calls) and len(p1.chat_calls) == 2
    assert m1 == m2 == ["m-good", "m-good"]              # the assistant group's default tier
    assert all(r.params == {"message_id": p1.chat_calls[0].params["message_id"]}
               for r in p1.chat_calls)


# ---- (b) H3 ----

def test_assistant_tool_switched_off_is_not_offered(tmp_path):
    pack = HarnessPack(1, {"H3.assistant.web_search": False})
    prov, _, events, _, _ = assistant_run(tmp_path, pack, [ChatTurn(text="答")])
    assert events[-1].type == "done"
    # ask_user: every turn offers it now (an inline turn waits too)
    assert [t.name for t in prov.chat_calls[0].tools] == ["read_k", "propose_page_edit", "ask_user"]


def test_assistant_tool_switched_on_is_the_default(tmp_path):
    p1, _, _, _, _ = assistant_run(tmp_path / "a", EMPTY, [ChatTurn(text="答")])
    p2, _, _, _, _ = assistant_run(tmp_path / "b", HarnessPack(1, {"H3.assistant.web_search": True}),
                                   [ChatTurn(text="答")])
    assert dumps(p1.chat_calls) == dumps(p2.chat_calls)


# ---- (c) H10 tier ----

def test_assistant_tier_pin_routes_the_turn_and_the_users_choice_wins(tmp_path):
    pack = HarnessPack(1, {"H10.assistant.turn.tier": "fast"})
    _, models, events, _, _ = assistant_run(tmp_path / "a", pack, read_k_then_answer())
    assert events[-1].type == "done" and models == ["m-fast", "m-fast"]
    _, models, _, _, _ = assistant_run(tmp_path / "b", pack, read_k_then_answer(), chosen="good")
    assert models == ["m-good", "m-good"]


def test_assistant_compaction_keeps_the_unpinned_tier_while_the_turn_is_pinned(tmp_path):
    """H10.assistant.turn pins only the turn's own calls: the compaction request goes on the
    unpinned account (the assistant group's default, good)."""
    from evowiki.agent.compact import CompactPolicy
    from evowiki.assistant.compact import COMPACT_PURPOSE

    routed: list[tuple[str, str]] = []

    def answer(model, req):
        routed.append((req.purpose, model))
        assert req.purpose != COMPACT_PURPOSE
        return ChatTurn(text="答" + "字" * 30)

    def summarise(model, req):
        routed.append((req.purpose, model))
        assert req.purpose == COMPACT_PURPOSE
        return SUMMARY

    prov = ScriptedChatProvider("scripted", answer, summarise)
    usage = UsageControl(meter=_Meter(), prices=PriceTable({}),
                         tier_configs={"fast": FAST, "good": GOOD}, tiers=Chosen())
    pack = HarnessPack(1, {"H10.assistant.turn.tier": "fast"})
    gw = Gateway(GOOD, CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"scripted": prov}, usage=usage, harness=lambda uid: pack)
    store = MemorySessionStore()
    store.create(INFO)
    deps = TurnDeps(gateway=gw, tools=tools(), store=store, data=Data(), new_id=Ids(), clock=lambda: T0,
                    compaction=CompactPolicy(history_budget=200, keep_turns=2, keep_budget=200,
                                             hard_cap=1000, summary_max_tokens=500))
    for i in range(5):
        events = list(run_turn(deps, INFO, MessageIn(text=f"问{i}" + "字" * 30)))
        assert events[-1].type == "done"
    assert any(e.type == "compaction" for e in store.entries(INFO.session_id))
    compactions = [m for p, m in routed if p == COMPACT_PURPOSE]
    turns = [m for p, m in routed if p == "assistant.turn"]
    assert compactions and set(compactions) == {"m-good"}
    assert len(turns) == 5 and set(turns) == {"m-fast"}


def test_assistant_thinking_param(tmp_path):
    pack = HarnessPack(1, {"H10.assistant.turn.thinking": False})
    prov, _, _, _, _ = assistant_run(tmp_path, pack, [ChatTurn(text="答")])
    params = prov.chat_calls[0].params
    assert params[THINKING_PARAM] is False and "message_id" in params


# ---- (e) H9 before_answer ----

def test_assistant_delegate_notes_reach_the_turn(tmp_path):
    pack = HarnessPack(1, {"H9.assistant.before_answer": [delegate()]})
    sink = Recorder()
    script = [ChatTurn(tool_calls=(ToolCall(id="h1", name="read_k", args={}),)),
              ChatTurn(text="他已经会均值"),
              ChatTurn(text="答")]
    prov, models, events, store, ts = assistant_run(tmp_path, pack, script, trace=sink)
    assert events[-1].type == "done" and events[-1].llm_calls == 3
    assert [r.purpose for r in prov.chat_calls] == [
        "hook.assistant.before_answer", "hook.assistant.before_answer", "assistant.turn"]
    assert models == ["m-fast", "m-fast", "m-good"]     # the step's tier, then the turn's
    turn_req = prov.chat_calls[2]
    last = turn_req.messages[-1].text
    assert "\n\n" + NOTES_HEAD + "\n他已经会均值\n\n他问：" in last    # the notes, then the question
    hook_req = prov.chat_calls[0]
    assert hook_req.messages[0].text.startswith("先读他的 K\n\n")
    brief = hook_req.messages[0].text[len("先读他的 K\n\n"):]       # the turn's message before the notes
    assert last == brief[brief.index("当前页：《"):].replace("\n\n他问：", "\n\n" + NOTES_HEAD + "\n他已经会均值\n\n他问：")
    assert ts[0].calls == [{}]                           # the hook's read ran
    entries = [e.type for e in store.entries(INFO.session_id)]
    assert "tool" not in entries                        # internal: no tool entry
    assert not any(e.type in ("tool_start", "tool_end", "tool") for e in events)
    starts = [c for c in sink.calls if c[0] == "start"]
    assert [c[1] for c in starts] == ["assistant:s1:msg_001:hook:0", "assistant:s1:msg_001"]
    assert starts[0][3] == "hook.assistant.before_answer"
    hook_tools = [c for c in sink.calls if c[0] == "tool"]
    assert [(c[1], c[4]) for c in hook_tools] == [("assistant:s1:msg_001:hook:0", "read_k")]


def test_assistant_hook_routes_by_its_own_tier_not_the_turns_pin(tmp_path):
    pack = HarnessPack(1, {"H10.assistant.turn.tier": "fast",
                           "H9.assistant.before_answer": [delegate(tier="good")]})
    script = [ChatTurn(text="他已经会均值"), ChatTurn(text="答")]
    prov, models, events, _, _ = assistant_run(tmp_path, pack, script)
    assert events[-1].type == "done"
    assert [r.purpose for r in prov.chat_calls] == ["hook.assistant.before_answer",
                                                    "assistant.turn"]
    assert models == ["m-good", "m-fast"]


def test_assistant_hook_naming_a_tool_turned_off_fails_before_any_call(tmp_path):
    pack = HarnessPack(1, {"H9.assistant.before_answer": [delegate()],
                           "H3.assistant.read_k": False})
    prov, _, events, store, _ = assistant_run(tmp_path, pack, [])
    assert [e.type for e in events] == ["error"] and "read_k" in events[0].detail
    assert prov.chat_calls == []
    assert store.entries(INFO.session_id) == []             # nothing of the turn is logged


# ---- (f) a hook failure ----

def test_assistant_hook_failure_is_the_turns_error(tmp_path):
    pack = HarnessPack(1, {"H9.assistant.before_answer": [delegate()]})
    prov, _, events, store, _ = assistant_run(
        tmp_path, pack, [ProviderUnavailableError("down")])
    assert events[-1].type == "error" and events[-1].code == "llm_unavailable"
    assert not any(e.type in ("text", "done") for e in events)
    assert [r.purpose for r in prov.chat_calls] == ["hook.assistant.before_answer"]
    entries = [e.type for e in store.entries(INFO.session_id)]
    assert "assistant" not in entries and "turn_error" in entries
