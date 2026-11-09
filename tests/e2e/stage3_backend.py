"""Disposable /wiki backend for the stage 3 (Skills, pending-op confirm) walkthrough in REAL Obsidian
(driver obsidian-plugin/scripts/real-obsidian/stage3-walkthrough.mjs). Test-only, no paid call.

The audit backend's wiring (`tests/e2e/audit_backend.py`: `build_services` + DBOS on a fresh
Postgres, fail-closed scripted purposes, the scripted page writer), with:

- a seeded wiki for user `u1`: the path `path-demo` (rows p1..p3, unwritten: split / postpone
  drafts work on them) and one organized page 「z 分数」 (a proposal lands on it);
- a scripted assistant turn (below) that plays a model following the Skill rules: it loads what it
  needs (read_ref / load_skill: what the catalog names), calls the tool, and on 「好」 calls `confirm` with the pending id the turn's
  ops line names; the split turn narrates before its read_ref (that text must never be shown);
- `--db URL`: reuse a database (the restart of check 11), DBOS recovers the pending turn; the
  `crash` arm (POST /test/crash/arm) kills this process inside the model call right after a
  `confirm` result, before that call is checkpointed.

GET /test/s3: model calls (purpose), every assistant request (tools offered, params, prompt
version, flags), the ledger rows of u1, the profile, unscripted calls, the teaching spec's own
items with their provenance and the rule decisions (T6). POST /test/h9/{on|off}: the user's
H9.assistant.before_answer (read_path). GET /test/session/{sid}: the session's entries.
POST /test/forget/{sid}: the preference step of a session deletion (memory spec §4.2 step 6:
`wikiapp.preferences.redact_session_evidence` inside an opened forget request; the session itself
is the memory session's DELETE, not on this branch).

Preferences (T6): 「以后都先给例子再讲定义」 → remember_preference; 「这道题讲简单点」 →
applies=this_task; 「算了，以后都先讲定义再举例」 → read_rules, then remember_preference with
replaces (the rule texts keep his words, so they are his at once: rules.drafted).

  uv run python -m tests.e2e.stage3_backend --port 8833 [--db URL]
"""

import argparse
import json
import os
import re
import time
from typing import Any

for _k in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "TAVILY_API_KEY"):
    os.environ.pop(_k, None)            # never a paid call from this process
os.environ.setdefault("EVOWIKI_EMBEDDER", "hash")

import uvicorn

from evowiki.assistant.turn_runs import DbosTurnRuns
from evowiki.bot.settings import BotSettings
from evowiki.harness.source import reader
from evowiki.llm.chat import ChatRequest, ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.runtime.llm import runtime_gateway_config
from evowiki.service.app import create_app
from evowiki.service.deps import (
    assistant_import,
    assistant_practice,
    assistant_revise,
    build_assistant,
    build_services,
)
from evowiki.store import bot as bot_store
from evowiki.store.db import make_pool
from evowiki.store.rsi import PgRsiStore
from evowiki.teach.contracts import RsiSettings
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.e2e import audit_backend as ab
from tests.e2e.wiki_backend import FailClosedScript, mount_script_status
from tests.pg import build_template, fresh_database, start_admin_url
from tests.runtime.helpers import T0, make_user, seed_runtime
from tests.wikiapp.fake_writer import writing
from tests.wikiapp.script import PROVIDERS

USER = "u1"
P1, P2, P3 = (f"path-demo-p{i}" for i in (1, 2, 3))
PLAIN1, PLAIN2 = "z 分数是什么意思？", "那 z 等于 2 呢？"
SPLIT = "拆一下这页"
WRITE = "在路径最后加一页讲 z 分数的用法"
EXPLAIN = "把这段解释加进页面"
TWO = "第一页拆开，第三页往后放"
OK, CAN = "好", "可以"
NURSE, UNDO = "我是护士", "撤销刚才那个"
QUIZ, UNDO_SHORT = "考考我", "撤销"
LEARN_FILE, ASK_FILE = "按这份学", "这份讲义讲什么？"
WRONG_Q = "我为什么错了？"
HOOKED = "我该先学什么"
PREF = PREF_TEXT = "以后都先给例子再讲定义"
PREF_NEW, PREF_NEW_TEXT = "算了，以后都先讲定义再举例", "以后都先讲定义再举例"
THIS_ONCE = "这道题讲简单点"
RULE_ID = re.compile(r"- (si_[0-9A-Za-z_]+)：以后都先给例子再讲定义")
CHAT: list[dict[str, Any]] = []
STATE: dict[str, Any] = {"crash": False, "page": None}
ACT = re.compile(r"\b(act_[0-9a-f]+)\b")
PO = re.compile(r"\b(po_[0-9A-Za-z_]+)」?")
SRC = re.compile(r"source_id=([0-9A-Za-z_-]+)")


