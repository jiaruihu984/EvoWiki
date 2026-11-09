"""Phase 0 of the cross-session memory plan (the product specification
Task 1): what the learner sees forgotten today, written as the behaviour the memory layer must
give. Each test drives the real assistant loop with a scripted model and is a strict xfail until
the task that builds it removes the mark (Task 8: recall; Task 9: tools; Task 15: all)."""

from datetime import timedelta

from evowiki.agent.compact import CompactPolicy
from evowiki.assistant.compact import COMPACT_PURPOSE
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.llm.chat import ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.memory.ports import MemoryPorts
from evowiki.memory.store import InMemoryMemoryStore
from evowiki.retrieval.embed import HashingEmbedder
from tests.assistant.fakes import T0, Data, Ids, tools

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
DAY1 = SessionInfo(session_id="s1", user_id="u1", page_id="p1", note_concept_id=None,
                   created_at=T0)
DAY2 = SessionInfo(session_id="s2", user_id="u1", page_id="p2", note_concept_id=None,
                   created_at=T0 + timedelta(days=1))
SUMMARY = '{"handoff": {"summary": "前面几轮"}, "learning": {}}'


def _memory_messages(request):
    return [m for m in request.messages if getattr(m, "origin", "user") == "memory"]


def _deps(tmp_path, answer, store, *, clock, compaction=None):
    def summarise(m, r):
        assert r.purpose == COMPACT_PURPOSE
        return SUMMARY

    prov = ScriptedChatProvider("scripted", answer, summarise)
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})
    kw = {"compaction": compaction} if compaction is not None else {}
    memory = MemoryPorts(store=InMemoryMemoryStore(), embedder=HashingEmbedder(), sessions=store)
    return TurnDeps(gateway=gw, tools=tools(), store=store, data=Data(),
                    new_id=Ids(), clock=clock, memory=memory, **kw), prov


def _turns(prov):
    return [r for r in prov.chat_calls if r.purpose == "assistant.turn"]


def test_yesterdays_decision_is_recalled_on_another_page(tmp_path):
    """Day 1 on p1: the assistant proposes, the learner confirms. Day 2 on p2 he asks about it."""
    replies = iter(["建议 atomic 由知识本身决定，不按层级强制。", "好，就这么定。",
                    "上次定的是：atomic 由知识本身决定。"])
    store = MemorySessionStore()
    now = {"t": T0}
    deps, prov = _deps(tmp_path, lambda m, r: ChatTurn(text=next(replies)), store,
        clock=lambda: now["t"])
    store.create(DAY1)
    list(run_turn(deps, DAY1, MessageIn(text="atomic 应该怎么定？")))
    list(run_turn(deps, DAY1, MessageIn(text="可以，就这样。")))
    now["t"] = T0 + timedelta(days=1)
    store.create(DAY2)
    list(run_turn(deps, DAY2, MessageIn(text="上次我们怎么定 atomic 的？")))

    request = _turns(prov)[-1]
    recalled = _memory_messages(request)
    assert recalled, "the turn carries a separate memory context message"
    assert "atomic 由知识本身决定" in recalled[0].text
    assert "2026-10-08" in recalled[0].text                          # dated
    assert "由知识本身决定" not in request.messages[-1].text          # never merged into his words


def test_search_memory_finds_the_quote_and_read_past_turn_reads_it_whole(tmp_path):
    from evowiki.agent.tools import ToolContext
    from evowiki.assistant.tools.memory import ReadPastTurnTool, SearchMemoryTool  # Task 9

    store = MemorySessionStore()
    deps, _ = _deps(tmp_path, lambda m, r: ChatTurn(text="好的，换一种讲法。"), store,
                    clock=lambda: T0)
    store.create(DAY1)
    list(run_turn(deps, DAY1, MessageIn(text="我不想要那种讲法，太绕了。")))

    ctx = ToolContext(user_id="u1", session_id="s2")
    hits = SearchMemoryTool(deps.memory).run({"query": "那种讲法", "since": "2026-10-08"}, ctx)
    assert "不想要那种讲法" in hits.content
    turn = ReadPastTurnTool(deps.memory).run({"session_id": "s1",
                                              "message_id": hits.data["hits"][0]["message_id"]},
                                             ctx)
    assert "用户：我不想要那种讲法，太绕了。" in turn.content and "助手：" in turn.content


def test_folded_turn_of_the_same_session_is_recalled(tmp_path):
    """A long session: the decision of an early turn is folded by compaction, then asked about."""
    def answer(m, r):
        return ChatTurn(text="答" + "字" * 30)

    store = MemorySessionStore()
    deps, prov = _deps(tmp_path, answer, store, clock=lambda: T0,
                       compaction=CompactPolicy(history_budget=200, keep_turns=2, keep_budget=200,
                                                hard_cap=1000, summary_max_tokens=500))
    store.create(DAY1)
    list(run_turn(deps, DAY1, MessageIn(text="定一下：第二轮的决定是用具体数字讲方差" + "字" * 20)))
    for i in range(5):
        list(run_turn(deps, DAY1, MessageIn(text=f"问{i}" + "字" * 30)))
    assert any(e.type == "compaction" for e in store.entries("s1"))
    list(run_turn(deps, DAY1, MessageIn(text="最开始定的讲方差的办法是什么？")))

    recalled = _memory_messages(_turns(prov)[-1])
    assert recalled and "用具体数字讲方差" in recalled[0].text
