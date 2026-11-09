"""Disposable /wiki backend for the chat-first blocks walkthrough in REAL Obsidian (plan
2026-10-23-chat-first-blocks, task V; driver obsidian-plugin/scripts/real-obsidian/
blocks-walkthrough.mjs live). Test-only, modelled on tests/e2e/materials_backend.py.

Fresh Postgres database (bundled pgserver or EVOWIKI_TEST_DATABASE_URL — never the dev server),
the runtime fixture content, NO real LLM: every purpose is a scripted fake that answers from its
prompt, so the clicks may come in any order.

- assistant.turn (the 助手's chat turn, `assistant_turn`, assistant@v23's order): 开始's message
  (intent learn_goal, assistant@v24) has new_goal run by the turn itself, so its one model call
  is the tail below; a typed first message
  「我要学…」 (+ a 背景 line) → the new_goal(goal, words = [his 背景 line], notes) call with no text (the turn
  says the blocks call's framing and caveat before the card: new_goal's lead, the demo's two
  paragraphs), then — after the tool's answer — the main line, where to start and the question,
  worded from the tool result's lines (the demo's FIRST_REPLY tail); 「A和B合成一块」 → edit_goal_blocks(merge [A, B], name B), then 「可以，「A」并进了
  「B」，原来的…都在这一块里…」; the answer to the plan question: 「…了解 / 研究…」 →
  edit_goal_blocks(place 生长与繁殖 second) and the demo's sentence, 「…自己养…」 → no change, one
  sentence. Anything else is unscripted.
- goals.intake, S1 (intake@v32, 「把学习目标分成几大块」; one call per goal, it looks its draft
  over itself, D08): the demo's six 黑壳虾 blocks with levels (分类与鉴别〔基础〕, 水质与环境〔基础〕,
  开缸与日常〔核心〕, 营养与摄食〔核心〕, 生长与繁殖〔进阶〕, 疾病与风险〔进阶〕) and, for a chat card, the
  learner-facing lines (framing / caveat / mainline / start / question); his words saying 零基础 /
  完全没学过 are answered stated_beginner with those words (D07), 「尽快」 a limit 「尽快读到第一页」;
  读懂这篇 (「学习者要读懂这一篇资料」): one prerequisite block and the first three sections, one
  reading unit each (intake@v34).
  「其实不急」 in the chat → edit_goal_blocks(limits=[]); 「帮我读懂这篇」 with a PDF → new_goal on it.
- goals.intake for an entered block (intake@v26 scope text 「这是大目标「…」里的一块：「X」」, one
  level): X's covers as one page each (BLOCK_PAGES); a block not listed (one the learner added)
  gets one page of its own name. Every other goals.intake prompt is unscripted.
- goals.enter (S2, entering a block): answered by the same function (tests/wikiapp/script.py
  routes it to goals.intake's answer; no estimate scripted). goals.probe: tests.wikiapp.goal_script.write_item
  (the walkthrough clicks 跳过). goals.path: one page per listed concept, in the listed order
  (materials_backend.path_reply). graph.group_naming: no group names (the groups keep their
  fallback names).

The assistant's actions go through a ledger with the goal_new executor (the blocks → the goal map,
`create_goal_from_blocks`; the card's edited list → `revise`). Page writing is not part of this
walkthrough (生成第一页 is not clicked): the scripted writer answers from an empty queue (fails
closed if it is ever asked).

Fail closed: an unscripted LLM call raises wiki_backend's `Unscripted` (a BaseException) and is
listed by GET /test/wiki-script. The user (default `walk-user`) is created at start and printed as
JSON on the first stdout line. The RSI cycle is manual (as in wiki_backend).

`--copy-user U` (chat tails walkthrough, 2026-10-24): the fresh database first gets a COPY of U's
rows from the dev database (tests/e2e/copy_user.py: the source opened read-only, its URL from
the current checkout .env EVOWIKI_DATABASE_URL or EVOWIKI_COPY_SOURCE_URL), and the walkthrough runs as U over it.
An entered block's parts name which part builds on the first one (`recommended`, intake@v28's
line), so a 不知道 on the first part drops the questions on the others (probe §Failed below).
`WRITE_SECTION_S`: the scripted writer waits that long before each section (the page cards'
progress can be watched filling).

`--pending-pref TEXT` (个性化 walkthrough, 2026-10-28): after the user exists (copied or made), one
teaching-spec rule waiting for confirmation is written through the system actor's normal write path
(`commit_spec`, actor "system": rules/authority.py SYSTEM_ADDS allows exactly origin memory_backfill /
authority pending), with `--pending-why` as its evidence line, so 个性化 · 待确认的偏好 shows it.

  uv run python -m tests.e2e.blocks_backend --port 8761
"""

