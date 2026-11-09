"""Memory recall × RSI (plan Task 15 step 2, spec §3.5): what a turn recalled is logged so a
replay can be checked against it, `search(as_of=T)` sees memory as it stood at T, the harness
pack's H5 never sizes the recall block, and a DBOS replay of a turn reuses the recorded recall
instead of searching again."""

import hashlib
import threading
from datetime import timedelta
from itertools import count

import pytest

from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.service import AssistantService
from evowiki.assistant.session import MemorySessionStore
from evowiki.goals.context import estimate_tokens
from evowiki.harness.keys import KEYS
from evowiki.harness.pack import EMPTY, HarnessPack
from evowiki.llm.chat import ChatTurn, memory_fenced
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.memory import recall as recall_mod
from evowiki.memory.contracts import EvidenceRef
from evowiki.memory.ports import MemoryPorts
from evowiki.memory.recall import RECALL_BUDGET, RECALL_MAX_ITEMS, RETRIEVAL_VERSION
from evowiki.memory.search import episode_text, search
from evowiki.memory.store import Episode, InMemoryMemoryStore, Item
from evowiki.retrieval.embed import HashingEmbedder
from tests.assistant.fakes import INFO, T0, Data, Ids, tools

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
E = HashingEmbedder()
MSG = MessageIn(text="atomic 怎么定")
_n = count(1)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def put_decision(mem, content, *, at=T0 - timedelta(days=1), created=None, sid="s0"):
    iid = f"i{next(_n)}"
    mem.put_item(Item(item_id=iid, user_id="u1", kind="decision", content=content,
                      provenance="explicit_history",
                      evidence=(EvidenceRef(message_id="m0", session_id=sid, role="user",
                                            quote="可以，就这样"),),
                      scope_type="global", session_id=sid, valid_from=at,
                      created_at=created or at, status="active"))
    mem.set_item_vector(iid, E.embed_documents([content])[0], E)
    return iid


def put_episode(mem, user_text, *, at=T0 - timedelta(days=1), sid="s0"):
    mid = f"m{next(_n)}"
    ep = Episode(user_id="u1", session_id=sid, message_id=mid, answer_message_id=f"a{mid}",
                 trace_run_id=f"assistant:{sid}:{mid}", turn_index=0, page_id="p1", at=at,
                 user_text=user_text, answer_text="好的")
    mem.upsert_episode(ep)
    return ep


def turn(tmp_path, mem, *, pack=EMPTY, log="c.jsonl"):
    """One plain turn on MSG; (the assistant.turn request, the session log)."""
    store = MemorySessionStore()
    prov = ScriptedChatProvider("scripted", lambda m, r: ChatTurn(text="答"))
    gw = Gateway(CFG, CallLog(tmp_path / log), mode="record", providers={"scripted": prov},
                 harness=lambda uid: pack)
    deps = TurnDeps(gateway=gw, tools=tools(), store=store, data=Data(),
                    new_id=Ids(), clock=lambda: T0,
                    memory=MemoryPorts(store=mem, embedder=E, sessions=store))
    store.create(INFO)
    list(run_turn(deps, INFO, MSG))
    req = next(r for r in prov.chat_calls if r.purpose == "assistant.turn")
    return req, store.entries("s1")


def recall_of(req):
    return [m.text for m in req.messages if m.origin == "memory"]


# ---- what was recalled is on the answer ----

def test_every_recalled_row_carries_its_content_hash(tmp_path):
    mem = InMemoryMemoryStore()
    d = "atomic 由知识本身决定"
    iid = put_decision(mem, d)
    ep = put_episode(mem, "atomic 的粒度怎么选")
    _, entries = turn(tmp_path, mem)
    asst = [e.payload for e in entries if e.type == "assistant"][-1]
    assert asst["retrieval_version"] == RETRIEVAL_VERSION
    got = {r["id"]: r for r in asst["recalled"]}
    assert set(got) == {f"item:{iid}", f"ep:{ep.key}"}
    assert all(set(r) == {"id", "kind", "content_hash"} for r in got.values())
    assert got[f"item:{iid}"]["content_hash"] == sha(d)
    assert got[f"ep:{ep.key}"]["content_hash"] == sha(episode_text(ep))
    assert got[f"ep:{ep.key}"]["kind"] == "episode"


# ---- as_of ----

@pytest.mark.parametrize("mode", ["auto", "history"])
def test_search_as_of_sees_no_memory_written_after_it(mode):
    mem = InMemoryMemoryStore()
    old = put_decision(mem, "atomic 由知识本身决定")
    # about an earlier turn, but consolidated only after T: not there at T
    late = put_decision(mem, "atomic 放在 ConceptName 上", at=T0 - timedelta(days=1),
                        created=T0 + timedelta(hours=1))
    put_episode(mem, "atomic 粒度再想想", at=T0 + timedelta(hours=2))
    # threshold -1: every row passes the vector gate, so only as_of can leave one out
    at_t = search(mem, E, "u1", "atomic", mode=mode, as_of=T0, threshold=-1.0)
    assert [h.id for h in at_t] == [f"item:{old}"]
    now = {h.id for h in search(mem, E, "u1", "atomic", mode=mode, threshold=-1.0)}
    assert {f"item:{old}", f"item:{late}"} <= now and len(now) == 3


