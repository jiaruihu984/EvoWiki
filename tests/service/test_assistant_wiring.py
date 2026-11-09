"""build_services wires the reading assistant over Postgres (E1's PgRepo, F2's tools, sessions in
PgSessionStore) whenever a gateway exists; end to end over HTTP with a scripted LLM."""

import json

from fastapi.testclient import TestClient

from evowiki.assistant.service import AssistantService
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.runtime.llm import DEEPSEEK_NOTHINK
from evowiki.service import deps
from evowiki.service.app import create_app
from evowiki.service.deps import build_assistant, build_services
from evowiki.store.db import fetch_all
from evowiki.wikiapp.repo import PgRepo
from tests.runtime.helpers import FixedClock, seed_runtime
from tests.service.conftest import H, Ids, parse_sse, signup
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_page import organized

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
ASK_REPLY = {"answer": "见上", "observations": [
    {"cell": "c1.interpret", "polarity": "-", "strength": 0.6, "reason": "问 z=-1 的含义"}]}


def test_build_services_wires_the_assistant_only_with_a_gateway(db_url, pool, monkeypatch):
    seed_runtime(pool)
    env = {"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash"}
    svc = build_services(env, clock=FixedClock(NOW))
    try:
        assert svc.gateway is None and svc.assistant is None
    finally:
        svc.pool.close()
    # DeepSeek mode needs both tiers: "deepseek" (good) and DEEPSEEK_NOTHINK (fast)
    monkeypatch.setattr(deps, "runtime_providers",
                        lambda e: {"deepseek": object(), DEEPSEEK_NOTHINK: object()})
    svc = build_services({**env, "EVOWIKI_RUNTIME_PROVIDER": "deepseek"},
                         clock=FixedClock(NOW))
    try:
        assert svc.gateway is not None and isinstance(svc.assistant, AssistantService)
        # cross-session memory is wired: the turn's ports and the three tools
        from evowiki.assistant.durable import durable_turn_deps
        from evowiki.assistant.session import SessionInfo
        from evowiki.memory.lander import AutoRuleGuard, S3PreferenceLander
        from evowiki.memory.store import PgMemoryStore

        turn = svc.assistant.turn_deps
        assert turn.memory is not None and isinstance(turn.memory.store, PgMemoryStore)
        assert turn.memory.embedder is svc.embedder and turn.memory.sessions is turn.store
        assert isinstance(turn.memory.lander, S3PreferenceLander)
        assert isinstance(turn.memory.guard, AutoRuleGuard)
        assert turn.memory.authority is not None
        assert turn.memory.known_concepts is not None
        assert durable_turn_deps(turn).memory.steps is True
        from evowiki.content.models import Lang
        from evowiki.store.users import UserRow, create_user
        with pool.connection() as conn:
            create_user(conn, UserRow(user_id="u1", background="none", condition="S2",
                                      lang=Lang.ZH, created_at=NOW))
        info = SessionInfo(session_id="s", user_id="u1", page_id=None, note_concept_id=None,
                           created_at=NOW)
        names = {t.spec.name for t in turn.tools(info, NOW)}
        assert {"search_memory", "read_past_turn", "forget_memory", "cancel_card"} <= names
    finally:
        svc.pool.close()