import argparse
import dataclasses
import json
import os
import re
import sys
import time
from datetime import UTC
from pathlib import Path

import uvicorn

from evowiki.assistant.loop import LEARN_GOAL_HEAD
from evowiki.bot.actions import ActionLedger
from evowiki.bot.change_source import LedgerChangeSource
from evowiki.bot.executors_assistant import GoalNewExecutor
from evowiki.clock import SystemClock
from evowiki.goals.intake import OUTLINE_PURPOSE
from evowiki.goals.path import PURPOSE as PATH
from evowiki.llm.chat import ChatRequest, ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.retrieval.embed import embedder_from_env
from evowiki.service.app import create_app
from evowiki.service.deps import (
    Services,
    build_assistant,
    graph_change_sources,
    import_deps,
    ledger_names,
    ledger_stale,
    register_page_actions,
)
from evowiki.sources.runs import InlineSourceRuns
from evowiki.store.db import make_pool
from evowiki.store.rsi import PgRsiStore
from evowiki.teach.contracts import RsiSettings
from evowiki.wikiapp.llm import require
from evowiki.wikiapp.page_run_store import PgPageRunStore
from evowiki.wikiapp.page_runs import InlinePageRuns, spawn_thread
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.write_queue import PgIdleEnv, WriteQueue
from tests.e2e.materials_backend import _SOURCE_ID, organize_reply, path_reply
from tests.e2e.wiki_backend import FailClosedScript, Unscripted, mount_script_status
from tests.pg import build_template, fresh_database, start_admin_url
from tests.runtime.helpers import T0, make_user, seed_runtime
from tests.runtime.llm_helpers import GW_CONFIG
from tests.wikiapp.e2e_driver import Ids
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.fake_writer import WRITE, writing
from tests.wikiapp.goal_script import PROBE, write_item
from tests.wikiapp.script import PROVIDERS

os.environ.setdefault("EVOWIKI_EMBEDDER", "hash")
# the PDF the chat-goal walkthrough attaches (a bookmarked outline; imported without a model)
PDF = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "outline.pdf"

# ---- the fake models ------------------------------------------------------------------------

# The approved demo's 黑壳虾 first reply (the product specification
# FIRST_REPLY) and its six blocks with levels (big-goal-demo.html TOPIC), as the one blocks call
# (intake@v29) answers them.
FRAMING = "这份大纲不是一份养虾教程，而是先把它认清楚，再把它养活，最后能自己判断出了什么问题。"
CAVEAT = ("先提醒一句：「黑壳虾」是水族店里的俗称，一般指新米虾属（Neocaridina）的一种，常和别的米虾混着卖，"
          "所以单开了「分类与鉴别」，放在最前面。")
NOTES = "「黑壳虾」是水族店里的俗称，一般指新米虾属（Neocaridina）的一种，常和别的米虾混着卖。"
MAINLINE = ("主线是 分类与鉴别 → 水质与环境 → 开缸与日常，「营养与摄食」可以和开缸一起学，"
            "「生长与繁殖」「疾病与风险」留到虾养稳以后。")
START = ["硝化", "开缸", "过水", "喂多少"]
QUESTION = ("你是想自己把虾养好，还是把它当成一种生物来研究？前一种会把操作排前面，"
            "后一种会多讲分类和生理。")