# ---- H5 never sizes the recall block ----

def _h5_packs():
    h5 = [k for k in KEYS if k.dim == "H5"]
    assert {k.name for k in h5} >= {"H5.assistant.context_budget", "H5.assistant.top_k_pages"}
    return [EMPTY, HarnessPack(1, {k.name: k.lo for k in h5}),
            HarnessPack(2, {k.name: k.hi for k in h5})]


def test_recall_block_is_the_same_under_any_h5(tmp_path):
    mem = InMemoryMemoryStore()
    for i in range(RECALL_MAX_ITEMS + 3):
        put_decision(mem, f"atomic 第{i}条决定：" + "按知识本身的结构划分粒度，" * 12,
                     at=T0 - timedelta(days=1, minutes=i))
    seen = []
    for j, pack in enumerate(_h5_packs()):
        req, entries = turn(tmp_path, mem, pack=pack, log=f"c{j}.jsonl")
        asst = [e.payload for e in entries if e.type == "assistant"][-1]
        seen.append((recall_of(req), asst["recalled"]))
    assert all(s == seen[0] for s in seen[1:])
    texts, recalled = seen[0]
    assert len(texts) == 1 and 0 < len(recalled) <= RECALL_MAX_ITEMS
    assert estimate_tokens(memory_fenced(texts[0])) <= RECALL_BUDGET


# ---- DBOS replay reuses the recorded recall ----

@pytest.fixture
def dbos_memory_turn(db_url, tmp_path):
    from evowiki.agent import dbos_app
    from evowiki.assistant import turn_runs
    from evowiki.assistant.turn_runs import DbosTurnRuns

    class Applier:
        def apply(self, user_id, draft):
            return ("s9",)

    def make(mem, prov):
        gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                     providers={"scripted": prov})
        store = MemorySessionStore()
        store.create(INFO)
        deps = TurnDeps(gateway=gw, tools=tools(), store=store, data=Data(),
                        new_id=Ids(), clock=lambda: T0,
                        memory=MemoryPorts(store=mem, embedder=E, sessions=store))
        svc = AssistantService(deps, Applier(), DbosTurnRuns())
        turn_runs.install(svc)
        return svc

    dbos_app.launch(db_url, app_version="t")
    yield make
    dbos_app.shutdown()
    turn_runs.uninstall()


def test_dbos_replay_recalls_what_the_first_run_recalled(dbos_memory_turn, monkeypatch):
    from dbos import DBOS

    from tests.assistant.test_turn_runs import wait_until, workflow_id

    mem = InMemoryMemoryStore()
    put_decision(mem, "atomic 由知识本身决定")
    lock = threading.Lock()
    requests = []

    def answer(m, r):
        with lock:
            requests.append(r)
        return ChatTurn(text="答")

    searched = []
    real = recall_mod.recall_block
    monkeypatch.setattr(recall_mod, "recall_block",
                        lambda *a, **kw: searched.append(1) or real(*a, **kw))
    svc = dbos_memory_turn(mem, ScriptedChatProvider("scripted", answer))
    assert [e.type for e in svc.send("u1", "s1", MSG)][-1] == "done"
    wid = workflow_id(svc)
    wait_until(lambda: DBOS.retrieve_workflow(wid).get_status().status == "SUCCESS")
    first = [recall_of(r) for r in requests if r.purpose == "assistant.turn"]
    assert first and first[0] and searched == [1]

    # a new memory that this question would recall now
    put_decision(mem, "atomic 后来改成放在 ConceptName 上", at=T0)
    fresh = svc.turn_deps.memory.recall(info=INFO, query=MSG.text, concept_ids=["z_score"],
                                        state=_empty_state(), now=T0)
    assert "ConceptName" in fresh.message.text
    searched.clear()

    steps = DBOS.list_workflow_steps(wid)
    names = [s["function_name"] for s in steps]
    rec = names.index("assistant.memory.recall")
    model = names.index("llm.converse:assistant.turn")
    assert rec < model
    # replay from the model call: the recall step answers from its record, so the request is
    # the recorded one and the call log serves it (the provider is not asked again)
    before = len(requests)
    forked = DBOS.fork_workflow(wid, steps[model]["function_id"])
    assert forked.get_result() == "done"
    assert requests[before:] == [] and searched == [], "the recall step ran again"
    # control: replaying from the recall step itself searches again, and the changed recall
    # makes a new request (so the call log above could not have hidden a changed one)
    assert DBOS.fork_workflow(wid, steps[rec]["function_id"]).get_result() == "done"
    again = [recall_of(r) for r in requests[before:] if r.purpose == "assistant.turn"]
    assert searched == [1] and again and again[0] != first[0]
    assert "ConceptName" in again[0][0] and "ConceptName" not in first[0][0]


def _empty_state():
    from evowiki.assistant.session import fold
    return fold(INFO, [])
