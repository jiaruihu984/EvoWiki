"""Coding-agent RSI phase 2 end to end with fakes (plan P2 T12), through the real wiring: the page
writer (`PageWriter` + `forge_opener`) and the assistant (`build_assistant(forge=...)`, HTTP and a
durable turn). Scripted models and coder, in-process sandbox; one real-WASI smoke at the end
(skipped without the runtime).

Story 1: three traced page writes reading pages one by one → `run_round` (scripted coder, fake
gate) promotes, opts in and embeds 「一次读多页」 → the next page write offers it first-class and
calls it (the host port reads two pages) → kill switch → change list row (cost in `why`) → undo →
the next write offers nothing. A round without a gate (`NoGate`) leaves the candidate `built`.

Story 2: an assistant turn calls `request_tool` then `call_tool`; the user sees no internals; the
candidate is `built` with origin `session:<sid>`; the session's next turn finds and calls it,
another session cannot; a turn that crashes right after the recorded `forge.request_tool` step
recovers with the overlay rebuilt from the recording (no second build)."""

import json
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from evowiki.agent import durable
from evowiki.agent.tools import ToolContext
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.durable import durable_turn_deps
from evowiki.assistant.loop import run_turn
from evowiki.forge import change_source as cs
from evowiki.forge import optin, switch
from evowiki.forge.models import ForgeDisabled
from evowiki.forge.round import NoGate, run_round
from evowiki.forge.sandbox import runtime as wasi_runtime
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import Account
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.service.app import create_app
from evowiki.service.deps import build_assistant
from evowiki.sources.config import DATA_BEGIN, DATA_END
from evowiki.store import forge as store
from evowiki.store.db import fetch_all
from evowiki.teach.change_source import source_for
from evowiki.wikiapp.repo import MemoryRepo, PgRepo
from tests.deep.fakes import DeepScript, call
from tests.deep.test_writer import outline_input, writer
from tests.forge.fakes import InProcessSandbox
from tests.forge.test_propose import script
from tests.forge.test_round import NOW, FakeGate, coder, traced  # noqa: F401 - fixture
from tests.forge.test_round import deps as round_deps
from tests.forge.test_run import Gw, ReadPage, Recording, request_args, runtime
from tests.forge.test_wiring import tool_names, two_rounds, write_requests
from tests.runtime.helpers import FixedClock
from tests.service.conftest import H, Ids, parse_sse, services, signup  # noqa: F401 - fixture
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids as WikiIds
from tests.wikiapp.test_page import organized

BUILTINS = ("plan_page", "read_k", "read_page", "read_spec", "write_section", "place_component",
            "read_ref", "draw_visual", "write_block", "render_check", "finish")
HIDDEN = ("生成", "沙箱", "模型", "DeepSeek", "deepseek", "forge", "wasi", "WASI")


def write_page(first: Callable[[str], list[dict]], forge) -> tuple[str, DeepScript]:
    """One page write through `PageWriter` (forged tools via `forge_opener`) on a fresh wiki;
    `first(pid)`: the calls the writer makes before writing the sections."""
    env = (MemoryRepo(["u1"]), Script(), WikiIds())
    pid = organized(env)
    ds = DeepScript().plan().write(*two_rounds(*first(pid)))
    w = writer(ds)
    w.forge = forge
    w.write(outline_input(env, pid))
    assert not ds.pending()
    return pid, ds


def offered(ds: DeepScript) -> set[tuple[str, ...]]:
    return {tuple(tool_names(r)) for r in write_requests(ds)}


def tool_defs(reqs) -> set[str]:
    return {json.dumps([t.model_dump() for t in r.tools], ensure_ascii=False) for r in reqs}


# --- story 1: detector → round → first-class in the page writer → change list → undo -------------