BLOCKS = {"title": "黑壳虾", "framing": FRAMING, "caveat": CAVEAT, "reference": "无", "blocks": [
    {"name": "分类与鉴别", "level": "基础", "summary": "水族店里叫黑壳虾的到底是哪种虾、怎么认出来",
     "covers": ["俗称指哪种虾", "和樱花虾水晶虾怎么分", "身体结构", "分公母"]},
    {"name": "水质与环境", "level": "基础", "summary": "虾要什么样的水、水为什么会坏",
     "covers": ["水温", "pH 和 GH / KH", "氨氮和亚硝酸盐", "硝化", "溶氧"]},
    {"name": "开缸与日常", "level": "核心", "summary": "从买缸到虾稳定下来，每天要做什么",
     "covers": ["缸和密度", "过滤和防吸虾", "开缸", "过水", "第一次换水"], "target": True},
    {"name": "营养与摄食", "level": "核心", "summary": "它平时吃什么、喂多少、为什么喂多了会坏水",
     "covers": ["生物膜和藻", "虾粮", "补钙", "喂多了为什么坏水"], "target": True},
    {"name": "生长与繁殖", "level": "进阶", "summary": "蜕皮、抱卵到小虾长大",
     "covers": ["蜕皮", "抱卵和孵化", "小虾怎么养"], "target": True},
    {"name": "疾病与风险", "level": "进阶", "summary": "虾不对劲、突然死虾时怎么查",
     "covers": ["应激信号", "蜕皮失败", "铜中毒", "突然死虾怎么查"], "target": True}],
    "mainline": MAINLINE, "start": START, "question": QUESTION}
# The card (the demo's six), by name.
SIX = {"分类与鉴别", "水质与环境", "开缸与日常", "营养与摄食", "生长与繁殖", "疾病与风险"}
CASCADE = {"title": "复杂网络的级联失效", "framing": "", "caveat": "", "reference": "无", "blocks": [
    {"name": "网络结构回顾", "level": "基础", "summary": "度分布、连通分量和网络的表示",
     "covers": ["度分布", "连通分量", "随机图"]},
    {"name": "负载与容量模型", "level": "基础", "summary": "节点的负载和容量怎么定义",
     "covers": ["介数负载", "容量冗余", "过载判据"]},
    {"name": "级联传播机制", "level": "核心", "summary": "一个节点失效后怎么连锁蔓延",
     "covers": ["失效传播", "阈值模型", "雪崩规模分布"]},
    {"name": "相依网络", "level": "核心", "summary": "两张网互相依赖时的突然崩溃",
     "covers": ["相依边", "互渗流", "一阶相变"]},
    {"name": "稳健性分析", "level": "进阶", "summary": "怎么衡量网络抗攻击的能力",
     "covers": ["随机失效", "蓄意攻击", "渗流阈值"]},
    {"name": "缓解与防护", "level": "进阶", "summary": "怎样减少级联带来的损失",
     "covers": ["负载重分配", "冗余设计", "关键节点保护"]},
    {"name": "电网案例", "level": "进阶", "summary": "真实电网里的大停电",
     "covers": ["电网建模", "大停电分析"]}]}
COVERS = {b["name"]: b["covers"] for b in BLOCKS["blocks"]}
# An entered block's pages (one level, intake@v26): its covers as one page each.
BLOCK_PAGES: dict[str, list[str]] = {name: list(COVERS[name]) for name in SIX}
_BLOCKS_CALL = "把学习目标分成几大块"        # intake@v32: the user's blocks prompt
# intake@v32: entering a block that is not atomic splits it one level (goals/intake ONE_LEVEL_SCOPE)
_ENTERED = re.compile(r"(?:里的一块|下面的一部分)「(.+?)」（id ")


