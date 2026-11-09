"""Disposable backend for the 待确认的记忆 (possibly conflicting memories) walkthrough in REAL
Obsidian (driver obsidian-plugin/scripts/real-obsidian/conflicts-walkthrough.mjs). Test-only, no
paid call.

The audit backend (tests/e2e/audit_backend.py: build_services on a fresh Postgres, DBOS launched
by the app lifespan, every model a scripted fake) with the production conflicts flow as
`build_services` / `build_assistant` wire it (GET /assistant/memory/conflicts, the 冲突 Skill's
read_memory_conflicts / resolve_memory_conflict, the ledger's memory_conflict_resolve undo).

POST /test/seed (header X-Evowiki-User), through the real stores (land_preference, the profile
store), for that user: rules 「以后讲公式先举例子」「以后直接给公式，别举例了」「每页都写长一点，讲透」
(his words in an earlier chat, active), a 我是谁 line 「我每天只有半小时」, and two open groups in
memory_conflict as the detector would store them: k1 = the two formula rules, k2 = the profile
line and the long-page rule. -> {"user", "rules": {text: id}, "fact_key", "groups": {"k1", "k2"}}
GET /test/conflicts-state (header): the groups by status and the rules in force.
POST /test/user (header): that user with nothing (the 个性化 empty states).

Scripted chat (the turn 「处理」 opens, intent resolve_memory_conflicts, whose first call the loop
makes itself): the groups it read → the first group (why, its items) and ask_user with
保留「…」 per item and 两条都保留; his answer → resolve_memory_conflict (keep that id / both, his
answer as the quote) → read_memory_conflicts again → the next group, or 「都处理完了。」.

  uv run python -m tests.e2e.conflicts_backend --port 8862 --user conflicts-user
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import SessionInfo
from evowiki.assistant.tools.ask_user import REPLIED
from evowiki.assistant.tools.conflicts import NONE_OPEN
from evowiki.llm.chat import ChatRequest, ChatTurn, ToolCall
from evowiki.memory.contracts import EvidenceRef, MemoryScope
from evowiki.profile.note import WHO
from evowiki.profile.store import PgProfileStore
from evowiki.store.db import fetch_all, fetch_one
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.preferences import CurrentMessage, land_preference
from evowiki.wikiapp.repo import PgRepo
from tests.e2e import audit_backend as ab
from tests.runtime.helpers import make_user

START = "处理待确认的记忆"
RULES = ("以后讲公式先举例子", "以后直接给公式，别举例了", "每页都写长一点，讲透")
FACT = "我每天只有半小时"
SAID = "以后讲公式先举例子。以后直接给公式，别举例了。每页都写长一点，讲透。我每天只有半小时。"
WHYS = {"k1": "一个说先举例子，一个说直接给公式", "k2": "每天只有半小时，规则却要每页写长"}
QUESTION = "你想保留哪条？也可以直接告诉我怎么改。"
NUM = {2: "两", 3: "三"}

_GROUP = re.compile(r"^第 \d+ 组 \[([^\]]+)\]：(.*)$")
_ITEM = re.compile(r"^- \[([^\]]+)\]（[^）]*）(.*?)(；原话「.*」)?$")
_inner = ab.assistant_turn


def _groups(text: str) -> list[dict[str, Any]]:
    """read_memory_conflicts' text (tools/conflicts.py conflicts_text) back into groups."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if m := _GROUP.match(line.strip()):
            out.append({"id": m.group(1), "why": m.group(2), "items": []})
        elif (m := _ITEM.match(line.strip())) and out:
            out[-1]["items"].append({"id": m.group(1), "text": m.group(2)})
    return out


def _all_text(request: ChatRequest) -> list[str]:
    return [m.text for m in request.messages if m.text]


def _latest_listing(request: ChatRequest) -> list[dict[str, Any]] | None:
    for t in reversed(_all_text(request)):
        if "待确认的记忆 " in t and " 组：" in t:
            return _groups(t)
        if NONE_OPEN in t:                       # the read result (fenced by the loop)
            return []
    return None


def _ask(groups: list[dict[str, Any]], n: int) -> ChatTurn:
    if not groups:
        return ChatTurn(text="都处理完了。")
    g = groups[0]
    k = len(g["items"])
    lines = [f"{g['why']}，{NUM.get(k, str(k))}条现在都在用："] + [f"「{x['text']}」" for x in g["items"]]
    options = [f"保留「{x['text']}」" for x in g["items"]] + [f"{NUM.get(k, str(k))}条都保留"]
    return ChatTurn(text="\n\n".join(lines), tool_calls=(ToolCall(
        id=f"ask{n}", name="ask_user", args={"question": QUESTION, "options": options}),))


def _reply(text: str) -> str | None:
    """His answer, from ask_user's resume result (tools/ask_user.py REPLIED); None: not one."""
    head, tail = REPLIED.split("{q}")[0], "他的下一条消息：\n"
    if not text.startswith(head) or tail not in text:
        return None
    return text.split(tail, 1)[1].split("\n", 1)[0].strip()


SETTLED = ("已处理", "两条都保留了")