def test_page_writer_story(pool, traced, monkeypatch):  # noqa: F811
    emb, gate = HashingEmbedder(dim=256), FakeGate()
    res = run_round("u1", deps=round_deps(pool, coder()), gate=gate, embedder=emb, now=NOW)
    assert res.promoted == ("fc_r1",) and gate.seen == [("fc_r1", "u1")]
    with pool.connection() as conn:
        assert optin.opted_in(conn, "u1", "deep", ["batch_read_page"], {}) == {"batch_read_page"}

    # the next page write offers it first-class and calls it; the host port reads two pages
    sandbox = Recording()
    rt = runtime(pool, sandbox=sandbox)
    pid, ds = write_page(lambda p: [call("batch_read_page", page_ids=[p, p])], rt)
    reqs = write_requests(ds)
    assert offered(ds) == {(*BUILTINS, "batch_read_page", "call_tool")}
    assert len(tool_defs(reqs)) == 1                       # prompt cache: fixed for the whole run
    assert sandbox.calls == [{"page_ids": [pid, pid]}]
    out = json.loads(unfence([m.text for m in reqs[2].messages if m.role == "tool"][1]))  # [0] is plan_page's
    assert len(out["results"]) == 2 and out["results"][0] == out["results"][1]
    assert out["results"][0]                               # the page's outline, read on the host

    # kill switch: the next write is the built-ins only, a round refuses
    with pool.connection() as conn:
        switch.set_enabled(conn, "u1", "tool", False)
    _, off = write_page(lambda p: [], runtime(pool))
    assert offered(off) == {BUILTINS}
    with pytest.raises(ForgeDisabled):
        run_round("u1", deps=round_deps(pool, coder()), gate=gate, embedder=emb, now=NOW)
    with pool.connection() as conn:
        switch.set_enabled(conn, "u1", "tool", True)

    # the change list (track H, behind the plugin flag) and its undo, routed by the id prefix
    monkeypatch.setattr(cs, "PLUGIN_ACCEPTS_H", True)
    sources = cs.forge_change_sources(pool, lambda: NOW)
    [row] = sources[0].list("u1")
    assert row.track == "H" and row.summary == "新增了工具「一次读多页」" and row.can_undo
    assert "花费 $0.04" in row.why and not any(w in row.summary for w in HIDDEN)
    undone = source_for(sources, row.patch_id).undo("u1", row.patch_id, now=NOW)
    assert undone.status == "rolled_back" and not undone.can_undo

    # after the undo the next write no longer offers it (nothing reachable: the built-ins only)
    _, after = write_page(lambda p: [], runtime(pool))
    assert offered(after) == {BUILTINS}


def test_round_without_a_gate_keeps_the_candidate_built(pool, traced):  # noqa: F811
    res = run_round("u1", deps=round_deps(pool, coder()), gate=NoGate(),
                    embedder=HashingEmbedder(dim=256), now=NOW)
    assert res.built == ("fc_r1",) and res.promoted == ()
    with pool.connection() as conn:
        assert store.get_candidate(conn, "fc_r1").status == "built"
        assert store.registry_head(conn, "u1")[1] == {}
    _, ds = write_page(lambda p: [], runtime(pool))
    assert offered(ds) == {BUILTINS}


# --- story 2: the assistant asks for a tool mid-turn -------------------------------------------

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
ASK_REPLY = {"answer": "见上", "observations": []}


def tc(i: int, tool: str, **args) -> ChatTurn:
    return ChatTurn(tool_calls=(ToolCall(id=f"c{i}", name=tool, args=args),))


class Chat:
    """The assistant's scripted model: pops one turn per turn call, records the requests."""

    def __init__(self):
        self.outs: list[ChatTurn] = []
        self.requests = []

    def __call__(self, model, request):
        if request.purpose != "assistant.turn":       # closing an earlier session: no summary
            return ChatTurn(text="")
        self.requests.append(request)
        return self.outs.pop(0)


def tools_of(entries) -> list[tuple[str, bool]]:
    return [(e["payload"]["name"], e["payload"]["ok"]) for e in entries]


def tool_entries(pool, sid):
    with pool.connection() as conn:
        return fetch_all(conn, "select payload from assistant_entry where session_id = %s "
                               "and type = 'tool' order by seq", (sid,))


class Crash(BaseException):
    """The process dies (not an error the turn could handle)."""


