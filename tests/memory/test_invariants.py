"""Memory-layer invariants (plan Task 15 steps 1-2, spec §2.4 / §3.1): the layer never writes K,
and a recall block never reaches the learner's words - neither the session log's user entry, nor
`ctx.user_words`, nor what consolidation reads.

Task 8 (tests/assistant/test_loop_memory.py) already checks the user entry and `ctx.user_words`
of the recalling turn; this file adds the import ban, K's sequence numbers around a recall plus a
consolidation, and the consolidation / summary inputs of a session whose turn recalled."""

import ast
import json
import re
from pathlib import Path

from evowiki.assistant.compact import COMPACT_PURPOSE
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore, SessionInfo, fold
from evowiki.assistant.wiki import WikiData
from evowiki.assistant.wrapup import InlineWrapups
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider, ScriptedProvider
from evowiki.memory.consolidate import CONSOLIDATE_PURPOSE
from evowiki.memory.contracts import EvidenceRef
from evowiki.memory.ports import MemoryPorts
from evowiki.memory.store import InMemoryMemoryStore, Item
from evowiki.retrieval.embed import HashingEmbedder
from tests.assistant.fakes import T0, Data, Ids, tools
from tests.assistant.test_wiki_adapter import env, wiki  # noqa: F401 - fixtures
from tests.wikiapp.test_goal import NOW

MEMORY = Path(__file__).resolve().parents[2] / "src" / "evowiki" / "memory"
REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
E = HashingEmbedder()

# Modules whose job is to write K (the aspect event log), checked against the code: the K-write
# helpers of the wiki flows, the aspect-event append, the validated K append, the deterministic
# evidence update, and the assistant code that commits question / quiz evidence. (The plan's
# `goals.probe.record_evidence` does not exist: goals/probe.py is pure; `record_evidence` is only
# a tool name listed in assistant/skills.py, with no implementation module yet.)
K_WRITERS = (
    "evowiki.wikiapp.kwrite",
    "evowiki.store.aspects",
    "evowiki.runtime.aspect_beliefs",
    "evowiki.belief.update",
    "evowiki.assistant.wiki",
    "evowiki.assistant.tools.evidence",
    "evowiki.goals.observe",
)
# K's event types (constructing one is the first step of any K write) and the repo's commit
# entry points: memory reaches the wiki only through the lander port (the teaching spec).
BANNED_NAMES = {
    "evowiki.belief.aspects": {"DefineConcept", "Observe", "RevokeInference", "Declare",
                               "AddNote", "RetireNote", "Claim", "ResolveClaim", "AspectEvent",
                               "AspectEventAdapter"},
    "evowiki.wikiapp.repo": {"Commit", "commit_signal"},
}