def _said(request: ChatRequest) -> tuple[str, list[str], list[str]]:
    """(this turn's user message, the tool results after it, the tools called after it)."""
    msgs = request.messages
    users = [i for i, m in enumerate(msgs) if m.role == "user"]
    if not users:
        return "", [], []
    after = msgs[users[-1] + 1:]
    called = [c.name for m in after if m.role == "assistant" for c in m.tool_calls]
    return msgs[users[-1]].text, [m.text for m in after if m.role == "tool"], called


def _call(name: str, i: str, **args: Any) -> ChatTurn:
    return ChatTurn(tool_calls=(ToolCall(id=i, name=name, args=args),))


def _ops(said: str) -> list[str]:
    head = said.split("可以撤销或等他确认的操作", 1)
    return [p for p in PO.findall(head[1].split("\n\n", 1)[0])] if len(head) > 1 else []


NARRATION = "我需要先载入拆页的参考。"      # the split turn's text between rounds (never shown)


def _q(said: str) -> str:
    """The question: after 「他问：」, before a two_phase fold's 「已载入：」 note."""
    return said.rsplit("他问：\n", 1)[-1].split("\n\n已载入：", 1)[0].strip()


def assistant_turn(script: FailClosedScript, request: ChatRequest) -> ChatTurn:
    said, results, called = _said(request)
    offered = [t.name for t in request.tools]
    CHAT.append({"at": time.time(), "purpose": request.purpose, "version": request.prompt_version,
                 "tools": offered, "params": request.params,
                 "bridge": "先指出错在哪" in said, "said": said[-400:], "system": request.system,
                 "called": called, "results": [r[:300] for r in results],
                 "principles": said.split("为他组织内容的原则", 1)[-1].split("\n\n", 1)[0]
                 if "为他组织内容的原则" in said else ""})
    if STATE["crash"] and "confirm" in called:
        print("crash: inside the model call after confirm", flush=True)
        os._exit(1)
    if request.purpose == "hook.assistant.before_answer":       # an H9 before-answer step
        if not results:
            return _call("read_path", "h1")
        return ChatTurn(text="路径要点：先学第 1 页。")
    q = _q(said)
    if q in (PLAIN1, PLAIN2, HOOKED):
        return ChatTurn(text="z 分数是离均值有几个标准差。")
    if q == SPLIT:
        if "split_page" not in offered:
            return ChatTurn(text=NARRATION, tool_calls=(
                ToolCall(id="r1", name="read_ref", args={"path": "edit-wiki/path"}),))
        if "split_page" not in called:
            return _call("split_page", "s1", page=P2, why="一页讲太多",
                         parts=[{"title": "z 分数怎么算"}, {"title": "z 分数怎么读"}])
        if called.count("split_page") == 1 and "路径上没有" in results[-1]:
            # p2 was split by an earlier check (3): split p1, still on the path (check 11)
            return _call("split_page", "s1b", page=P1, why="一页讲太多",
                         parts=[{"title": "z 分数的意思"}, {"title": "z 分数的用法"}])
        return ChatTurn(text="草稿在卡片上，回「好」我就拆。")
    if q == WRITE:                          # write_page bound for 「好」 (its page is written on approve)
        if "write_page" not in offered:
            return _call("read_ref", "r4", path="edit-wiki/path")
        if "write_page" not in called:
            return _call("write_page", "w1", title="z 分数的用法", covers="z 分数拿来做什么",
                         path_id="path-demo")
        return ChatTurn(text="草稿在卡片上，回「好」我就加。")
    if q == EXPLAIN:
        if "propose_page_edit" not in offered:
            return _call("read_ref", "r2", path="edit-wiki/pages")
        if "propose_page_edit" not in called:
            return _call("propose_page_edit", "e1", kind="add_section", page_id=STATE["page"],
                         heading="一个例子", text="身高 180、均值 170、标准差 5：z = 2。",
                         cue="身高 180、均值 170、标准差 5，z 是多少？", answer="z = 2。",
                         why="你刚问过怎么读", paragraphs=[])
        return ChatTurn(text="我提了一段修改建议，你点了才会改。")
    if q == TWO:
        if "split_page" not in offered:
            return _call("read_ref", "r3", path="edit-wiki/path")
        if "split_page" not in called:
            return ChatTurn(tool_calls=(
                ToolCall(id="s2", name="split_page", args={
                    "page": P1, "why": "太笼统", "parts": [{"title": "甲"}, {"title": "乙"}]}),
                ToolCall(id="s3", name="postpone", args={"pages": [P3], "why": "先学前面的"})))
        return ChatTurn(text="两张草稿都在卡片上。")
    if q in (OK, CAN):
        if not called:
            ops = _ops(said)
            return _call("confirm", "c1", pending_id=ops[0] if ops else "po_none", asked=q)
        return ChatTurn(text="好的。")
    if q == NURSE:
        if "remember_about_me" not in offered:
            return _call("load_skill", "l1", skills=["remember"])
        if "remember_about_me" not in called:
            return _call("remember_about_me", "m1", applies="lasting", section="我是谁", quote=NURSE,
                         text="护士")
        return ChatTurn(text="记下了。")
    if q == PREF:
        if "remember_preference" not in offered:
            return _call("load_skill", "l3", skills=["remember"])
        if "remember_preference" not in called:
            return _call("remember_preference", "p1", applies="lasting", quote=PREF,
                         text=PREF_TEXT)
        return ChatTurn(text="记下了：以后先给例子再讲定义。")
    if q == THIS_ONCE:
        if "remember_preference" not in offered:
            return _call("load_skill", "l4", skills=["remember"])
        if "remember_preference" not in called:
            return _call("remember_preference", "p2", applies="this_task", quote=THIS_ONCE,
                         text="讲简单点")
        return ChatTurn(text="好，这道题我讲简单点。")
    if q == PREF_NEW:
        if "read_rules" not in offered or "remember_preference" not in offered:
            return _call("load_skill", "l5", skills=["remember", "settings"])
        if "read_rules" not in called:
            return _call("read_rules", "r9")
        if "remember_preference" not in called:
            hit = RULE_ID.findall("\n".join(results))
            return _call("remember_preference", "p3", applies="lasting", quote="以后都先讲定义再举例",
                         text=PREF_NEW_TEXT, replaces=hit[-1] if hit else "")
        return ChatTurn(text="改好了：以后先讲定义再举例。")
    if q in (UNDO, UNDO_SHORT):
        if not called:
            return _call("undo", "u1", action_id="last", asked=q)
        return ChatTurn(text="撤销了。")
    if q == QUIZ:
        if "open_review" not in called:       # a core tool: offered from the first request
            return _call("open_review", "q1")
        return ChatTurn(text="题目在下面。")
    if q == LEARN_FILE:
        if not called:
            ids = SRC.findall(said)
            return _call("new_goal", "g1", goal="", source_ids=ids[-1:])
        return ChatTurn(text="按这份讲义排好了分块，等你确认。")
    if q == ASK_FILE:
        return ChatTurn(text="这份讲义讲向量和矩阵。")
    if q == WRONG_Q:
        return ChatTurn(text="你把方差和标准差混在了一起。换个例子：……")
    return ab.assistant_turn(script, request)