def test_assistant_request_tool_story(services, tmp_path, monkeypatch):  # noqa: F811
    client = TestClient(create_app(services), base_url="http://127.0.0.1")
    signup(client, "u1")
    repo, ids = PgRepo(services.pool), Ids()
    pid = organized((repo, Script(), ids))
    pool = services.pool
    with pool.connection() as conn:
        optin.set_local(conn, "u1", "assistant", "request_tool", True, NOW)
        switch.set_enabled(conn, "u1", "tool", True, NOW)
    chat, forge_gw = Chat(), Gw(costs=[0.01, 0.02, 0.03, 0.04])
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={
        "scripted": ScriptedChatProvider(
            "scripted", chat, complete=lambda m, r: json.dumps(ASK_REPLY))})
    rt = runtime(pool, gw=forge_gw, sandbox=InProcessSandbox())
    services.assistant = build_assistant(pool, gw, services.ctx.content, services.embedder,
                                         FixedClock(NOW), ids, forge=rt)

    def session() -> str:
        r = client.post("/assistant/sessions", json={"page_id": pid}, headers=H("u1"))
        assert r.status_code == 200, r.text
        return r.json()["session_id"]

    def ask(sid: str, text: str) -> tuple[list, str]:
        r = client.post(f"/assistant/sessions/{sid}/messages", json={"text": text},
                        headers=H("u1"))
        assert r.status_code == 200, r.text
        return parse_sse(r.text), r.text

    # turn 1: request_tool builds 「翻倍」 into the turn's overlay, call_tool runs it
    s1 = session()
    chat.outs = [tc(1, "request_tool", **request_args()),
                 tc(2, "call_tool", name="double", args={"n": 21}), ChatTurn(text="答案是 42。")]
    evs, raw = ask(s1, "21 翻倍是多少？")
    assert [e for e, _ in evs][-1] == "done" and not chat.outs
    # the turn's spend includes the build (scripted chat calls cost 0)
    assert evs[-1][1]["cost_usd"] == pytest.approx(0.10)
    assert tools_of(tool_entries(pool, s1)) == [("request_tool", True), ("call_tool", True)]
    assert all(a == Account(user_id="u1", group="assistant") for a in forge_gw.accounts)
    builds = len(forge_gw.inner.requests)
    assert builds == 4
    shown = client.get(f"/assistant/sessions/{s1}", headers=H("u1")).json()
    assert shown["messages"][-1]["text"] == "答案是 42。"
    for text in (raw, json.dumps(shown, ensure_ascii=False)):     # what the user can see
        assert not any(w in text for w in HIDDEN), text
    with pool.connection() as conn:
        [c] = store.candidates_by_origin(conn, "u1", f"session:{s1}", statuses=("built",))
        assert c.name == "double" and c.status == "built"
        assert store.registry_head(conn, "u1") == (0, {})          # the registry is untouched
    first_defs = tool_defs(chat.requests[:3])
    assert len(first_defs) == 1                                   # fixed within the turn

    # turn 2, same session: find_tool finds it, call_tool runs it; no new build
    chat.requests.clear()
    chat.outs = [tc(3, "find_tool", query="翻倍"),
                 tc(4, "call_tool", name="double", args={"n": 5}), ChatTurn(text="是 10。")]
    ask(s1, "5 呢？")
    entries = tool_entries(pool, s1)
    assert tools_of(entries)[-2:] == [("find_tool", True), ("call_tool", True)]
    assert json.loads(entries[-1]["payload"]["content"]) == {"n": 10}
    assert '"name":"double"' in entries[-2]["payload"]["content"]
    assert len(forge_gw.inner.requests) == builds
    assert len(tool_defs(chat.requests)) == 1
    assert "find_tool" in [t.name for t in chat.requests[0].tools]

    # another session cannot reach it
    s2 = session()
    chat.requests.clear()
    chat.outs = [tc(5, "call_tool", name="double", args={"n": 1}), ChatTurn(text="不行。")]
    ask(s2, "1 翻倍？")
    assert tools_of(tool_entries(pool, s2)) == [("call_tool", False)]
    assert {"call_tool", "request_tool"} <= {t.name for t in chat.requests[0].tools}

    # a durable turn dies right after the recorded build; the recovered run replays it
    s3 = session()
    deps = durable_turn_deps(services.assistant.turn_deps)
    info = deps.store.info(s3)
    real = durable.step_ran
    log: list[tuple[str, str, object]] = []
    dead = [False]

    def first(name, fn):
        if not durable.in_workflow():
            return real(name, fn)
        if dead[0]:
            raise Crash()
        try:
            out = real(name, fn)
        except Exception as e:
            log.append((name, "raise", e))
            raise
        log.append((name, "ok", out))
        if name == "forge.request_tool":
            dead[0] = True
            raise Crash()
        return out

    chat.outs = [tc(6, "request_tool", **request_args(name="triple", title="三倍",
                                                       purpose="把整数 n 变成三倍")),
                 tc(7, "call_tool", name="triple", args={"n": 4}), ChatTurn(text="是 8。")]
    forge_gw.inner = script()
    monkeypatch.setattr(durable, "step_ran", first)
    with durable.virtual_workflow(f"turn:{s3}:m3"), pytest.raises(Crash):
        list(run_turn(deps, info, MessageIn(text="4 呢？"), message_id="m3"))
    steps = [n for n, _, _ in log]
    assert steps.count("forge.request_tool") == 1 and steps.count("forge.pin") == 1
    assert steps.count("harness.pack") == 1          # the tool factory takes the turn's one read
    assert steps.index("harness.pack") < steps.index("forge.pin")
    built = len(forge_gw.inner.requests)
    assert built == 4

    recorded = iter(log)

    def recover(name, fn):
        if not durable.in_workflow():
            return real(name, fn)
        nxt = next(recorded, None)
        if nxt is None:
            return real(name, fn)                 # past the crash point: live again
        rec, kind, val = nxt
        assert rec == name, (rec, name)
        durable._virtual.get().next_step += 1     # the step keys stay where the first run had them
        if kind == "raise":
            raise val                             # type: ignore[misc]
        return json.loads(json.dumps(val[0])), False   # type: ignore[index]

    monkeypatch.setattr(durable, "step_ran", recover)
    with durable.virtual_workflow(f"turn:{s3}:m3"):
        list(run_turn(deps, info, MessageIn(text="4 呢？"), message_id="m3"))
    assert len(forge_gw.inner.requests) == built          # the build is not re-run
    entries = tool_entries(pool, s3)
    assert tools_of(entries) == [("request_tool", True), ("call_tool", True)]
    assert json.loads(entries[-1]["payload"]["content"]) == {"n": 8}   # the scripted "triple"
    with pool.connection() as conn:
        assert len(store.candidates_by_origin(conn, "u1", f"session:{s3}",
                                              statuses=("built",))) == 1