def intake_reply(prompt: str) -> dict:
    """goals.intake: the blocks call, or one entered block's parts; anything else fails closed."""
    if _BLOCKS_CALL in prompt:
        # S1 (intake@v31): his words for the goal (the 「他为这个目标说的原话」 lines, or the goal line)
        line = re.search(r"学习目标：「(.*?)」", prompt, re.DOTALL)
        said = re.search(r"他为这个目标说的原话：.*?<<<资料内容开始>>>\n(.*?)\n<<<资料内容结束>>>",
                         prompt, re.DOTALL)        # fenced (intake@v31)
        words = "\n".join([said.group(1) if said else "", line.group(1) if line else ""])
        word = next((w for w in ("完全零基础", "零基础", "完全没学过") if w in words), "")
        limits = ["尽快读到第一页"] if "尽快" in words else []
        if "学习者要读懂这一篇资料" in prompt:       # 读懂这篇: prerequisites + the reading units
            labels = re.findall(r"^\s*(§\S+) ", prompt, re.MULTILINE)
            return {"title": "读懂这篇", "blocks": [
                {"name": "读之前要懂的", "level": "基础", "summary": "这篇用到、但没讲清楚的概念",
                 "covers": ["基本概念"]}], "limits": limits,
                "units": [{"title": x, "spans": [{"section": x}]} for x in labels[:3]],
                "stated_beginner": word != "", "beginner_quote": word}
        base = CASCADE if "级联" in words else BLOCKS
        reply = {k: v for k, v in base.items()
                 if '"framing"' in prompt or k not in ("framing", "caveat", "mainline", "start",
                                                     "question")}
        return {**reply, "limits": limits, "stated_beginner": word != "", "beginner_quote": word}
    m = _ENTERED.search(prompt)
    if m:
        name = m.group(1)
        pages = BLOCK_PAGES.get(name, [name])
        return {"parts": [      # each cover one point to learn and check on its own
            {"name": p, "summary": f"{p}是怎么回事、要注意什么", "atomic": True} for p in pages],
            "recommended": [{"from": pages[0], "to": p, "why": f"{p}是在{pages[0]}的基础上讲的"}
                            for p in pages[1:]]}
    raise Unscripted(f"goals.intake: {prompt[:80]!r}")


_MERGE = re.compile(r"(.+?)和(.+?)合成一块")
_LINE = re.compile(r"^- (主线|从哪里开始|问题)：(.+)$", re.MULTILINE)
_COUNT = "零一二三四五六七八九十"


def _tail(told: str) -> str:
    """After new_goal's result: the main line, where to start, the question — worded from the
    tool result's lines, as assistant@v23 asks (the demo's tail)."""
    got = dict(_LINE.findall(told))
    start = re.findall(r"「([^」]+)」", got.get("从哪里开始", ""))
    n = _COUNT[len(start)] if len(start) < len(_COUNT) else str(len(start))
    parts = [got.get("主线", ""),
             f"建议先从{''.join(f'「{x}」' for x in start)}这{n}个点开始。" if start else "",
             "" if got.get("问题", "没有要问的") == "没有要问的" else got["问题"]]
    return "\n\n".join(p for p in parts if p)


