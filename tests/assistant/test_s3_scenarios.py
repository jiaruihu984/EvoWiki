"""Stage 3 phase 1 regression suite (T8b): the B9 scenarios, end to end through `run_turn` with a
SCRIPTED chat model, the in-memory session store and an in-memory ledger (no paid call, no
database; the real ledger tools on Postgres are tests/assistant/test_s3_real_ledger.py). Pass
conditions are checked by data (ledger rows, the session log, the tool lists of the requests),
never by reply text.

- S3-3 a draft bound by its own call in turn 1, 「好」 in turn 2 runs exactly that op
  (confirmed_by); a page-edit proposal bound the same way runs through its ledger row;
  a card nobody bound (a blocks card whose tail asks) is never confirmed by 「可以」; two ops →
  a pick, nothing before it; an op one message old has expired; a 「哪一个？」 keeps the ops for
  「第一个」;
- S3-4 after a task switch the old Skill's tools are gone, `undo` stays and finds the row;
- S3-6 a load error is a tool error and the turn answers; two_phase, a provider refusing a changed
  list mid-turn and a tool-search failure all give the same outcome as mid_turn_swap;
- S3-5: phase 2 (record_evidence + the self-report table);
- S3-1 / S3-7 on real Postgres (T6: land_preference); S3-2 nothing stored;
- the gate (and what it says to load), offer's availability check, H9 hooks under Skills, the
  memory tools by name, a durable turn = the plain one."""

import re
from types import SimpleNamespace
from typing import Any

from evowiki.agent.tools import ActionCard, ProposalDraft, ToolResult, ToolSpec
from evowiki.assistant import loop as loop_mod
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore, fold
from evowiki.assistant.tools.actions import (
    ActionDeps,
    Grounded,
    RememberAboutMeTool,
    UndoTool,
)
from evowiki.bot.executors_profile import KIND as PROFILE_FACT
from evowiki.bot.executors_profile import ProfileFactExecutor
from evowiki.harness.pack import HarnessPack
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.exposure import ToolExposure
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.profile.store import MemoryProfileStore
from tests.assistant.fakes import INFO, Data, FakeTool, Ids
from tests.assistant.memory_ledger import MemoryLedger, RecordingExecutor

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)


class DraftTool:
    """A draft-tier action tool as the ledger tools behave: a proposed confirm row, its card."""

    def __init__(self, ledger: MemoryLedger, name: str = "split_page", kind: str = "page_split"):
        self.ledger, self.kind = ledger, kind
        spec = ToolSpec(name=name, description=f"{name} 工具", effect="propose",
                        category="action", acts=True,
                        parameters={"type": "object",
                                    "properties": {"title": {"type": "string"},
                                                   "goal": {"type": "string"},
                                                   "words": {"type": "array"}}})
        self.spec = spec

    def run(self, args: dict, ctx) -> ToolResult:
        title = str(args.get("title") or args.get("goal") or "拆页")
        row = self.ledger.propose(ctx.user_id, kind=self.kind, scope="structure",
                                  source="assistant", summary=title, why="",
                                  content={"confirm": True, "title": title},
                                  key=f"k_{len(self.ledger.rows)}", level="ask")
        card = ActionCard(action_id=row.action_id, kind=self.kind, title=title,
                          status="proposed", confirm=True)
        return ToolResult(content=f"已生成草稿（{row.action_id}）：{title}\n- 问题：要自己养吗？",
                          action=card)


class ProposeFake:
    """propose_page_edit as the real one answers: a proposal draft on p1."""

    def __init__(self):
        from evowiki.agent.tool_helpers import spec
        from evowiki.assistant.tools.propose import Args
        self.spec = spec("propose_page_edit", "提议改页面", Args, "propose")

    def run(self, args: dict, ctx) -> ToolResult:
        return ToolResult(content="已生成", proposal=ProposalDraft(
            kind="add_section", page_id="p1", heading=args["heading"], text=args["text"],
            why=args["why"]))


class DirectFake:
    """A direct tool's inner part (Grounded wraps it): records its runs."""

    def __init__(self, name: str):
        self.calls: list[dict] = []
        self.spec = ToolSpec(name=name, description=f"{name} 工具", effect="propose",
                             category="action", acts=True,
                             parameters={"type": "object",
                                         "properties": {"what": {"type": "string"}}})

    def run(self, args: dict, ctx) -> ToolResult:
        self.calls.append(args)
        return ToolResult(content="已改")