# --- real WASI: the story-1 batch tool with host calls -----------------------------------------

@pytest.mark.skipif(not wasi_runtime.available(), reason=wasi_runtime.why_unavailable())
def test_batch_tool_runs_in_real_wasi_with_host_calls(pool, traced):  # noqa: F811
    from evowiki.forge.sandbox.wasi import WasiSandbox
    run_round("u1", deps=round_deps(pool, coder()), gate=FakeGate(),
              embedder=HashingEmbedder(dim=256), now=NOW)
    rt = runtime(pool, sandbox=WasiSandbox.from_env())
    run = rt.for_run(user_id="u1", agent="deep", run_id="r1", group="page", pack_values={})
    assert run is not None
    reader = ReadPage()
    ctx = ToolContext(user_id="u1", session_id="refine:p1", page_id="p1")
    tool = run.tools([reader], ctx)[0]
    assert tool.spec.name == "batch_read_page"
    res = tool.run({"page_ids": ["a", "b"]}, ctx)
    assert res.error is None, res.content
    assert json.loads(res.content) == {"results": ["<a>", "<b>"]}
    assert [c.user_id for c in reader.ctxs] == ["u1", "u1"]


# --- cost: a page write's cost_usd includes its request_tool build ------------------------------

def test_page_cost_includes_the_request_tool_build(pool, traced):  # noqa: F811
    with pool.connection() as conn:
        optin.set_local(conn, "u1", "deep", "request_tool", True, NOW)
    first = [{"id": "rt-1", "name": "request_tool", "args": request_args()},
             {"id": "ct-1", "name": "call_tool", "args": {"name": "double", "args": {"n": 3}}}]

    def write(forge):
        env = (MemoryRepo(["u1"]), Script(), WikiIds())
        pid = organized(env)
        ds = DeepScript().plan().write(*two_rounds(*first))
        w = writer(ds)
        w.forge = forge
        res = w.write(outline_input(env, pid))
        assert not ds.pending()
        return res, [unfence(m.text) for r in write_requests(ds) for m in r.messages
                     if m.role == "tool"]

    res, out = write(runtime(pool, gw=Gw(costs=[0.01, 0.02, 0.03, 0.04])))
    assert any("已就绪" in t for t in out) and any(t.startswith("{") and json.loads(t) == {"n": 6}
                                                  for t in out)
    plain, _ = write(None)                  # the same writer calls without the forge
    assert res.calls == plain.calls
    assert res.cost_usd == pytest.approx(plain.cost_usd + 0.10)



def unfence(text: str) -> str:
    """A read tool's result as the tool returned it (the loop fences it as material)."""
    if DATA_BEGIN not in text:
        return text
    return text.split(DATA_BEGIN + "\n", 1)[1].rsplit("\n" + DATA_END, 1)[0]