def assistant_gateway(script: FailClosedScript) -> Gateway:
    providers = {p: ScriptedChatProvider(p, lambda _m, r: assistant_turn(script, r),
                                         complete=script._responder(p)) for p in PROVIDERS}
    # the production default: DeepSeek (tool exposure mid_turn_swap)
    cfg = runtime_gateway_config({"EVOWIKI_RUNTIME_PROVIDER": "deepseek"})
    return Gateway(cfg, script.log, mode="record", clock=lambda: T0, providers=providers,
                   record_max_attempts=1)


def seed(pool) -> str:
    """The path rows and one organized page for u1 (tests' own fixtures, PgRepo)."""
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_path_edits import Ids, seed_events
    from tests.wikiapp.test_review_flow import organized_learned_page
    repo, ids = PgRepo(pool), Ids()
    pid = organized_learned_page((repo, Script(), ids))
    aspect, wiki = seed_events(ids)
    repo.commit(repo.load(USER), Commit(aspect=aspect, wiki=wiki))
    return pid


def prepare(pool) -> None:
    """A fresh database's runtime rows, user u1 and its settings (the Bot's idle writes off)."""
    seed_runtime(pool)
    make_user(pool, USER)
    PgRsiStore(pool).put_settings(USER, RsiSettings(cycle="manual"))      # TEST ONLY
    kinds = {**BotSettings().kinds, "idle_write": False}                  # TEST ONLY
    with pool.connection() as conn:
        bot_store.put_settings(conn, USER, BotSettings(kinds=kinds), T0)