class World:
    def __init__(self, tmp_path, script: list[Any], *, pack: HarnessPack | None = None):
        self.script = list(script)
        self.prov = ScriptedChatProvider("scripted", self._chat)
        gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                     providers={"scripted": self.prov})
        self.gw = gw.with_harness(lambda _uid: pack) if pack is not None else gw
        self.splits, self.goals = RecordingExecutor(), RecordingExecutor()
        self.profile = MemoryProfileStore()
        self.ledger = MemoryLedger({"page_split": self.splits, "goal_new": self.goals,
                                    PROFILE_FACT: ProfileFactExecutor(self.profile)})
        self.store = MemorySessionStore()
        self.store.create(INFO)
        adeps = ActionDeps(repo=SimpleNamespace(load=lambda uid: None),  # type: ignore[arg-type]
                           ledger=self.ledger, clock=lambda: INFO.created_at,  # type: ignore[arg-type]
                           new_id=Ids())
        self.bot = DirectFake("bot_settings")
        self.cards = DirectFake("make_cards")
        self.tools = [FakeTool("read_page", "read", ToolResult(content="页面大纲")),
                      FakeTool("read_path", "read", ToolResult(content="路径")),
                      DraftTool(self.ledger), DraftTool(self.ledger, "new_goal", "goal_new"),
                      RememberAboutMeTool(adeps), Grounded(UndoTool(adeps)),
                      Grounded(self.bot), Grounded(self.cards)]
        self.deps = TurnDeps(gateway=self.gw, tools=self.tools, store=self.store, data=Data(),
                             new_id=Ids(), clock=lambda: INFO.created_at,
                             ledger=self.ledger)

    def _chat(self, model, req):
        out = self.script.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    def turn(self, text: str, **msg) -> list:
        return list(run_turn(self.deps, INFO, MessageIn(text=text, **msg)))

    def sent_tools(self, i: int) -> list[str]:
        return [t.name for t in self.prov.chat_calls[i].tools]

    def state(self):
        return fold(INFO, self.store.entries(INFO.session_id))


def call(name: str, i: int, **args) -> ChatTurn:
    return ChatTurn(tool_calls=(ToolCall(id=f"c{i}", name=name, args=args),))


def say(text: str) -> ChatTurn:
    return ChatTurn(text=text)


def tool_entries(w: World, name: str) -> list[dict]:
    return [e.payload for e in w.store.entries(INFO.session_id)
            if e.type == "tool" and e.payload["name"] == name]


# ---- the gate and loading ----

def test_a_tool_not_offered_is_refused_and_says_which_reference_brings_it(tmp_path):
    w = World(tmp_path, [call("split_page", 1, title="甲"), say("好的")])
    ev = w.turn("把这页拆开")
    assert ev[-1].type == "done"
    assert "split_page" not in w.sent_tools(0) and w.ledger.rows == {}
    [entry] = tool_entries(w, "split_page")
    assert not entry["ok"] and "先 read_ref edit-wiki/path" in entry["content"]


def test_after_read_ref_the_next_request_carries_the_references_tools(tmp_path):
    w = World(tmp_path, [call("read_ref", 1, path="edit-wiki/path"),
                         call("split_page", 2, title="甲"), say("草稿在卡片上")])
    w.turn("把这页拆开")
    assert "split_page" not in w.sent_tools(0) and "read_ref" in w.sent_tools(0)
    assert "split_page" in w.sent_tools(1)
    assert [r.status for r in w.ledger.rows.values()] == ["proposed"]
    params = w.prov.chat_calls[1].params
    assert params["tool_exposure"] == "mid_turn_swap" and "edit-wiki/path" in params["skills"]
    assert len(params["skills_library"]) == 8
    # the reference is the system's own rules: never fenced as material (found in the walk)
    [ref] = [m for m in w.prov.chat_calls[1].messages if m.role == "tool"]
    assert ref.text.startswith("【参考·edit-wiki/path】")
    assert w.state().skills == ("edit-wiki",)              # used this turn: carried
    # the catalog names the reference with its tools: a change behind it is one read_ref away
    assert "edit-wiki/pages（propose_page_edit" in w.prov.chat_calls[0].system


def test_page_rewrite_instructions_name_an_offered_tool(tmp_path):
    from evowiki.assistant.skills import library

    w = World(tmp_path, [call("read_ref", 1, path="edit-wiki/pages"), say("请看修改草稿")])
    w.tools.extend([ProposeFake(), FakeTool("revise_page", "propose"),
                    FakeTool("illustrate_section", "propose"), FakeTool("add_source", "propose")])
    w.turn("换个讲法讲这一节")
    [entry] = tool_entries(w, "read_ref")
    recommended = re.findall(r"用 ([a-z][a-z_]+)", entry["content"])
    assert recommended and set(recommended) <= set(library().ref("edit-wiki/pages").tools)
    assert set(recommended) <= set(w.sent_tools(1))
    assert 'kind="rewrite_section"' in entry["content"]


def test_a_reference_read_in_an_earlier_turn_is_named_not_resent(tmp_path):
    w = World(tmp_path, [call("read_ref", 1, path="edit-wiki/path"),
                         call("split_page", 2, title="甲"), say("草稿"), say("嗯")])
    w.turn("把这页拆开")
    w.turn("这个是什么意思")
    system = w.prov.chat_calls[3].system
    assert "读过的参考：edit-wiki/path" in system
    assert "covers 写这一页回答的那个问题" not in system        # the body is not resent
    assert "split_page" in w.sent_tools(3)                     # its tools stay registered