def assistant_turn(script: FailClosedScript, request: ChatRequest) -> ChatTurn:
    """assistant.turn (assistant@v23's order): 「我要学…」 → the new_goal call alone (its `notes` =
    what a web search found; the turn leads the card with the blocks call's framing + caveat),
    then the tail from the tool result;
    「A和B合成一块」 → edit_goal_blocks(merge, named B), then where A's contents are now; the
    answer to the plan question: 「…了解 / 研究…」 → edit_goal_blocks(place 生长与繁殖 second),
    「…自己养…」 → no change, one sentence; 「这页…」 with a 当前页 → read_page (no page_id), then an answer naming the
    page. Anything else is unscripted (fails closed)."""
    msgs = request.messages
    users = [i for i, m in enumerate(msgs) if m.role == "user"]
    said = msgs[users[-1]].text if users else ""
    CHAT_LOG.append({"version": request.prompt_version, "tool_choice": request.tool_choice,
                     "turn": len(users), "learn_goal": LEARN_GOAL_HEAD in said,
                     "said": said[-160:]})
    if "慢慢想" in said:            # a turn that takes a while (＋ while it runs opens a new conversation)
        time.sleep(SLOW_S)
        return ChatTurn(text="想好了：这一轮慢一些，是为了试一下中途开新对话。")
    after = msgs[users[-1] + 1:] if users else ()
    # stage 3: a load_skill call of this script (id "ls-…") is not a tool result to word
    told = "\n".join(m.text for m in after if m.role == "tool"
                     and not (m.tool_call_id or "").startswith("ls-"))
    offered = {t.name for t in request.tools}

    def needs(name: str, skill: str) -> ChatTurn | None:
        """A Skill tool this request does not offer yet: load its Skill first (as a model does)."""
        if name in offered or request.tool_choice == "none":
            return None
        return ChatTurn(tool_calls=(ToolCall(id=f"ls-{name}", name="load_skill",
                                             args={"skills": [skill]}),))
    if "这页" in said and "当前页：" in said:   # 「这页…」 with the turn's 当前页 (assistant@v42): read it, then answer
        if not told:
            return ChatTurn(tool_calls=(ToolCall(id="p1", name="read_page", args={}),))
        page = said.split("当前页：", 1)[1].split("》", 1)[0] + "》"
        return ChatTurn(text=f"我看了{page}：现在多是文字，可以在要动手的地方配几个组件。")
    if not told and LEARN_GOAL_HEAD in said:    # 开始 (intent learn_goal): the turn ran new_goal
        told = said.split(LEARN_GOAL_HEAD, 1)[1]    # itself; its result is in the user message
    # his message alone: the turn's preamble comes before 「他问：」, a loaded Skill's note is
    # appended after a blank line (agent/loop.py) — its examples must not read as his words
    carried = "他问：\n" in said
    said = said.rsplit("他问：\n", 1)[-1].split("\n\n", 1)[0]
    merge = _MERGE.search(said)
    if merge:
        a, b = merge.group(1).strip(), merge.group(2).strip()
        if told:
            had = "、".join(COVERS.get(a, [])[:3]) or "内容"
            return ChatTurn(text=f"可以，「{a}」并进了「{b}」，原来的{had}都在这一块里，排在前面讲。")
        if (load := needs("edit_goal_blocks", "plan-goal")) is not None:
            return load
        return ChatTurn(tool_calls=(ToolCall(id="e1", name="edit_goal_blocks", args={
            "ops": [{"op": "merge", "names": [b, a]}]}),))   # the first named stays (BlockEditArg)
    if re.search(r"了解|研究|原理", said) and "养" not in said and len(users) > 1:
        if told:
            return ChatTurn(text="可以，按研究这种生物来排：「分类与鉴别」「生长与繁殖」放到前面，"
                                 "开缸和日常照顾只讲到能看懂原理为止。")
        if (load := needs("edit_goal_blocks", "plan-goal")) is not None:
            return load
        return ChatTurn(tool_calls=(ToolCall(id="e2", name="edit_goal_blocks", args={
            "ops": [{"op": "place", "name": "生长与繁殖", "position": 2}]}),))
    if "其实不急" in said and len(users) > 1:      # his condition changed: limits only (S1)
        if told:
            return ChatTurn(text="好，那就不赶了：分块不变，以后讲得细一点。")
        return ChatTurn(tool_calls=(ToolCall(id="e3", name="edit_goal_blocks", args={
            "limits": []}),))
    if "帮我读懂这篇" in said and len(users) == 1:    # 读懂这篇: S1 in this turn, the reading card
        if told:
            return ChatTurn(text="卡片在上面，确认以后就按这篇来带读。")
        ids = _SOURCE_ID.findall("\n".join(m.text for m in msgs))
        if ids:
            return ChatTurn(tool_calls=(ToolCall(id="g3", name="new_goal", args={
                "goal": "帮我读懂这篇", "source_ids": [ids[-1]]}),))
    if "自己养" in said and len(users) > 1:
        return ChatTurn(text="可以，就按自己养来排，顺序不变；分类和生理只讲到能看懂操作为止。")
    if "帮我整理这一篇" in said and len(users) == 1:
        # a chat goal from an attached PDF (no words of a goal): new_goal with the attachment the
        # 「本会话的附件」 statement names, then the tail from the tool result
        if told:
            return ChatTurn(text=_tail(told))
        ids = _SOURCE_ID.findall("\n".join(m.text for m in msgs))
        if ids and (load := needs("new_goal", "plan-goal")) is not None:
            return load
        if ids:
            return ChatTurn(tool_calls=(ToolCall(id="g2", name="new_goal", args={
                "goal": "", "source_ids": [ids[-1]]}),))
    goal = re.search(r"(我要学[^\n]+)", said)
    # a new conversation may carry the last one's turns (cross-session carryover): then his
    # message comes after 「他问：」
    if goal and (len(users) == 1 or carried):
        if told:
            return ChatTurn(text=_tail(told))
        if (load := needs("new_goal", "plan-goal")) is not None:
            return load
        bg = re.search(r"背景：([^\n]+)", said)
        # S1: his own words for the goal, verbatim (the 背景 line he typed)
        return ChatTurn(tool_calls=(ToolCall(
            id="g1", name="new_goal", args={
                "goal": goal.group(1).strip(), "words": [bg.group(1).strip()] if bg else [],
                "notes": NOTES}),))
    what = f"{request.purpose}/{request.subject}: {said[-60:]!r}"
    script.unscripted.append(what)
    print(f"[blocks_backend] UNSCRIPTED ASSISTANT TURN: {what}", file=sys.stderr, flush=True)
    raise Unscripted(what)