def assistant_turn(script, request: ChatRequest) -> ChatTurn:
    _, tools = ab._said(request)
    texts = _all_text(request)
    if not any(START in t for t in texts):
        return _inner(script, request)
    ab.CHAT.append({"conflicts_turn": True, "tools": [t.name for t in request.tools], "results": tools})
    n = len(texts)
    last = tools[-1] if tools else ""
    answer = _reply(last)
    if answer is not None:                       # his answer to the group asked: settle it
        groups = _latest_listing(request) or []
        if not groups:
            return ChatTurn(text="都处理完了。")
        g = groups[0]
        keep = next((x["id"] for x in g["items"] if answer == f"保留「{x['text']}」"), None)
        if keep is None and answer.endswith("条都保留"):
            keep = "both"
        if keep is None:
            return ChatTurn(text="好，你想怎么改？")
        return ChatTurn(tool_calls=(ToolCall(id=f"res{n}", name="resolve_memory_conflict", args={
            "conflict_id": g["id"], "keep": keep, "quote": answer}),))
    if last.startswith(SETTLED):                 # settled: read what is left
        return ChatTurn(tool_calls=(ToolCall(id=f"read{n}", name="read_memory_conflicts", args={}),))
    groups = _latest_listing(request)
    if groups is None:                           # a refusal: say it
        return ChatTurn(text=last or "没能处理待确认的记忆")
    turn = _ask(groups, n)
    done = next((t for t in reversed(tools) if t.startswith(SETTLED)), None)
    if done is None:
        return turn
    said_done = done.split("：", 1)[-1] + "，可以撤销。" if done.startswith("已处理") else done
    return ChatTurn(text=f"{said_done}\n\n{turn.text}", tool_calls=turn.tool_calls)


ab.assistant_turn = assistant_turn       # assistant_gateway looks it up at call time


def seed(svc: Any, uid: str) -> dict[str, Any]:
    pool, new_id = svc.pool, svc.new_id
    with pool.connection() as conn:
        if fetch_one(conn, "select 1 from app_user where user_id = %s", (uid,)) is None:
            make_user(pool, uid)
    now = datetime.now(UTC)
    store = PgSessionStore(pool)
    sid = new_id("ses")
    store.create(SessionInfo(session_id=sid, user_id=uid, page_id=None, note_concept_id=None,
                             created_at=now - timedelta(days=2)))
    mid = new_id("msg")
    store.append(sid, new_id("ent"), "user", {"message_id": mid, "text": SAID}, now - timedelta(days=2))
    store.append(sid, new_id("ent"), "assistant", {"message_id": new_id("msg"), "text": "好，都记下了。"},
                 now - timedelta(days=2, seconds=-20))
    repo, profile = PgRepo(pool), PgProfileStore(pool, svc.clock)
    current = CurrentMessage(message_id=mid, words=(SAID,))
    ev = lambda q: [EvidenceRef(session_id=sid, message_id=mid, role="user", quote=q)]
    rules: dict[str, str] = {}
    for text in RULES:
        got = land_preference(repo, repo.load(uid), kind="teaching", text=text,
                              provenance="explicit_current", evidence=ev(text),
                              scope=MemoryScope(scope_type="global"), session_id=sid, now=now,
                              new_id=new_id, current=current, profile=profile)
        if got.status != "active":
            raise RuntimeError(f"seed: {text} did not land: {got}")
        rules[text] = got.item_id
    got = land_preference(repo, repo.load(uid), kind="about_me", text=FACT,
                          provenance="explicit_current", evidence=ev(FACT),
                          scope=MemoryScope(scope_type="global"), section=WHO, session_id=sid,
                          now=now, new_id=new_id, current=current, profile=profile)
    if got.status != "active":
        raise RuntimeError(f"seed: {FACT} did not land: {got}")
    lib = {e.text: e.id for e in svc.conflicts.library(uid)}
    groups = {"k1": sorted([rules[RULES[0]], rules[RULES[1]]]), "k2": sorted([lib[FACT], rules[RULES[2]]])}
    with pool.connection() as conn, conn.transaction():
        for k, ids in groups.items():
            conn.execute("insert into memory_conflict (conflict_id, user_id, item_ids, why, status, "
                         "detected_at) values (%s, %s, %s, %s, 'open', %s)",
                         (f"{k}-{uid}", uid, ids, WHYS[k], now))
    return {"user": uid, "rules": rules, "fact_key": lib[FACT],
            "groups": {k: f"{k}-{uid}" for k in groups}}


_mount = ab.mount_script_status


def mount(app, script) -> None:
    _mount(app, script)
    svc = app.state.services
    assert svc.conflicts is not None, "build_services did not wire conflicts"

    @app.post("/test/seed")
    def _seed(request: Request) -> dict[str, Any]:
        return seed(svc, request.headers["X-Evowiki-User"])

    @app.post("/test/user")
    def _user(request: Request) -> dict[str, str]:
        uid = request.headers["X-Evowiki-User"]
        with svc.pool.connection() as conn:
            if fetch_one(conn, "select 1 from app_user where user_id = %s", (uid,)) is None:
                make_user(svc.pool, uid)
        return {"user": uid}

    @app.get("/test/conflicts-state")
    def _state(request: Request) -> dict[str, Any]:
        uid = request.headers["X-Evowiki-User"]
        with svc.pool.connection() as conn:
            rows = fetch_all(conn, "select conflict_id, status from memory_conflict where user_id = %s "
                                   "order by conflict_id", (uid,))
        rules = [i.text for i in tspec.effective_spec(PgRepo(svc.pool).load(uid).state).items
                 if i.authority == "user"]
        return {"groups": {r["conflict_id"]: r["status"] for r in rows}, "rules": rules,
                "open": [g.conflict_id for g in svc.conflicts.open(uid)]}


ab.mount_script_status = mount           # main() looks it up at call time

if __name__ == "__main__":
    ab.main()