# ---- S3-3: 「好」 confirms the one bound op ----

def _propose(w: World, n: int = 1) -> None:
    calls = [call("read_ref", 1, path="edit-wiki/path"),
             ChatTurn(tool_calls=tuple(ToolCall(id=f"d{i}", name="split_page",
                                                args={"title": f"拆法{i}"})
                                       for i in range(1, n + 1)))]
    w.script[:0] = [*calls, say("草稿在卡片上，等你确认。")]
    w.turn("帮我拆一下这页")


def test_s3_3_one_bound_op_and_ok_runs_exactly_it_with_its_evidence(tmp_path):
    w = World(tmp_path, [])
    _propose(w)
    [op] = w.state().turns[-1].pending_ops
    assert op["kind"] == "ledger" and op["action_id"] == "act_1"
    assert op["proposed_in"]["role"] == "assistant"
    w.script += [call("confirm", 3, pending_id=op["pending_id"], asked="好"), say("已经拆好了。")]
    w.turn("好")
    assert op["pending_id"] in w.prov.chat_calls[-2].messages[-1].text     # the ops line
    assert w.splits.executed == ["act_1"]
    row = w.ledger.rows["act_1"]
    assert row.status == "executed"
    first, second = row.result["confirmed_by"]
    assert first["role"] == "assistant" and first["message_id"] == op["proposed_in"]["message_id"]
    mid = w.state().turns[-1].message_id
    assert second == {"message_id": mid, "session_id": "s1", "role": "user", "quote": "好"}


def test_s3_3_the_draft_call_itself_binds(tmp_path):
    """S3 live run 2026-10-28: DeepSeek never called offer after split_page, so 「好」 confirmed
    nothing. The draft call itself binds it: no second call."""
    w = World(tmp_path, [])
    _propose(w)
    assert [m.name for c in w.prov.chat_calls for m in c.tools if m.name == "offer"]  # still there
    names = [e.payload["name"] for e in w.store.entries("s1") if e.type == "tool"]
    assert names == ["read_ref", "split_page"]                       # no offer call
    [split] = tool_entries(w, "split_page")
    assert "他下一条回「好」就确认这张草稿（po_act_1）" in split["content"]
    w.script += [call("confirm", 3, pending_id="po_act_1", asked="好"), say("拆好了。")]
    w.turn("好")
    assert w.splits.executed == ["act_1"]


def test_h4_the_blocks_card_never_becomes_an_op(tmp_path):
    """The goal-blocks tools bind nothing, and 「可以」 approves nothing."""
    w = World(tmp_path, [call("load_skill", 1, skills=["plan-goal"]),
                         call("new_goal", 2, goal="黑壳虾"),
                         say("主线是先认识它。你是想自己养吗？")])
    w.turn("我要学黑壳虾")
    assert w.state().turns[-1].pending_ops == ()
    w.script += [call("confirm", 3, pending_id="po_act_1", asked="可以"), say("好")]
    w.turn("可以")
    assert w.goals.executed == [] and w.ledger.rows["act_1"].status == "proposed"


def test_s3_3_a_page_edit_proposal_gets_its_ledger_row_only_at_his_ok(tmp_path):
    w = World(tmp_path, [])
    edits = RecordingExecutor()
    w.ledger.register("assistant_edit", edits)
    w.tools.append(ProposeFake())
    w.script += [call("read_ref", 1, path="edit-wiki/pages"),
                 call("propose_page_edit", 2, kind="add_section", heading="补充", text="正文",
                      why="刚问过"), say("加一段，回「好」就加进去。")]
    w.turn("把这个加进页面")
    [op] = w.state().turns[-1].pending_ops
    assert op["kind"] == "proposal" and not w.ledger.rows      # a draft nobody decides leaves no row
    w.script += [call("confirm", 3, pending_id=op["pending_id"], asked="好"), say("加好了。")]
    ev = w.turn("好")
    [row] = w.ledger.rows.values()
    assert (row.kind, row.content["session_id"]) == ("assistant_edit", "s1")
    assert edits.executed == [row.action_id]
    assert not any(e.type == "action" for e in ev)        # its proposal card is the card


def test_plan_goal_is_preloaded_for_learn_goal_and_an_open_blocks_draft(tmp_path):
    """The structured signals that offer the blocks tools from the first call: 学点新的 (learn_goal)
    and, in the next turns, the session's open blocks draft; a plain 「我要学…」 has neither."""
    w = World(tmp_path, [say("主线是先认识它。"), say("好，合成一块。")])
    w.turn("我要学黑壳虾", intent="learn_goal", goal="黑壳虾")
    assert "new_goal" in w.sent_tools(0)
    w.turn("分类和水质合成一块")                         # the blocks draft is still open
    assert "new_goal" in w.sent_tools(1)
    plain = World(tmp_path / "plain", [say("要我帮你排一下吗？")])
    plain.turn("我要学黑壳虾")
    assert "new_goal" not in plain.sent_tools(0)


