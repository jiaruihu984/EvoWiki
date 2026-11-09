"""Disposable /wiki backend for the 学点新的 diagnosis walkthrough in REAL Obsidian (the approved demo's
#diagnose / #diagnose-zero; driver obsidian-plugin/scripts/real-obsidian/diagnose-walkthrough.mjs).
Test-only, no paid call: tests/e2e/blocks_backend.py's wiring (fresh Postgres, scripted purposes,
the goal_new ledger, the scripted page writer) with:

- goals.intake S1 (「把学习目标分成几大块」): the demo's six 黑壳虾 blocks; 零基础 in his words →
  stated_beginner (D07: no block is diagnosed);
- an entered block (S2, one level): its covers as pages (blocks_backend.BLOCK_PAGES);
- a scripted assistant turn that plays a model following the plan-goal / assess Skills:
  - 开始 (intent learn_goal): the turn ran new_goal itself; the tail is the one line the Skill asks
    for (「已分成 N 个学习主题，可以在左侧修改，改好后点「确认」。」);
  - the diagnosis (intent diagnose, 「确认」): the turn calls ask_rough (the 摸底 grid), then on his marks
    ask_check with the demo's question for that block (DIAG below) on each pick the results name,
    until none is left, then 「从「X」开始，第一页在写。」 (X: the first block he did not mark 掌握);
  - his answer to a question (an option's text) resumes the turn the way master's ask model does.
Anything else fails closed (Unscripted, listed by GET /test/wiki-script).

  uv run python -m tests.e2e.diag_backend --port 8791 [--user walk-user]
"""

import argparse
import dataclasses
import json
import os
import re
import sys
import time

import uvicorn

from evowiki.assistant.loop import DIAGNOSE_HEAD, LEARN_GOAL_HEAD
from evowiki.bot.actions import ActionLedger
from evowiki.bot.executors_assistant import GoalNewExecutor
from evowiki.clock import SystemClock
from evowiki.goals.intake import OUTLINE_PURPOSE
from evowiki.goals.path import PURPOSE as PATH
from evowiki.llm.chat import ChatRequest, ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.service.app import create_app
from evowiki.service.deps import Services, build_assistant, import_deps
from evowiki.sources.runs import InlineSourceRuns
from evowiki.store.db import make_pool
from evowiki.store.rsi import PgRsiStore
from evowiki.teach.contracts import RsiSettings
from evowiki.wikiapp.llm import require
from evowiki.wikiapp.page_run_store import PgPageRunStore
from evowiki.wikiapp.page_runs import InlinePageRuns, spawn_thread
from evowiki.wikiapp.repo import PgRepo
from tests.e2e import blocks_backend as bb
from tests.e2e.materials_backend import organize_reply, path_reply
from tests.e2e.wiki_backend import FailClosedScript, Unscripted, mount_script_status
from tests.pg import build_template, fresh_database, start_admin_url
from tests.runtime.helpers import T0, make_user, seed_runtime
from tests.runtime.llm_helpers import GW_CONFIG
from tests.wikiapp.e2e_driver import Ids
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.fake_writer import WRITE, writing
from tests.wikiapp.script import PROVIDERS

os.environ.setdefault("EVOWIKI_EMBEDDER", "hash")
for _k in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "TAVILY_API_KEY"):
    os.environ.pop(_k, None)            # never a paid call from this process

# The demo's 细问 per block (big-goal-demo.html TOPICS["黑壳虾"].diag): stem, options, the right one.
DIAG: dict[str, tuple[str, list[str], int]] = {
    "分类与鉴别": ("「黑壳虾」这个名字是？", ["一个物种的学名", "水族店的俗称，可能是几种虾", "樱花虾的幼体"], 1),
    "水质与环境": ("新开的缸第二天就放虾，最可能出什么问题？", ["氨氮升高，虾中毒", "水太干净", "pH 太高"], 0),
    "开缸与日常": ("新买的虾到家，放进缸之前要先做什么？", ["先饿它两天", "直接倒进缸里", "过水：慢慢把缸里的水兑进袋子"], 2),
    "营养与摄食": ("虾粮喂多了，最先会怎样？", ["虾长得更快", "剩下的粮烂掉，水变坏", "虾开始抱卵"], 1),
    "生长与繁殖": ("母虾肚子下面挂着一团小颗粒，这是？", ["抱卵，过几周孵出小虾", "生病了", "吃多了"], 0),
    "疾病与风险": ("往虾缸里加了含铜的药，虾会怎样？", ["长得更红", "没有影响", "铜对虾有毒，可能死虾"], 2),
}
OPENING = "已分成 {n} 个学习主题，可以在左侧修改，改好后点「确认」。"
_S1 = re.compile(r"把学习目标先?分成几大块")
_PICK = re.compile(r"pick_id：(\S+)")
_PICK_NAME = re.compile(r"知识点 / 块：「(.+?)」")
_SELF = re.compile(r"要问的：自评")

CHAT_LOG: list[dict] = []