def build(url: str, pool) -> tuple[Any, Any, Any]:
    """The service app over `url` (scripted models, DBOS turns), with u1's seeded wiki."""
    with pool.connection() as conn:
        page = conn.execute("select 1 from wiki_event where user_id = %s limit 1",
                            (USER,)).fetchone()
    STATE["page"] = seed(pool) if page is None else _page_id(pool)
    svc = build_services({"EVOWIKI_DATABASE_URL": url, "EVOWIKI_EMBEDDER": "hash",
                          "EVOWIKI_BOT": "off", "EVOWIKI_DBOS_EXECUTOR": "stage3"})
    script = ab.build_script()
    script.always("review.followup_check",
                  lambda _p: {"stem": "z = 2 表示什么？", "options": ["高出两个标准差", "高出 2 分",
                                                                  "是均值的 2 倍"], "answer": 0})
    svc.gateway = script.gateway()
    model = ab.WriterModel()
    svc.page_writing = writing(PgRepo(svc.pool), model)
    svc.page_writing.writer.before_write = model.reset
    svc.assistant = build_assistant(
        # the user's harness pack read per turn, as the service's gateway does (H9 of check 10)
        svc.pool, assistant_gateway(script).with_harness(reader(svc.pool)), svc.ctx.content,
        svc.embedder, svc.clock, svc.new_id,
        runs=DbosTurnRuns(), ledger=svc.actions,
        import_source=lambda uid, req: assistant_import(svc, uid, req),
        revise=lambda uid, pid, sids, key: assistant_revise(svc, uid, pid, sids, key),
        practice=lambda uid, pid, kind, covers, key: assistant_practice(svc, uid, pid, kind, covers,
                                                                        key))
    app = create_app(svc)
    mount_script_status(app, script)
    return app, svc, script


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8833)
    ap.add_argument("--db", default="", help="reuse this database (a restart)")
    a = ap.parse_args(argv)
    if a.db:
        url = a.db
        pool = make_pool(url)
    else:
        admin, _keep = start_admin_url()
        build_template(admin)
        url, _ = fresh_database(admin)
        pool = make_pool(url)
        prepare(pool)
    app, svc, script = build(url, pool)

    def rows(sql: str, *args: Any) -> list[dict[str, Any]]:
        with pool.connection() as conn:
            return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
                    for r in conn.execute(sql, args).fetchall()]

    @app.get("/test/s3")
    def _s3() -> dict[str, Any]:
        from evowiki.profile.store import PgProfileStore
        return {"calls": ab.CALLS, "chat": CHAT, "unscripted": list(script.unscripted),
                "page": STATE["page"], "url": url,
                "ledger": rows("select action_id, kind, status, source, summary, result, "
                               "created_at, decided_at from agent_action where user_id = %s "
                               "order by created_at", USER),
                "profile": PgProfileStore(pool, svc.clock).get(USER).model_dump(mode="json"),
                "events": rows("select seq, type from wiki_event where user_id = %s order by seq",
                               USER),
                "spec": _spec(), }

    def _spec() -> dict[str, Any]:
        from evowiki.wikiapp.teachspec import effective_spec
        st = PgRepo(pool).load(USER).state
        own = [i for i in effective_spec(st).items if i.origin not in ("default", "seed")]
        return {"items": [{**i.model_dump(mode="json"),
                           "evidence": st.item_evidence.get(i.item_id)} for i in own],
                "decisions": [d.model_dump(mode="json") for d in st.rule_decisions],
                "evidence": dict(st.item_evidence)}

    @app.post("/test/forget/{sid}")
    def _forget(sid: str) -> dict[str, Any]:
        from evowiki.wikiapp.preferences import redact_session_evidence
        with pool.connection() as conn, conn.transaction():
            rid = conn.execute("select evowiki_forget_request(%s, %s, null, 'api:walk') as r",
                               (USER, sid)).fetchone()["r"]
            conn.execute("select evowiki_forget_open(%s)", (rid,))
            out = redact_session_evidence(conn, USER, sid, request_id=rid)
            conn.execute("select evowiki_forget_close(%s)", (rid,))
        return out

    @app.get("/test/session/{sid}")
    def _session(sid: str) -> list[dict[str, Any]]:
        return rows("select seq, type, payload from assistant_entry where session_id = %s "
                    "order by seq", sid)

    @app.post("/test/h9/{op}", status_code=204)
    def _h9(op: str) -> None:
        from evowiki.store import harness as store_harness
        step = {"at": "assistant.before_answer", "kind": "delegate", "tier": "fast",
                "tools": ["read_path"], "instruction": "先看他的学习路径", "output_to": "context.notes",
                "max_tool_calls": 2}
        with pool.connection() as conn:
            store_harness.set_value(conn, USER, "H9.assistant.before_answer",
                                    [step] if op == "on" else None, source="user", why="walk",
                                    now=svc.clock())

    @app.post("/test/crash/arm", status_code=204)
    def _crash() -> None:
        STATE["crash"] = True

    print(json.dumps({"wiki_backend": f"http://127.0.0.1:{a.port}", "user": USER, "db": url,
                      "page": STATE["page"]}, ensure_ascii=False), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


def _page_id(pool) -> str:
    repo = PgRepo(pool)
    st = repo.load(USER).state
    return next(pid for pid, rec in st.pages.items() if rec.page is not None)


if __name__ == "__main__":
    main()