def test_s3_3_a_draft_is_bound_for_ok_without_the_model_asking(tmp_path):
    """Haiku set no switch, so 「好」 got 「我没法替你点」: the harness binds the draft itself."""
    w = World(tmp_path, [])
    _propose(w)
    assert [o["pending_id"] for o in w.state().turns[-1].pending_ops] == ["po_act_1"]


def test_s3_3_the_blocks_card_with_a_question_is_not_approved_by_ok(tmp_path):
    """A learn_goal turn: the blocks card, then the tail asks a question; 「可以」 answers the
    question, it approves nothing."""
    w = World(tmp_path, [say("主线是先认识它。你是想自己养吗？"),
                         call("confirm", 2, pending_id="po_act_1", asked="可以"), say("好")])
    w.turn("我要学黑壳虾", intent="learn_goal", goal="黑壳虾")
    assert [r.status for r in w.ledger.rows.values()] == ["proposed"]
    assert w.state().turns[-1].pending_ops == ()
    w.turn("可以")
    assert w.goals.executed == [] and w.ledger.rows["act_1"].status == "proposed"


def test_s3_3_two_bound_ops_pause_on_a_pick_and_run_only_the_picked_one(tmp_path):
    """The pick ends the turn (its card); the option he clicks is his next message, which
    resumes the confirm call."""
    w = World(tmp_path, [])
    _propose(w, 2)
    assert len(w.state().turns[-1].pending_ops) == 2
    w.script += [call("confirm", 3, pending_id="po_act_1", asked="好")]
    evs = w.turn("好")
    [ask] = [e for e in evs if e.type == "ask"]
    assert ask.kind == "confirm_pick" and ask.payload["options"] == ["1. 拆法1", "2. 拆法2", "都不是"]
    assert evs[-1].type == "done" and w.splits.executed == []   # nothing ran before his pick
    calls = len(w.prov.chat_calls)
    w.script += [say("好了")]
    w.turn("2. 拆法2")
    assert w.splits.executed == ["act_2"] and len(w.prov.chat_calls) == calls + 1
    assert w.state().pending is None


def test_s3_3_none_of_them_runs_nothing(tmp_path):
    w = World(tmp_path, [])
    _propose(w, 2)
    w.script += [call("confirm", 3, pending_id="po_act_1", asked="好")]
    w.turn("好")
    w.script += [say("那先不动")]
    w.turn("都不是")
    assert w.splits.executed == []


def test_s3_3_which_one_asked_in_text_keeps_the_ops_for_the_answer(tmp_path):
    """M3: two ops, the answer asks which one in text (no confirm call); 「第一个」 next turn
    resolves."""
    w = World(tmp_path, [])
    _propose(w, 2)
    w.script += [say("你是说哪一个？")]
    w.turn("好")
    assert w.splits.executed == []
    kept = w.state().turns[-1].pending_ops
    assert [o["pending_id"] for o in kept] == ["po_act_1", "po_act_2"]
    assert all(o["reasked"] for o in kept)
    w.script += [call("confirm", 4, pending_id="po_act_1", asked="第一个"), say("好了")]
    w.turn("第一个")
    assert w.splits.executed == ["act_1"]


def test_s3_3_an_op_from_two_messages_ago_has_expired(tmp_path):
    w = World(tmp_path, [])
    _propose(w)
    w.script += [say("z 是标准分。")]
    w.turn("z 是什么")
    w.script += [call("confirm", 3, pending_id="po_act_1", asked="好"), say("现在没有要确认的")]
    w.turn("好")
    assert w.splits.executed == []
    [entry] = tool_entries(w, "confirm")
    assert not entry["ok"] and "没有等他确认的操作" in entry["content"]


def test_m1_a_direct_tool_of_a_skill_not_loaded_cannot_be_offered(tmp_path):
    """The bot_settings bypass: offer would let 「好」 run a settings tool nobody loaded."""
    w = World(tmp_path, [call("offer", 1, tool="bot_settings", args={"what": "卡少一点"},
                              title="把卡改少"), say("要我改吗？")])
    w.turn("卡太多了")
    [entry] = tool_entries(w, "offer")
    assert not entry["ok"] and "load_skill settings" in entry["content"]
    assert w.state().turns[-1].pending_ops == ()
    w2 = World(tmp_path / "b", [call("load_skill", 1, skills=["settings"]),
                                call("offer", 2, tool="bot_settings", args={"what": "卡少一点"},
                                     title="把卡改少"), say("要我改吗？"),
                                call("confirm", 3, pending_id="po_msg_0001_1", asked="好"),
                                say("改好了")])
    w2.turn("卡太多了")
    [op] = w2.state().turns[-1].pending_ops
    assert "卡少一点" in op["title"]                       # the title carries the key args
    w2.script[0] = call("confirm", 3, pending_id=op["pending_id"], asked="好")
    w2.turn("好")
    assert w2.bot.calls == [{"what": "卡少一点"}]