def test_assistant_end_to_end_over_postgres(services, tmp_path):
    client = TestClient(create_app(services))
    signup(client, "u1")
    repo = PgRepo(services.pool)
    ids = Ids()                     # one id source: the page's events and the assistant's never clash
    pid = organized((repo, Script(), ids))           # a real organized page via E1's flows

    outs = iter([
        ChatTurn(tool_calls=(ToolCall(id="c1", name="search_wiki", args={"query": "z 分数"}),
                             ToolCall(id="c2", name="read_page", args={}))),
        ChatTurn(tool_calls=(ToolCall(id="c3", name="propose_page_edit", args={
            "kind": "add_section", "heading": "补充：z=-1", "text": "比均值低一个标准差。", "cue": "z=-1 表示什么？", "answer": "比均值低一个标准差。",
            "paragraphs": [],
            "why": "刚问过"}),)),
        ChatTurn(text="z=-1 表示比均值低一个标准差。")])
    asks = []

    def complete(model, request):
        asks.append(request.purpose)
        return json.dumps(ASK_REPLY, ensure_ascii=False)

    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={
        "scripted": ScriptedChatProvider("scripted", lambda m, r: next(outs), complete=complete)})
    services.assistant = build_assistant(services.pool, gw, services.ctx.content,
                                         services.embedder, FixedClock(NOW), ids)

    r = client.post("/assistant/sessions", json={"page_id": pid}, headers=H("u1"))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert client.post("/assistant/sessions", json={"page_id": "nope"},
                       headers=H("u1")).status_code == 404
    r = client.post(f"/assistant/sessions/{sid}/messages", json={"text": "z=-1 是什么意思？"},
                    headers=H("u1"))
    assert r.status_code == 200, r.text
    evs = parse_sse(r.text)
    kinds = [e for e, _ in evs]
    assert kinds[-1:] == ["done"], evs
    assert asks == []                   # no question observer call after the answer (stage 3 T9)
    pid_prop = next(d for e, d in evs if e == "proposal")["proposal_id"]

    s = client.get(f"/assistant/sessions/{sid}", headers=H("u1")).json()
    assert [m["role"] for m in s["messages"]] == ["user", "assistant"]
    assert s["messages"][1]["text"] == "z=-1 表示比均值低一个标准差。"
    assert any(src.get("page_id") == pid for src in s["messages"][1]["sources"])
    with services.pool.connection() as conn:
        tools = fetch_all(conn, "select payload from assistant_entry where session_id = %s "
                                "and type = 'tool' order by seq", (sid,))
    assert [(t["payload"]["name"], t["payload"]["ok"]) for t in tools] == [
        ("search_wiki", True), ("read_page", True), ("propose_page_edit", True)]

    # no observer: nothing is read into K behind the model's back (evidence is record_evidence's)
    with services.pool.connection() as conn:
        rows = fetch_all(conn, "select event_id from aspect_event where user_id = %s "
                               "and event_id like 'assistant:%%'", ("u1",))
    assert rows == []

    out = client.post(f"/assistant/proposals/{pid_prop}/accept", headers=H("u1")).json()
    assert out["status"] == "accepted" and len(out["applied"]) == 1, out
    page = repo.load("u1").state.pages[pid].page          # appended at the end, assistant text
    assert page.outline.sections[-1].id == out["applied"][0]
    assert page.texts[out["applied"][0]].body == "比均值低一个标准差。"


def test_build_services_wires_chat_attachments(db_url, pool, monkeypatch):
    """Production always has the attachment reader and image routing; eyes only in DeepSeek
    mode with an Anthropic key (the main model cannot see), metered."""
    from evowiki.assistant.attachments import PgAttachmentReader
    from evowiki.llm.usage import Account
    seed_runtime(pool)
    env = {"EVOWIKI_DATABASE_URL": db_url, "EVOWIKI_EMBEDDER": "hash"}
    acct = Account(user_id="u1", group="assistant")
    cases = [({"deepseek": object(), DEEPSEEK_NOTHINK: object()}, "deepseek", False, False),
             ({"deepseek": object(), DEEPSEEK_NOTHINK: object(), "anthropic": object()},
              "deepseek", False, True),
             ({"anthropic": object(), "deepseek": object()}, "anthropic", True, False)]
    for providers, mode, sees, has_eyes in cases:
        monkeypatch.setattr(deps, "runtime_providers", lambda e, p=providers: p)
        monkeypatch.setattr(deps, "runtime_deep_providers", lambda e: {})
        credentials = {"DEEPSEEK_API_KEY": "fake-deepseek"}
        if "anthropic" in providers:
            credentials["ANTHROPIC_API_KEY"] = "fake-anthropic"
        svc = build_services({**env, **credentials, "EVOWIKI_RUNTIME_PROVIDER": mode}, clock=FixedClock(NOW))
        try:
            td = svc.assistant.turn_deps
            assert isinstance(td.attachments, PgAttachmentReader), mode
            assert td.sees_images is not None and td.sees_images(acct) is sees, mode
            assert (td.eyes is not None) is has_eyes, (mode, providers.keys())
            if has_eyes:
                assert td.eyes.metered and td.eyes.route_provider("generator") == "anthropic"
                # the same meter as the main gateway: one budget for the user's assistant
                assert td.eyes._usage.meter is svc.gateway._usage.meter
            # recorded-but-unstarted import runs are marked interrupted once DBOS is up
            hooks = [h.__name__ for h in svc.durable.after_launch]
            assert "start_unstarted_runs" in hooks
        finally:
            svc.pool.close()