def intake_reply(prompt: str) -> dict:
    """S1 (this branch's prompt wording) or an entered block; the rest is blocks_backend's."""
    if _S1.search(prompt):
        word = next((w for w in ("完全零基础", "零基础", "完全没学过") if w in prompt), "")
        reply = {k: v for k, v in bb.BLOCKS.items()
                 if "framing" in prompt or k not in ("framing", "caveat", "mainline", "start", "question")}
        for b in reply["blocks"]:
            b["atomic"] = True             # entering a block plans its pages (the demo shows the page cards)
        return {**reply, "limits": [], "stated_beginner": word != "", "beginner_quote": word}
    return bb.intake_reply(prompt)


def _tool(name: str, i: str, **args) -> ChatTurn:
    return ChatTurn(tool_calls=(ToolCall(id=i, name=name, args=args),))


def assistant_turn(script: FailClosedScript, request: ChatRequest) -> ChatTurn:
    """The scripted 助手 turn (module doc). Reads only the request (stateless)."""
    msgs = request.messages
    users = [i for i, m in enumerate(msgs) if m.role == "user"]
    said = msgs[users[-1]].text if users else ""
    after = msgs[users[-1] + 1:] if users else ()
    results = [m for m in after if m.role == "tool" and not (m.tool_call_id or "").startswith("ls-")]
    last = results[-1].text if results else ""
    calls = [c for m in after if m.role == "assistant" for c in m.tool_calls]
    CHAT_LOG.append({"said": said[-80:], "calls": [c.name for c in calls], "last": last[:300]})
    offered = {t.name for t in request.tools}

    def load(name: str, skill: str) -> ChatTurn | None:
        if name in offered or request.tool_choice == "none":
            return None
        return _tool("load_skill", f"ls-{name}-{len(calls)}", skills=[skill])

    # 开始 (learn_goal): new_goal ran in the turn; the tail is the Skill's one line
    if LEARN_GOAL_HEAD in said and not calls:
        n = len(re.findall(r"^\s*\d+\.\s|^- 「", said.split(LEARN_GOAL_HEAD, 1)[1], re.MULTILINE)) or len(bb.SIX)
        return ChatTurn(text=OPENING.format(n=n))
    # the diagnosis: ask_rough first, then next_check / ask_check until no pick is left (his answers
    # resume the turn that asked: earlier calls are in the whole conversation)
    ever = [c.name for m in msgs if m.role == "assistant" for c in m.tool_calls]
    head = DIAGNOSE_HEAD in said      # the turn called ask_rough itself: its result ends his message
    if head or "ask_check" in ever:
        done = [c for c in calls if c.name == "ask_check"]
        result = said.split(DIAGNOSE_HEAD, 1)[1] if head and not calls else last
        pick = _PICK.search(result)
        if pick is None:              # no pick left: where he starts
            told = "\n".join([said, *(m.text for m in msgs if m.role == "tool")])
            # the server names where he starts (check.start_line: the block the goal enters next); quoted as a model would
            named = re.findall(r"从「(.+?)」开始", told)
            start = named[-1] if named else ""
            return ChatTurn(text=f"从「{start}」开始，第一页在写。" if start else "这几块都会了。")
        m_name = _PICK_NAME.search(result)
        name = m_name.group(1) if m_name else ""
        if _SELF.search(result):
            return _tool("ask_check", f"a{len(done)}", pick_id=pick.group(1), self_report={
                "stem": f"「{name}」学过吗？", "options": ["学过，还会用", "学过，忘了", "没学过"],
                "means": ["know", "partly", "dont"]})
        stem, options, answer = DIAG.get(name, (f"「{name}」是讲什么的？", [f"{name}的基本做法", "和它无关的事", "一种工具"], 0))
        return _tool("ask_check", f"a{len(done)}", pick_id=pick.group(1),
                     quiz={"stem": stem, "options": options, "answer": answer})
    what = f"{request.purpose}: {said[-60:]!r}"
    script.unscripted.append(what)
    print(f"[diag_backend] UNSCRIPTED ASSISTANT TURN: {what}", file=sys.stderr, flush=True)
    raise Unscripted(what)


def assistant_gateway(script: FailClosedScript) -> Gateway:
    def complete(p):
        scripted = script._responder(p)

        def answer(model, request):
            # assistant.compact is a JSON call (compact@v7): an unusable (empty) reply, as before
            return "" if request.purpose == "assistant.compact" else scripted(model, request)
        return answer

    providers = {p: ScriptedChatProvider(p, lambda _m, r: assistant_turn(script, r),
                                         complete=complete(p)) for p in PROVIDERS}
    return Gateway(GW_CONFIG, script.log, mode="record", clock=lambda: T0, providers=providers,
                   record_max_attempts=1)


# A little existing knowledge (the demo's 统计 and 线性代数 regions), so the goal grows into a graph he has:
# topic → its points (part_of), prerequisites between points, K: 掌握 (declared know), 不会 (declared dont), none.
EXISTING = {
    "统计": [("st_mean", "均值", "know"), ("st_var", "方差", "know"), ("st_cov", "协方差", None)],
    "线性代数": [("la_vec", "向量", "know"), ("la_mul", "矩阵乘法", "know"), ("la_eig", "特征值与特征向量", "dont")],
}