# ---- S3-4: lifecycle ----

def test_s3_4_after_a_task_switch_the_skill_tools_are_gone_and_undo_finds_the_row(tmp_path):
    w = World(tmp_path, [
        call("load_skill", 1, skills=["remember"]),
        call("remember_about_me", 2, applies="lasting", section="我是谁", quote="我是护士",
             text="护士"),
        say("记下了。")])
    w.turn("我是护士")
    assert "remember_about_me" in w.sent_tools(1)
    assert w.profile.get("u1").owned["我是谁"] == ("护士",)
    w.script += [say("z 是标准分。")]
    w.turn("z 是什么")
    assert "remember_about_me" in w.sent_tools(3)          # carried one more turn (used in turn 1)
    w.script += [call("undo", 5, action_id="last", asked="撤销刚才那个"), say("已撤销。")]
    w.turn("撤销刚才那个")
    turn3 = w.sent_tools(4)
    assert "remember_about_me" not in turn3 and "undo" in turn3    # task over; undo stays
    assert "act_1" in w.prov.chat_calls[4].messages[-1].text        # the ops line lists it
    assert w.ledger.rows["act_1"].status == "undone"
    assert w.profile.get("u1").owned.get("我是谁", ()) == ()


def test_m6_undo_is_offered_for_any_undoable_row_of_the_user(tmp_path):
    """A row this session's log never saw (another session's) still makes undo available, but the
    ops line lists only this session's rows."""
    w = World(tmp_path, [say("可以撤销")])
    w.ledger.record("u1", kind=PROFILE_FACT, scope="suggestion", source="assistant",
                    summary="记下了：护士", content={"section": "我是谁", "text": "护士"},
                    key="assistant:turn:s0:msg_9:3")
    w.turn("能撤销吗")
    assert "undo" in w.sent_tools(0)
    assert "act_1" not in w.prov.chat_calls[0].messages[-1].text


def test_s3_4_a_bare_undo_acts_only_on_this_sessions_row(tmp_path):
    """Live S3 run: 「撤销」 in session B undid session A's split. A bare undo finds nothing in a
    session that changed nothing; the other session's row is listed with its session and undone
    only when named."""
    from evowiki.assistant.session import SessionInfo
    w = World(tmp_path, [
        call("load_skill", 1, skills=["remember"]),
        call("remember_about_me", 2, applies="lasting", section="我是谁", quote="我是护士",
             text="护士"),
        say("记下了。")])
    w.turn("我是护士")                                     # session s1 writes act_1
    other = SessionInfo(session_id="s2", user_id="u1", page_id="p9", note_concept_id=None,
                        created_at=INFO.created_at)
    w.store.create(other)
    # s1 was one short turn: the new session does not pay for a summary of it
    w.script += [call("undo", 3, asked="撤销"), call("undo", 4, other_sessions=True, asked="撤销"),
                 say("这次对话里没有改动；上次对话里记过「护士」，要撤销那个吗？")]
    list(run_turn(w.deps, other, MessageIn(text="撤销")))
    assert w.ledger.rows["act_1"].status == "executed"
    first = w.prov.chat_calls[3]                          # s2's first turn call
    assert "act_1" not in first.messages[-1].text         # its ops line
    assert "undo" in [t.name for t in first.tools]
    bare, listed = [e.payload for e in w.store.entries("s2")
                    if e.type == "tool" and e.payload["name"] == "undo"]
    assert not bare["ok"] and "这次对话里没有可以撤销的动作" in bare["content"]
    assert listed["ok"] and "act_1「" in listed["content"] and "对话 s1" in listed["content"]
    w.script += [call("undo", 5, action_id="act_1", asked="撤销上次那条"), say("已撤销。")]
    list(run_turn(w.deps, other, MessageIn(text="撤销上次那条")))
    assert w.ledger.rows["act_1"].status == "undone"


def test_make_cards_is_a_core_tool_and_keeps_its_grounding(tmp_path):
    """「考考我这页」 on a page without cards must not wait for a Skill load the model may skip: make_cards is
    offered from the first request; its `asked` check stays."""
    w = World(tmp_path, [call("make_cards", 1, what="z 分数", asked="考考我"), say("题在下面。")])
    w.turn("考考我")
    assert "make_cards" in w.sent_tools(0) and "load_skill" in w.sent_tools(0)
    assert w.cards.calls == [{"what": "z 分数"}]
    assert w.state().skills == ()                     # it carries no Skill into the next turn
    w.script += [call("make_cards", 2, what="z 分数", asked="再考我一次吧"), say("好")]
    w.turn("嗯")
    [_, refused] = tool_entries(w, "make_cards")
    assert not refused["ok"] and len(w.cards.calls) == 1        # not his words: nothing ran