def _imports(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    """(module, imported names) of every import in `path`, in function bodies and
    TYPE_CHECKING blocks too; a relative import is resolved against evowiki.memory."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [(a.name, ()) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                mod = ".".join(["evowiki", "memory"][: 3 - node.level] + ([mod] if mod else []))
            out.append((mod, tuple(a.name for a in node.names)))
    return out


def _writer(name: str) -> bool:
    return any(name == w or name.startswith(w + ".") for w in K_WRITERS)


def _banned(mod: str, names: tuple[str, ...]) -> list[str]:
    if _writer(mod):
        return [mod]
    return [f"{mod}.{n}" for n in names
            if _writer(f"{mod}.{n}") or n in BANNED_NAMES.get(mod, ())]


def test_memory_imports_no_k_writer():
    files = sorted(MEMORY.rglob("*.py"))
    assert {f.name for f in files} >= {"recall.py", "consolidate.py", "ports.py", "store.py"}
    found = {f.name: bad for f in files
             for bad in [[b for mod, names in _imports(f) for b in _banned(mod, names)]] if bad}
    assert found == {}


def test_the_import_check_catches_each_form(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("import evowiki.store.aspects\n"
                 "from evowiki.wikiapp import kwrite\n"
                 "from evowiki.belief.aspects import Observe, AspectConfig\n"
                 "def f():\n    from evowiki.wikiapp.repo import Commit, Repo\n",
                 encoding="utf-8")
    bad = [b for mod, names in _imports(p) for b in _banned(mod, names)]
    assert bad == ["evowiki.store.aspects", "evowiki.wikiapp.kwrite",
                   "evowiki.belief.aspects.Observe", "evowiki.wikiapp.repo.Commit"]


# ---- K's sequence numbers around a recall and a consolidation ----

def _session(store, info, turns):
    store.create(info)
    for i, (m, u, a, ans) in enumerate(turns):
        store.append(info.session_id, f"{info.session_id}-u{i}", "user",
                     {"message_id": m, "text": u, "selection": None}, NOW)
        store.append(info.session_id, f"{info.session_id}-a{i}", "assistant",
                     {"message_id": a, "text": ans, "sources": []}, NOW)


def test_recall_and_consolidation_leave_k_alone(wiki, tmp_path):  # noqa: F811
    repo, pid, _ = wiki
    sessions = MemorySessionStore()
    s1 = SessionInfo(session_id="s1", user_id="u1", page_id=pid, note_concept_id=None,
                     created_at=NOW)
    _session(sessions, s1, [("m1", "atomic 应该怎么定？", "a1", "建议 atomic 由知识本身决定。"),
                            ("m2", "可以，就这样", "a2", "好，就这么定。")])
    mem = InMemoryMemoryStore()
    mem.put_item(Item(item_id="q1", user_id="u1", kind="open_question",
                      content="z 分数为什么除以标准差", provenance="explicit_history",
                      evidence=(EvidenceRef(message_id="m0", session_id="s0", role="user",
                                            quote="为什么除以标准差"),),
                      scope_type="global", concept_ids=("c1",), session_id="s0",
                      valid_from=NOW, created_at=NOW, status="active"))
    k_reads: list[tuple[str, ...]] = []
    data = WikiData(repo, lambda: NOW)

    def known(uid, concepts):           # the service's reader (deps.py), counted
        k_reads.append(tuple(concepts))
        return data.known_concepts(uid, concepts)

    ports = MemoryPorts(store=mem, embedder=E, sessions=sessions, known_concepts=known,
                        clock=lambda: NOW)
    reply = {"items": [{"kind": "decision", "content": "atomic 由知识本身决定",
                        "scope_type": "global",
                        "evidence": [{"message_id": "a1", "role": "assistant"},
                                     {"message_id": "m2", "role": "user",
                                      "quote": "可以，就这样"}]}]}
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={
        "scripted": ScriptedProvider("scripted", lambda m, r: json.dumps(reply))})

    before = repo.load("u1")
    log_before = list(repo.aspect_log("u1"))
    s2 = s1.model_copy(update={"session_id": "s2"})
    sessions.create(s2)
    r = ports.recall(info=s2, query="z 分数为什么除以标准差", concept_ids=["c1"],
                     state=fold(s2, sessions.entries("s2")), now=NOW)
    calls = ports.consolidate(fold(s1, sessions.entries("s1")), gateway=gw, account=None,
                              first=0, through=2)
    after = repo.load("u1")

    assert r.message is not None and k_reads == [("c1",)]      # K was read, for the gate
    assert calls and {i.kind for i in mem.items("u1")} == {"open_question", "decision"}
    assert (after.aspect_seq, after.wiki_seq) == (before.aspect_seq, before.wiki_seq)
    assert repo.aspect_log("u1") == log_before


# ---- a recall block never becomes his words ----

MARK = "知识本身决定粒度"


def test_recall_never_reaches_consolidation_or_the_summary(tmp_path):
    """Day 2 recalls a stored decision (MARK); day 3's first turn asks for day 2's wrap-up (summary +
    consolidation, assistant/wrapup.py: the run's own body, in the caller here instead of on the
    queue). Both read the session log, and the recall block is not in it."""
    summary = '{"handoff": {"summary": "问了 atomic"}, "learning": {}}'
    seen: dict[str, list] = {"turn": [], "summary": [], "consolidate": []}
    prompts: list[str] = []

    def chat(m, r):
        assert r.purpose != COMPACT_PURPOSE
        seen["turn"].append(r)
        if not any(x.role == "tool" for x in r.messages):   # read_k first: its ctx is checked
            return ChatTurn(tool_calls=(ToolCall(id=f"peek{len(seen['turn'])}", name="read_k",
                                                 args={}),))
        return ChatTurn(text="好的，继续。" + "字" * 200)

    def complete(m, r):
        if r.purpose == COMPACT_PURPOSE:
            seen["summary"].append(r.model_dump_json())
            return summary
        assert r.purpose == CONSOLIDATE_PURPOSE
        seen["consolidate"].append(r.model_dump_json())
        prompts.append(r.prompt)
        return json.dumps({"items": []})

    store = MemorySessionStore()
    prov = ScriptedChatProvider("scripted", chat, complete)
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})
    mem = InMemoryMemoryStore()
    mem.put_item(Item(item_id="i1", user_id="u1", kind="decision",
                      content=f"atomic 由{MARK}", provenance="explicit_history",
                      evidence=(EvidenceRef(message_id="m0", session_id="s0", role="user",
                                            quote="可以，就这样"),),
                      scope_type="global", session_id="s0", valid_from=T0, created_at=T0,
                      status="active"))
    built = tools()
    deps = TurnDeps(gateway=gw, tools=built, store=store, data=Data(),
                    new_id=Ids(), clock=lambda: T0,
                    memory=MemoryPorts(store=mem, embedder=E, sessions=store))
    deps.wrapups = InlineWrapups(deps)
    base = SessionInfo(session_id="s2", user_id="u1", page_id="p1", note_concept_id=None,
                       created_at=T0)
    store.create(base)
    list(run_turn(deps, base, MessageIn(text="atomic 怎么定")))
    list(run_turn(deps, base, MessageIn(text="那就照这个来")))
    day3 = base.model_copy(update={"session_id": "s3"})
    store.create(day3)
    list(run_turn(deps, day3, MessageIn(text="今天讲点别的")))

    recalled = [m for r in seen["turn"] for m in r.messages if m.origin == "memory"]
    assert recalled and MARK in recalled[0].text                # day 2 did recall it
    assert not seen["summary"] and seen["consolidate"]  # no separate closing summary
    assert all(MARK not in s for s in seen["summary"])
    # consolidation lists the stored decisions in a section of their own (to supersede /
    # resolve them); the dialogue it extracts from is the log's, without the recall block
    assert all("历史资料" not in s and "(item:i1)" not in s for s in seen["consolidate"])
    dialogue = prompts[0].split("这次要整理的对话：", 1)[1]
    assert MARK not in dialogue
    for sid in ("s2", "s3"):
        users = [e.payload for e in store.entries(sid) if e.type == "user"]
        assert users and all(MARK not in json.dumps(p, ensure_ascii=False) for p in users)
    seen_words = [tuple(c.user_words) for t in built if t.name == "read_k" for c in t.ctxs]
    assert seen_words and all(MARK not in w for ws in seen_words for w in ws)
    # the consolidation prompt quotes the session's own lines only
    said = [t for prompt in prompts for role, t in re.findall(r"\[\S+\] (用户|助手)：(.*)", prompt) if role == "用户"]
    assert "atomic 怎么定" in said and "那就照这个来" in said
    assert all(MARK not in text for text in said)