CHAT_LOG: list[dict] = []          # every assistant.turn model call (GET /test/chat-turns)
SLOW_S = 6.0


def assistant_json(script: FailClosedScript, provider: str):
    """The assistant gateway's JSON calls: the script's, except the conversation summary.
    assistant.compact (compact@v7) is a JSON call; closing an earlier session (a copied user's,
    --copy-user, or the acceptance run's own) gets no summary: an unusable reply, which the
    product skips and logs — never a fake summary of real talk."""
    scripted = script._responder(provider)

    def answer(model, request):
        return "" if request.purpose == "assistant.compact" else scripted(model, request)
    return answer


def assistant_gateway(script: FailClosedScript) -> Gateway:
    """The assistant's gateway: the scripted chat turn above plus the same scripted JSON purposes
    (the blocks calls new_goal makes run on it)."""
    providers = {p: ScriptedChatProvider(p, lambda _m, r: assistant_turn(script, r),
                                         complete=assistant_json(script, p)) for p in PROVIDERS}
    return Gateway(GW_CONFIG, script.log, mode="record", clock=lambda: T0, providers=providers,
                   record_max_attempts=1)


_TASK_NAME = re.compile(r"^- (?:self_report|mastery|quiz)：(?:区域|知识点) (\S+) ([^（；：\n]+)",
                        re.MULTILINE)


def probe_reply(prompt: str) -> dict:
    """goals.probe: goal_script.write_item, with each node's name in the stem instead of its id
    (「学过养起来吗？」, as a model writes it)."""
    out = write_item(prompt)
    names = dict(_TASK_NAME.findall(prompt))
    for q in out["questions"]:
        for nid, name in sorted(names.items(), key=lambda x: -len(x[0])):   # c12 before c1
            q["stem"] = q["stem"].replace(nid, name.strip())
    return out


def build_script() -> FailClosedScript:
    s = FailClosedScript()
    s.always(OUTLINE_PURPOSE, intake_reply)
    s.always(PROBE, probe_reply)
    s.always(PATH, path_reply)
    # 生成第一页: the organizer's outline (three sections) and each section's text (the scripted
    # writer takes goals.write answers in order: enough for a few pages)
    s.always(ORGANIZE, organize_reply)
    s.push(WRITE, *[{"text": f"回想：第 {i} 段\n\n这一段先用一个具体的例子讲清楚，再说它为什么这样。"}
                    for i in range(1, 121)])
    s.always("graph.group_naming", lambda _prompt: {"groups": []})
    return s