def seed_wiki(pool, user: str) -> None:
    """GraphSaved with the two topics and their points, the points' K concepts and declarations."""
    from evowiki.belief.aspects import AddNote, Declare, DefineConcept
    from evowiki.goals.contracts import cell_id
    from evowiki.nodes import NodeGraph, NodeRec, Relation
    from evowiki.wikiapp.events import GraphSaved, WikiEvent
    from evowiki.wikiapp.repo import Commit
    seq = iter(range(1, 10_000))
    ids = lambda kind: f"seed_{kind}_{next(seq):04d}"
    now, aspects = T0, ("term", "purpose")
    nodes, rels, aspect = [], [], []
    for t, (topic, pts) in enumerate(EXISTING.items()):
        tid = f"topic_{t}"
        nodes.append(NodeRec(id=tid, name=topic, x=float(t), y=0.0))
        for i, (cid, name, said) in enumerate(pts):
            nodes.append(NodeRec(id=cid, name=name, x=float(t) + 0.1 * i, y=0.1))
            rels.append(Relation(kind="part_of", src=cid, dst=tid))
            if i > 0:
                rels.append(Relation(kind="prerequisite", src=pts[i - 1][0], dst=cid))
            aspect.append(DefineConcept(event_id=ids("aev"), ts=now, concept_id=cid, aspects=aspects))
            for a in aspects if said else ():
                c = cell_id(cid, a)
                aspect += [Declare(event_id=ids("aev"), ts=now, cell=c, value=said),
                           AddNote(event_id=ids("aev"), ts=now, cell=c, text=said, evidence_ref=f"declare:{c}")]
    repo = PgRepo(pool)
    repo.commit(repo.load(user), Commit(aspect=aspect, wiki=[WikiEvent(
        event_id=ids("wev"), ts=now, body=GraphSaved(graph=NodeGraph.of(nodes, rels), action="goal_nodes"))]))
    # each automatic group named after its topic (as graph group naming would)
    from evowiki.nodes import cluster, member_key
    from evowiki.wikiapp.events import GroupName, GroupsNamed
    snap = repo.load(user)
    topics = {f"topic_{t}": topic for t, topic in enumerate(EXISTING)}
    names = tuple(GroupName(key=member_key(g.members), name=next((topics[m] for m in g.members if m in topics), ""),
                            summary="", level=g.level, members=g.members) for g in cluster(snap.state.graph))
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=now,
                                             body=GroupsNamed(names=tuple(n for n in names if n.name)))]))


def build_script() -> FailClosedScript:
    s = FailClosedScript()
    s.always(OUTLINE_PURPOSE, intake_reply)
    s.always(PATH, path_reply)
    s.always(ORGANIZE, organize_reply)
    s.push(WRITE, *[{"text": f"回想：第 {i} 段\n\n这一段先用一个具体的例子讲清楚，再说它为什么这样。"}
                    for i in range(1, 121)])
    s.always("graph.group_naming", lambda _prompt: {"groups": []})
    return s


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--user", default="walk-user")
    a = ap.parse_args(argv)
    admin, _keep = start_admin_url()
    build_template(admin)
    url, _ = fresh_database(admin)
    pool = make_pool(url)
    ctx = seed_runtime(pool)
    make_user(pool, a.user)
    seed_wiki(pool, a.user)
    PgRsiStore(pool).put_settings(a.user, RsiSettings(cycle="manual"))   # TEST ONLY
    script = build_script()
    pace = float(os.environ.get("WRITE_SECTION_S", "0") or 0)
    if pace > 0:
        take = script.take
        script.take = lambda purpose, prompt="take": (time.sleep(pace) if purpose == WRITE else None, take(purpose, prompt))[1]
    clock, ids = SystemClock(), Ids()
    gateway = script.gateway()
    svc = Services(pool=pool, ctx=ctx, clock=clock, embedder=HashingEmbedder(), gateway=gateway, new_id=ids,
                   runs=InlinePageRuns(spawn=spawn_thread, store=PgPageRunStore(pool)),
                   page_writing=writing(PgRepo(pool), script))
    svc.sources = InlineSourceRuns(dataclasses.replace(import_deps(svc), llm_for=lambda _uid: None))
    def cancel_writes(user_id, page_ids):
        for row in svc.runs.store.live_of(user_id):
            if row.page_id in page_ids:
                svc.runs.discard(row.run_id, clock())

    svc.actions = ledger = ActionLedger(pool, clock, ids, {})
    ledger.register("goal_new", GoalNewExecutor(PgRepo(pool), lambda uid: require(gateway, uid),
                                                clock, ids, pool=pool, runs=lambda: None,
                                                cancel_writes=cancel_writes))
    svc.assistant = build_assistant(pool, assistant_gateway(script), ctx.content, svc.embedder,
                                    clock, ids, ledger=ledger)
    app = create_app(svc)
    mount_script_status(app, script)

    @app.get("/test/chat-turns")
    def _chat_turns() -> list[dict]:
        return CHAT_LOG
    print(json.dumps({"wiki_backend": f"http://127.0.0.1:{a.port}", "user": a.user}, ensure_ascii=False), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