# ---- S3-6: fallbacks ----

def test_s3_6_a_load_error_is_a_tool_error_and_the_turn_still_answers(tmp_path):
    w = World(tmp_path, [call("load_skill", 1, skills=["nope"]), say("这一步没做成。")])
    ev = w.turn("记住这个")
    assert ev[-1].type == "done"
    [entry] = tool_entries(w, "load_skill")
    assert not entry["ok"] and "nope" in entry["content"] and "remember" in entry["content"]
    assert w.sent_tools(1) == w.sent_tools(0)               # nothing was loaded


def _scripted_split() -> list[Any]:
    return [call("read_ref", 1, path="edit-wiki/path"), call("split_page", 2, title="甲"),
            say("草稿在卡片上。")]


def _outcome(w: World) -> tuple:
    calls = [(e["name"], e["ok"]) for e in (e.payload for e in w.store.entries("s1")
                                             if e.type == "tool")]
    answer = next(e.payload["text"] for e in w.store.entries("s1") if e.type == "assistant")
    return calls, answer, [r.status for r in w.ledger.rows.values()]


def test_s3_6_two_phase_gives_the_same_outcome_as_mid_turn_swap(tmp_path, monkeypatch):
    swap = World(tmp_path / "a", _scripted_split())
    swap.turn("把这页拆开")
    monkeypatch.setattr(loop_mod, "_exposure",
                        lambda *a, **k: (ToolExposure.two_phase, ""))
    two = World(tmp_path / "b", _scripted_split())
    two.turn("把这页拆开")
    assert _outcome(two) == _outcome(swap)
    req = two.prov.chat_calls[1]
    assert req.params["tool_exposure"] == "two_phase" and "split_page" in [t.name for t in req.tools]
    assert all(m.role != "tool" for m in req.messages)       # the load call was folded
    assert "已载入：" in req.messages[-1].text and "【参考·edit-wiki/path】" in req.messages[-1].text


class _BadRequest(Exception):
    """An SDK BadRequestError as the providers let it through: HTTP 400 and the provider text."""

    def __init__(self, message: str):
        super().__init__(message)
        self.status_code = 400
        self.body = {"error": {"message": message}}


def test_s3_6_a_provider_refusing_a_changed_tool_list_is_downgraded_keeping_the_tools(tmp_path):
    swap = World(tmp_path / "a", _scripted_split())
    swap.turn("把这页拆开")
    refused = _BadRequest("messages.1.content.0: Invalid `signature` in `thinking` block")
    w = World(tmp_path / "b", [call("read_ref", 1, path="edit-wiki/path"), refused,
                               call("split_page", 2, title="甲"), say("草稿在卡片上。")])
    w.turn("把这页拆开")
    assert _outcome(w) == _outcome(swap)
    retry = w.prov.chat_calls[-2]
    assert retry.params["tool_exposure"] == "two_phase"
    assert retry.params.get("exposure_reason") == "tool_list_rejected"
    assert "split_page" in [t.name for t in retry.tools]        # given mid-turn, kept (M10)


def test_s3_6_an_unrelated_400_is_not_a_downgrade(tmp_path):
    w = World(tmp_path, [call("read_ref", 1, path="edit-wiki/path"),
                         _BadRequest("max_tokens: must be at most 8192")])
    ev = w.turn("把这页拆开")
    assert ev[-1].type == "error"


def test_s3_6_a_tool_search_failure_retries_the_call_two_phase(tmp_path, monkeypatch):
    swap = World(tmp_path / "a", _scripted_split())
    swap.turn("把这页拆开")
    monkeypatch.setattr(loop_mod, "_exposure",
                        lambda *a, **k: (ToolExposure.native_deferred, ""))
    w = World(tmp_path / "b", _scripted_split())
    w.turn("把这页拆开")
    assert _outcome(w) == _outcome(swap)
    first = w.prov.chat_calls[0]                 # the deferred request never reached a provider
    assert first.params["tool_exposure"] == "two_phase" and not first.deferred


# ---- the scenarios whose code side is a later task ----

RULE = "我喜欢组件多一点的页面"
LIKES = "这页没有组件，我喜欢组件多一点的页面"


def _request_text(req) -> str:
    return req.system + "\n".join(m.text or "" for m in req.messages)