def add_pending_pref(pool, user_id: str, text: str, why: str, new_id) -> str:
    """One rule waiting for the user's confirmation, written as Memory's backfill is: the system
    actor through `commit_spec` (the checkpoint allows a system write of memory_backfill / pending
    only). `new_id`: the backend's own ids (event ids are unique across the database). Returns its
    item id."""
    import uuid

    from evowiki.wikiapp import teachspec as tspec
    from evowiki.wikiapp.teach import commit_spec
    repo = PgRepo(pool)
    rid = f"mb_{uuid.uuid4().hex[:10]}"
    commit_spec(repo, repo.load(user_id), lambda s: tspec.add_item(
        s, item_id=rid, text=text, origin="memory_backfill", authority="pending", origin_ref="walkthrough"),
        actor="system", action="guideline", now=SystemClock()(), new_id=new_id,
        evidence={rid: why} if why.strip() else None)
    return rid


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8761)
    ap.add_argument("--user", default="walk-user")
    ap.add_argument("--copy-user", default=None, help="copy these users' rows (comma-separated) from the dev DB (read-only); run as the first")
    ap.add_argument("--pending-pref", default=None,
                    help="write one pending preference (Memory's backfill) for the user after start")
    ap.add_argument("--pending-why", default="你在几次对话里都这样说过",
                    help="the evidence line of --pending-pref (its ? in 个性化)")
    ap.add_argument("--read-writes", action="store_true",
                    help="a page-write queue that never writes, so GET /wiki/bot/recent lists the copied "
                         "page runs (个性化 → 活动记录's 「新页面」 rows) and GET /wiki/writes reads them")
    ap.add_argument("--write-delay", type=float, default=0.0,
                    help="seconds each page write waits first (a page stays `writing` that long)")
    a = ap.parse_args(argv)
    admin, _keep = start_admin_url()
    build_template(admin)
    url, _ = fresh_database(admin)
    pool = make_pool(url)
    ctx = seed_runtime(pool)
    if a.copy_user:
        from dotenv import dotenv_values

        from tests.e2e.copy_user import copy_user
        src = os.environ.get("EVOWIKI_COPY_SOURCE_URL") or dotenv_values(Path(__file__).resolve().parents[2] / ".env")["EVOWIKI_DATABASE_URL"]
        for u in a.copy_user.split(","):
            counts = copy_user(src, url, u)
            print(f"[blocks_backend] copied {u}: {sum(counts.values())} rows in {len(counts)} tables",
                  file=sys.stderr, flush=True)
        a.user = a.copy_user.split(",")[0]
    else:
        make_user(pool, a.user)
    PgRsiStore(pool).put_settings(a.user, RsiSettings(cycle="manual"))   # TEST ONLY (module doc)
    script = build_script()
    pace = float(os.environ.get("WRITE_SECTION_S", "0") or 0)
    if pace > 0:
        take = script.take
        script.take = lambda purpose, prompt="take": (time.sleep(pace) if purpose == WRITE else None, take(purpose, prompt))[1]
    clock = SystemClock()
    ids = Ids()
    if a.pending_pref:
        rid = add_pending_pref(pool, a.user, a.pending_pref, a.pending_why, ids)
        print(f"[blocks_backend] pending preference {rid} for {a.user}", file=sys.stderr, flush=True)
    gateway = script.gateway()
    # EVOWIKI_EMBEDDER=fastembed: the real local embedder (学点新的's places: similar groups near)
    svc = Services(pool=pool, ctx=ctx, clock=clock, embedder=embedder_from_env(),
                   gateway=gateway, new_id=ids,
                   runs=InlinePageRuns(spawn=spawn_thread, store=PgPageRunStore(pool)),
                   page_writing=writing(PgRepo(pool), script))
    # the assistant over the scripted chat turn; its goal_new rows through a ledger with the
    # goal_new executor (the blocks → the goal map)
    # chat attachments: a PDF uploaded in the 助手 is imported inline (no model: its bookmarks are the toc)
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
    # the change list (GET /wiki/rsi/changes) as the real server has it: the graph's changes and
    # the ledger's rows (个性化 → 活动记录 draws them, 撤销 undoes them)
    svc.change_sources = (*graph_change_sources(pool, ids),
                          LedgerChangeSource(ledger, ledger_stale(pool), ledger_names(pool)))
    if a.read_writes:
        svc.write_queue = WriteQueue(svc.runs, lambda: None, PgRepo(pool), clock, ids,
                                     PgIdleEnv(pool, UTC, ids), ledger=ledger,
                                     can_write=lambda _uid: False)
        # the page actions' executors as the real server has them (撤销 of a page change in 活动记录),
        # then the scripted goal_new again (the blocks → the goal map)
        register_page_actions(svc, ledger)
        ledger.register("goal_new", GoalNewExecutor(PgRepo(pool), lambda uid: require(gateway, uid),
                                                    clock, ids, pool=pool, runs=lambda: None,
                                                    cancel_writes=cancel_writes))
    if a.write_delay:
        svc.page_writing.writer.before_write = lambda: time.sleep(a.write_delay)
    app = create_app(svc)
    mount_script_status(app, script)

    @app.get("/test/chat-turns")
    def _chat_turns() -> list[dict]:
        return CHAT_LOG

    @app.post("/test/fail-next-write")
    def _fail_next_write() -> dict:
        """The next page write fails as an unavailable model would (organize → error), once."""
        script._queues[WRITE].appendleft("UNAVAILABLE")
        return {"armed": True}
    print(json.dumps({"wiki_backend": f"http://127.0.0.1:{a.port}", "user": a.user,
                      "pdf": str(PDF)}, ensure_ascii=False), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
