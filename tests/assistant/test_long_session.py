"""B1d acceptance (§6.4): a 50-turn conversation stays within budget, tool calls are never split
from their results, and a failing summary shows the omitted-turns notice."""
from evowiki.agent.compact import tool_pairs_intact
from evowiki.agent.tools import ToolResult
from evowiki.assistant.compact import COMPACT_PURPOSE, DEFAULT_POLICY, history_tokens, turn_tokens
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore, fold, session_out
from evowiki.goals.context import estimate_tokens
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.providers import ProviderRefusalError
from evowiki.llm.scripted import ScriptedChatProvider
from tests.assistant.fakes import INFO, T0, Data, FakeTool, Ids

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
GOOD = ('{"handoff": {"summary": "前面的问题与解释"}, '
        '"learning": {"unclear": [{"concept": "负的 z", "evidence_id": "m1"}]}}')
ANSWER = "答" + "解释" * 600          # ~1200 tokens: compaction starts after ~16 turns


def _run(tmp_path, summary_reply, n=50):
    def reply(model, req):
        assert req.purpose != COMPACT_PURPOSE
        last = req.messages[-1]
        # every 3rd question reads the page once (a long result), then answers
        asked = last.text.rsplit("他问：\n", 1)[-1]      # assistant@v29: the question is last
        if last.role == "user" and int(asked[1:].split("：")[0]) % 3 == 0:
            return ChatTurn(tool_calls=(ToolCall(id=f"r{len(req.messages)}", name="read_page",
                                                 args={}),))
        return ChatTurn(text=ANSWER)
    def summarise(model, req):
        assert req.purpose == COMPACT_PURPOSE
        if isinstance(summary_reply, Exception):
            raise summary_reply
        return summary_reply

    prov = ScriptedChatProvider("scripted", reply, summarise)
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})
    store = MemorySessionStore()
    store.create(INFO)
    deps = TurnDeps(gateway=gw, tools=[FakeTool("read_page", "read",
                                                ToolResult(content="页" * 3000))],
                    store=store, data=Data(), new_id=Ids(),
                    clock=lambda: T0)
    for i in range(n):
        ev = list(run_turn(deps, INFO, MessageIn(text=f"问{i}：" + "为什么" * 50)))
        assert ev[-1].type == "done", ev[-1]
    return prov, store


def _history_tokens_sent(req):
    # everything before the current question: summary (in system) is checked via the store;
    # here the verbatim history messages
    return sum(estimate_tokens(m.text) for m in req.messages[:-1] if m.role != "tool")


def test_fifty_turns_stay_within_budget_and_never_split_tool_calls(tmp_path):
    prov, store = _run(tmp_path, GOOD)
    turns = [r for r in prov.chat_calls if r.purpose == "assistant.turn"]
    assert len([r for r in prov.calls if r.purpose == COMPACT_PURPOSE]) >= 2
    for r in turns:
        assert tool_pairs_intact(r.messages)
        assert _history_tokens_sent(r) <= DEFAULT_POLICY.hard_cap
    st = fold(INFO, store.entries("s1"))
    assert history_tokens(st) <= DEFAULT_POLICY.hard_cap
    assert not any(e.payload.get("omitted") for e in store.entries("s1")
                   if e.type == "compaction")
    # maintain leaves at most history_budget tokens before the new turn is appended, so at most
    # history_budget // one-turn-size turns stay, plus the turn just answered
    live = len(st.turns) - st.first_kept
    assert len(st.turns) == 50 and live <= DEFAULT_POLICY.history_budget // turn_tokens(st.turns[0]) + 1
    # every 3rd question (0, 3, ..., 48 = 17 of them) read the page, and the result reached the model
    assert len([r for r in turns if any(m.role == "tool" for m in r.messages)]) == 17


def test_failing_summaries_drop_oldest_turns_with_a_visible_notice(tmp_path):
    prov, store = _run(tmp_path, "坏摘要")
    for r in (r for r in prov.chat_calls if r.purpose == "assistant.turn"):
        assert _history_tokens_sent(r) <= DEFAULT_POLICY.hard_cap
    out = session_out(fold(INFO, store.entries("s1")))
    notices = [m.text for m in out.messages if m.message_id.startswith("omit_")]
    assert notices and all(t.endswith("轮对话已省略（摘要生成失败）") for t in notices)
    assert not any(e.type == "compaction" and not e.payload.get("omitted")
                   for e in store.entries("s1"))
    # the notice is written before the question of the turn that dropped the turns (ruling 3)
    msgs = out.messages
    for i, m in enumerate(msgs):
        if m.message_id.startswith("omit_"):
            assert m.role == "assistant" and msgs[i + 1].role == "user"
    entries = store.entries("s1")
    for i, e in enumerate(entries):
        if e.type == "compaction" and e.payload.get("omitted"):
            assert entries[i + 1].type == "user"


def test_refused_summaries_still_let_the_turn_answer_with_the_omitted_notice(tmp_path):
    prov, store = _run(tmp_path, ProviderRefusalError("refused"))   # every turn is "done"
    for r in (r for r in prov.chat_calls if r.purpose == "assistant.turn"):
        assert _history_tokens_sent(r) <= DEFAULT_POLICY.hard_cap
    out = session_out(fold(INFO, store.entries("s1")))
    notices = [m.text for m in out.messages if m.message_id.startswith("omit_")]
    assert notices and all(t.endswith("轮对话已省略（摘要生成失败）") for t in notices)
    assert not any(e.type == "compaction" and not e.payload.get("omitted")
                   for e in store.entries("s1"))