def test_attachment_turn_over_postgres(services, tmp_path):
    """A material attached to a message reaches the model as a fenced document part; a deleted
    one is a 409 with the text to show (the session stays)."""
    from evowiki.sources import store
    from evowiki.sources.models import SectionRow, SourceRow
    client = TestClient(create_app(services))
    signup(client, "u1")
    row = SourceRow(source_id="src1", user_id="u1", kind="md", title="讲义", sha256="x",
                    bytes=10, pages=None, status="ready", error=None, unreadable_pages=(),
                    toc_mode="headings", progress={}, origin=None, created_at=NOW,
                    updated_at=NOW, deleted_at=None)
    with services.pool.connection() as conn:
        store.insert_source(conn, row, b"# x", "text/markdown")
        store.replace_sections(conn, "u1", "src1", [SectionRow(
            section_id="src1-1", source_id="src1", parent_id=None, ord=0, depth=0, title="向量",
            page_from=None, page_to=None, text="向量有方向和大小。", tokens=8, summary=None,
            read_by="text")])
        store.add_use(conn, "u1", "src1", "ask", session_id=None, now=NOW)
    prov = ScriptedChatProvider("scripted", lambda m, r: ChatTurn(text="讲的是向量。"))
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})
    services.assistant = build_assistant(services.pool, gw, services.ctx.content,
                                         services.embedder, FixedClock(NOW), Ids())
    sid = client.post("/assistant/sessions", json={}, headers=H("u1")).json()["session_id"]
    r = client.post(f"/assistant/sessions/{sid}/messages", json={"attachments": ["src1"]},
                    headers=H("u1"))
    assert r.status_code == 200, r.text
    done = next(d for e, d in parse_sse(r.text) if e == "done")
    card = done["attachments"][0]
    assert (card["title"], card["chars"], card["unreadable_pages"]) == ("讲义", 9, [])
    last = prov.chat_calls[0].messages[-1]
    assert [p.kind for p in last.parts] == ["doc"] and "向量有方向和大小。" in last.parts[0].text
    with services.pool.connection() as conn:
        store.soft_delete(conn, "u1", "src1", NOW)
    r = client.post(f"/assistant/sessions/{sid}/messages",
                    json={"text": "再看", "attachments": ["src1"]}, headers=H("u1"))
    assert r.status_code == 409 and r.json() == {"detail": "附件已经删除了，去掉它再发",
                                                  "code": "attachment_refused"}


def test_asking_with_an_old_attachment_makes_it_young_again(services, tmp_path):
    """D20: a 40-day-old chat attachment sent again in a new conversation gets an ask use of
    that session, so its expiry moves and the cleanup leaves it."""
    from datetime import timedelta

    from evowiki.sources import store
    from evowiki.sources.cleanup import KEEP_ASK_DAYS
    from evowiki.sources.models import SectionRow, SourceRow
    client = TestClient(create_app(services))
    signup(client, "u1")
    old = NOW - timedelta(days=40)
    row = SourceRow(source_id="src1", user_id="u1", kind="md", title="讲义", sha256="x",
                    bytes=10, pages=None, status="ready", error=None, unreadable_pages=(),
                    toc_mode="headings", progress={}, origin=None, created_at=old,
                    updated_at=old, deleted_at=None)
    with services.pool.connection() as conn:
        store.insert_source(conn, row, b"# x", "text/markdown")
        store.replace_sections(conn, "u1", "src1", [SectionRow(
            section_id="src1-1", source_id="src1", parent_id=None, ord=0, depth=0, title="向量",
            page_from=None, page_to=None, text="向量有方向和大小。", tokens=8, summary=None,
            read_by="text")])
        store.add_use(conn, "u1", "src1", "ask", session_id="chat-old", now=old)
    prov = ScriptedChatProvider("scripted", lambda m, r: ChatTurn(text="讲的是向量。"))
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})
    services.assistant = build_assistant(services.pool, gw, services.ctx.content,
                                         services.embedder, FixedClock(NOW), Ids())
    sid = client.post("/assistant/sessions", json={}, headers=H("u1")).json()["session_id"]
    r = client.post(f"/assistant/sessions/{sid}/messages",
                    json={"text": "讲了什么", "attachments": ["src1"]}, headers=H("u1"))
    assert r.status_code == 200, r.text
    card = next(d for e, d in parse_sse(r.text) if e == "done")["attachments"][0]
    assert card["expires_at"].startswith(str((NOW + timedelta(days=30)).date()))
    with services.pool.connection() as conn:
        assert [(u["session_id"], u["created_at"]) for u in store.uses_of(conn, "u1", "src1")
                ][-1] == (sid, NOW)
        assert store.stale_ask_only(conn, before=NOW - timedelta(days=KEEP_ASK_DAYS)) == []