def test_s3_1_a_preference_remembered_in_one_session_teaches_the_next_once(tmp_path, pool):
    """Real Postgres + the real tools (tests/assistant/test_stated_preferences.py's env): session
    s1 remembers it (load the remember Skill, then remember_preference, his words of this
    message); session s2's request carries it exactly once (the principles list; the profile
    block leaves 怎么学比较好 to the spec), and the page writer reads it."""
    from dataclasses import replace as dc_replace

    from evowiki.assistant.wiki import WikiData
    from evowiki.profile.context import context_for
    from evowiki.profile.derive import derived
    from evowiki.wikiapp.refine_apply import refine_input
    from evowiki.wikiapp.refine_spec import WikiSpecSource
    from tests.assistant.test_action_tools import INFO as AINFO
    from tests.assistant.test_action_tools import chat_turns, turn_deps
    from tests.assistant.test_stated_preferences import own_items, prefs_env
    from tests.wikiapp.test_review_flow import LATER
    env = prefs_env(pool)
    prov = chat_turns(call("load_skill", 1, skills=["remember"]),
                      call("remember_preference", 2, applies="lasting",
                           quote=RULE, text=RULE), say("记下了。"),
                      say("z 分数是标准化以后的分数。"))
    deps = dc_replace(turn_deps(tmp_path, env, prov, list(env.tools.values())),
                      data=WikiData(env.repo, lambda: LATER))
    list(run_turn(deps, AINFO, MessageIn(text=LIKES)))
    [item] = own_items(env)
    mid = fold(AINFO, deps.store.entries(AINFO.session_id)).turns[-1].message_id
    assert (item.text, item.origin, item.authority, item.origin_ref) == (
        RULE, "user_chat", "user", f"s1/{mid}")
    s2 = AINFO.model_copy(update={"session_id": "s2"})
    store2 = MemorySessionStore()             # a session of its own (no carry-over call)
    store2.create(s2)
    list(run_turn(dc_replace(deps, store=store2), s2, MessageIn(text="z 分数是什么")))
    req = prov.chat_calls[-1]
    assert req.subject == "s2"
    # as a rule it reaches the model once: the principles list, not also the profile block (the
    # ops line names the undoable row 「记下了：…」 by its title, which is no rule)
    principles = _request_text(req).split("为他组织内容的原则", 1)[1]
    profile_block = context_for(derived(env.repo.load("u1"), LATER), "assistant.turn")
    assert principles.count(RULE) == 1 and profile_block.count(RULE) == 0
    assert _request_text(req).replace(f"记下了：{RULE}", "").count(RULE) == 1
    inp = refine_input(env.repo.load("u1"), env.pid, LATER, WikiSpecSource(env.repo),
                       mode="rewrite")
    assert [i.text for i in inp.teaching.items].count(RULE) == 1


def test_s3_2_a_this_task_request_is_never_stored(tmp_path):
    w = World(tmp_path, [call("load_skill", 1, skills=["remember"]),
                         call("remember_about_me", 2, section="我是谁", quote="这道题讲简单点",
                              text="讲简单点", applies="this_task"),
                         say("好，简单讲。")])
    w.turn("这道题讲简单点")
    [entry] = tool_entries(w, "remember_about_me")
    assert entry["ok"] and w.ledger.rows == {}          # refused politely, nothing written


def test_s3_5_i_know_this_records_self_evidence_only(tmp_path):
    """「我会这个」 on a page: the scripted model records ONE typed self-report (can_do) on the cell it
    names; no Declare, no cell becomes known, the other aspects get nothing, no path is touched,
    and no other model call is made (stage 3 T9 / T10)."""
    from evowiki.assistant.session import SessionInfo
    from evowiki.assistant.tools.evidence import RecordEvidenceTool
    from evowiki.assistant.wiki import WikiData, WikiTools
    from evowiki.belief.aspects import Declare, Observe
    from evowiki.goals.contracts import Band
    from evowiki.goals.selfreport import SELF_CLAIM
    from evowiki.wikiapp.repo import MemoryRepo
    from tests.assistant.test_wiki_adapter import gateway
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_goal import NOW, Ids
    from tests.wikiapp.test_page import organized

    env = (MemoryRepo(["u1"]), Script(), Ids())
    repo, ids = env[0], env[2]
    pid = organized(env)
    k0 = repo.load("u1").kview(NOW)
    wiki_before = len(repo.wiki_log("u1"))
    gw, prov = gateway(tmp_path, [
        ChatTurn(tool_calls=(ToolCall(id="c1", name="record_evidence", args={"events": [
            {"cell": "c1.procedure", "kind": "self", "self_claim": "can_do",
             "quote": "我会这个"}]}),)),
        ChatTurn(text="好，那这一段可以快点过。")])
    info = SessionInfo(session_id="s1", user_id="u1", page_id=pid, note_concept_id=None,
                       created_at=NOW)
    store = MemorySessionStore()
    store.create(info)
    wiki = WikiTools(repo)
    deps = TurnDeps(gateway=gw, tools=lambda i, now: [*wiki(i, now),
                                                      RecordEvidenceTool(repo, lambda: NOW, ids)],
                    store=store, data=WikiData(repo, lambda: NOW), new_id=ids, clock=lambda: NOW)
    ev = list(run_turn(deps, info, MessageIn(text="这个我会这个")))
    assert ev[-1].type == "done" and len(prov.chat_calls) == 2 and prov.calls == []
    obs = [e.observation for e in repo.aspect_log("u1") if isinstance(e, Observe)
           and e.observation.evidence_ref.startswith("assistant:")]
    assert [(o.cell, o.kind, o.polarity, o.strength) for o in obs] == [
        ("c1.procedure", "self", *SELF_CLAIM["can_do"])]
    assert not any(isinstance(e, Declare) for e in repo.aspect_log("u1"))
    kv = repo.load("u1").kview(NOW)
    assert all(kv.cell(c).band is not Band.KNOWN for c in ("c1.procedure", "c1.interpret"))
    assert kv.cell("c1.interpret") == k0.cell("c1.interpret")          # not every aspect
    assert len(repo.wiki_log("u1")) == wiki_before                     # no path / page change


def test_s3_7_a_conflicting_preference_supersedes_the_old_one(pool):
    """Old and new conflict: both records and their sources are kept, only the new one teaches,
    and undo puts the old one back."""
    from evowiki.wikiapp.teachspec import page_teach
    from tests.assistant.test_stated_preferences import ctx, own_items, prefs_env, remember
    env = prefs_env(pool)
    remember(env, ctx("以后都先给例子再讲定义"), quote="以后都先给例子再讲定义",
             text="以后都先给例子再讲定义")
    [old] = own_items(env)
    res = remember(env, ctx("算了，以后都先讲定义再举例", mid="m2"), quote="以后都先讲定义再举例",
                   text="以后都先讲定义再举例", replaces=old.item_id)
    assert res.error is None, res.content
    st = env.state()
    rules = page_teach(st, env.pid).guidelines
    assert "以后都先讲定义再举例" in rules and "以后都先给例子再讲定义" not in rules
    revoked = st.rule_decisions[-1]
    assert (revoked.action, revoked.rule_id, revoked.text) == ("revoked", old.item_id, old.text)
    assert st.item_evidence[old.item_id] == "你说过「以后都先给例子再讲定义」"     # its source
    [new] = own_items(env)
    assert st.item_evidence[new.item_id] == "你说过「以后都先讲定义再举例」"
    assert (old.origin_ref, new.origin_ref) == ("s1/m1", "s1/m2")
    env.run("undo", action_id=res.action.action_id)
    assert [i.text for i in own_items(env)] == ["以后都先给例子再讲定义"]


# ---- hooks, memory tools, durability ----

STEP = {"at": "assistant.before_answer", "kind": "delegate", "tier": "fast",
        "tools": ["read_path"], "instruction": "先看他的学习路径", "output_to": "context.notes",
        "max_tool_calls": 2}


def test_h1_a_before_answer_hook_gets_its_read_tools_whatever_skills_are_loaded(tmp_path):
    pack = HarnessPack(1, {"H9.assistant.before_answer": [STEP]})
    w = World(tmp_path, [call("read_path", 1), say("路径要点：先学 z。"), say("答")], pack=pack)
    ev = w.turn("我该先学什么")
    assert ev[-1].type == "done", ev[-1]
    hook_req = w.prov.chat_calls[0]
    assert "read_path" in [t.name for t in hook_req.tools]
    assert "read_path" not in w.sent_tools(2)               # the turn itself has no path Skill


def _memory_tool(name: str) -> FakeTool:
    return FakeTool(name, "read", ToolResult(content="记忆"))


def test_the_memory_tools_bind_by_name_when_built_and_are_absent_otherwise(tmp_path):
    absent = World(tmp_path / "a", [call("load_skill", 1, skills=["settings"]), say("好")])
    absent.turn("看看设置")
    assert absent.state().skills == ("settings",)
    assert not {"search_memory", "read_past_turn", "forget_memory"} & {
        *absent.sent_tools(0), *absent.sent_tools(1)}
    w = World(tmp_path / "b", [call("load_skill", 1, skills=["settings"]), say("好")])
    w.tools += [_memory_tool("search_memory"), _memory_tool("read_past_turn"),
                _memory_tool("forget_memory")]
    w.turn("看看设置")
    assert {"search_memory", "read_past_turn"} <= set(w.sent_tools(0))
    assert "forget_memory" not in w.sent_tools(0) and "forget_memory" in w.sent_tools(1)


def test_a_durable_skill_turn_computes_the_same_tool_sets_as_the_plain_one(tmp_path):
    from evowiki.agent import durable
    from evowiki.assistant.durable import durable_turn_deps
    plain = World(tmp_path / "a", _scripted_split())
    plain.turn("把这页拆开")
    w = World(tmp_path / "b", _scripted_split())
    w.deps = durable_turn_deps(w.deps)
    with durable.virtual_workflow("turn:s1:m1"):
        w.turn("把这页拆开")
    assert [w.sent_tools(i) for i in range(3)] == [plain.sent_tools(i) for i in range(3)]
    assert w.state().skills == plain.state().skills == ("edit-wiki",)
