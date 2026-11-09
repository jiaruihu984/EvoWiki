"""确认 on a goal's card starts the diagnosis whatever the model would do with the word (live
2026-11-03: DeepSeek called confirm, got 「现在没有等他确认的操作」 and asked him what he wanted): the
diagnose turn calls ask_rough itself, the grid is its card with no model call, each recorded answer
names the next pick. Memory repo, a scripted chat that behaves like that model; nothing paid."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from evowiki.agent.tools import ActionCard, ToolContext
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import DIAGNOSE_HEAD, TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.assistant.tools.check import AskCheckTool, open_pick
from evowiki.assistant.tools.rough import NO_DIAGNOSIS, NO_GRID, TYPED_PURPOSE, AskRoughTool
from evowiki.assistant.turn_skills import WAITING_LINE
from evowiki.assistant.wiki import WikiData
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.wikiapp.goal import rough_blocks, stop_draft
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.map_edits import PathBlockOp, edit_blocks, edit_map
from evowiki.wikiapp.repo import MemoryRepo
from tests.assistant.test_wiki_adapter import gateway
from tests.wikiapp.goal_script import auto_probe, push_one_page
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_goal_blocks import BLOCKS, make

QUIZ = {"stem": "哪个是对的？", "options": ["这个", "那个"], "answer": 0}


class Trace:
    """The part of the trace these turns exercise: each tool call's result."""

    def __init__(self):
        self.results = {}

    def start(self, *_a, **_k):
        pass

    def model(self, *_a, **_k):
        pass

    def tool(self, _run, _seq, call_id, _name, _args, result):
        self.results[call_id] = result

    def finish(self, *_a, **_k):
        pass


class Ledger:
    """The part of ActionLedger a turn reads: one executed goal_new row with its draft."""

    def __init__(self, draft_id):
        self.row = type("Row", (), {"action_id": "act_g", "status": "executed", "kind": "goal_new",
                                    "summary": "「黑壳虾」分成 6 块", "result": {"draft_id": draft_id},
                                    "undo": {"draft_id": draft_id}, "content": {},
                                    "session_id": "s1"})()

    def get(self, _u, action_id):
        return self.row if action_id == "act_g" else None

    def list(self, *_a, **_k):
        return []


@pytest.fixture
def env():
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()


def ctx():
    return ToolContext(user_id="u1", session_id="s1")


def opened(env):
    push_one_page(env[1], ["c1"])
    res = make(env)
    assert res.open
    return res


class Chat:
    """The scripted model: `reply` answers each request (asking with none set fails); `seen`."""

    def __init__(self, reply=None):
        self.reply, self.seen = reply, []

    def __call__(self, _m, request):
        self.seen.append(request)
        assert self.reply is not None, "the model was asked"
        return self.reply()


def turn_env(env, tmp_path, reply=None, ledger=None):
    repo, s, ids = env
    gw, prov = gateway(tmp_path, [])
    chat = Chat(reply)
    prov._chat = chat
    info = SessionInfo(session_id="s1", user_id="u1", page_id=None, note_concept_id=None,
                       created_at=NOW)
    store = MemorySessionStore()
    store.create(info)
    cgw = lambda _u: CountingGateway(s.gateway())
    deps = TurnDeps(gateway=gw, store=store, data=WikiData(repo, lambda: NOW), new_id=ids,
                    clock=lambda: NOW, ledger=ledger,
                    tools=[AskCheckTool(repo, cgw, lambda: NOW, ids),
                           AskRoughTool(repo, cgw, lambda: NOW, ids)])
    return deps, info, store, chat


def ask_check_of(repo):
    """What a model does with the pick it was told: ask_check on the open pick."""
    def turn():
        pick = open_pick(repo.load("u1"))
        item = ({"self_report": {"stem": "学过吗？", "options": ["会", "不会"],
                                 "means": ["know", "dont"]}}
                if pick.intent == "self_report" else {"quiz": QUIZ})
        return ChatTurn(tool_calls=(ToolCall(id=f"a_{pick.pick_id}", name="ask_check",
                                             args={"pick_id": pick.pick_id, **item}),))
    return turn


def names(req):
    return {t.name for t in req.tools}


def confirm(res):
    """What the card he clicked sends: the 确认 with its own draft."""
    return MessageIn(text="确认", intent="diagnose", draft_id=res.draft.draft_id)


def like_the_real_model():
    """Live 2026-11-03: confirm on the 「确认」, then text asking what he wants."""
    turns = iter([ChatTurn(tool_calls=(ToolCall(id="c1", name="confirm", args={
        "pending_id": "act_g", "asked": "确认"}),)), ChatTurn(text="你现在想做什么？")])
    return lambda: next(turns)


def test_the_grid_appears_with_no_model_call_whatever_the_model_would_answer(env, tmp_path):
    """A model that would call confirm or answer in text is never asked: code calls ask_rough."""
    res = opened(env)
    deps, info, store, chat = turn_env(env, tmp_path, like_the_real_model())
    ev = list(run_turn(deps, info, confirm(res)))
    ask = next(e for e in ev if e.type == "ask")
    assert ask.kind == "ask_rough" and ask.payload["draft_id"] == res.draft.draft_id
    assert [b["name"] for b in ask.payload["blocks"]] == [b.name for b in BLOCKS]
    assert ev[-1].type == "done" and ev[-1].llm_calls == 0 and chat.seen == []
    assert not any(e.type == "text_delta" and e.text for e in ev)
    logged = [e.payload for e in store.entries("s1") if e.type == "tool"]
    assert [p["name"] for p in logged] == ["ask_rough"] and logged[0]["ask"]


def test_after_the_grid_the_model_writes_the_question_from_the_pick_it_is_told(env, tmp_path):
    """His marks resume the code's call: the result reaches the model in his message (no call in
    its history: a provider that signs its calls would refuse an unsigned one) with the pick;
    confirm is neither offered nor suggested; each recorded answer names the next pick."""
    repo = env[0]
    res = opened(env)
    deps, info, _store, chat = turn_env(env, tmp_path, ask_check_of(repo))
    ev = list(run_turn(deps, info, confirm(res)))
    first = next(e for e in ev if e.type == "ask").payload["blocks"][0]["id"]

    ev = list(run_turn(deps, info, MessageIn(text="标好了", reply_data={"marks": {first: "know"}})))
    asked = [e for e in ev if e.type == "ask"]
    assert [e.kind for e in asked] == ["ask_check"] and ev[-1].type == "done"
    req = chat.seen[0]
    said = req.messages[-1].text
    assert req.messages[-1].role == "user" and not req.messages[-1].tool_calls
    assert DIAGNOSE_HEAD in said and "他标了" in said and "pick_id：" in said
    assert "ask_check" in names(req)
    assert not {"confirm", "offer", "undo", "next_check"} & names(req)
    assert "用 confirm 执行" not in req.system

    chat.seen.clear()
    ev = list(run_turn(deps, info, MessageIn(text=asked[0].payload["options"][0])))
    req = chat.seen[0]
    assert req.messages[-1].role == "tool" and "已记下" in req.messages[-1].text
    assert "pick_id：" in req.messages[-1].text or "摸底结束" in req.messages[-1].text
    assert "confirm" not in names(req)


def test_nothing_marked_goes_on_with_the_next_question_and_his_words(env, tmp_path):
    repo = env[0]
    res = opened(env)
    deps, info, _store, chat = turn_env(env, tmp_path, ask_check_of(repo))
    list(run_turn(deps, info, confirm(res)))
    env[1].push(TYPED_PURPOSE, {"marks": []})
    ev = list(run_turn(deps, info, MessageIn(text="开始学")))
    said = chat.seen[0].messages[-1].text
    assert "开始学" in said and "什么也没记" in said and "skip_diagnosis" in said
    assert [e.kind for e in ev if e.type == "ask"] == ["ask_check"]


def test_marks_typed_over_the_grid_are_recorded_as_the_grids(env, tmp_path):
    """「水质忘了，其他没碰过」 instead of clicking: the same marks, through the grid's path; a block number the
    reply makes up is not a mark."""
    repo = env[0]
    res = opened(env)
    deps, info, _store, chat = turn_env(env, tmp_path, ask_check_of(repo))
    ev = list(run_turn(deps, info, confirm(res)))
    blocks = next(e for e in ev if e.type == "ask").payload["blocks"]
    env[1].push(TYPED_PURPOSE, {"marks": [{"n": 1, "mark": "forgot"}, {"n": 99, "mark": "know"}]})
    list(run_turn(deps, info, MessageIn(text="第一块学过但忘了")))
    draft = repo.load("u1").state.drafts[res.draft.draft_id]
    assert draft.rough == {blocks[0]["id"]: "forgot"}
    assert "他打字说的已记下" in chat.seen[0].messages[-1].text


def test_rest_typed_later_does_not_overwrite_what_he_already_answered(env, tmp_path):
    """「其他都没碰过」 after a marked block: the model is only shown the blocks not answered yet, so the earlier mark stands;
    his words reach the turn, a question in them is not lost."""
    repo = env[0]
    res = opened(env)
    deps, info, _store, chat = turn_env(env, tmp_path, ask_check_of(repo))
    ev = list(run_turn(deps, info, confirm(res)))
    blocks = next(e for e in ev if e.type == "ask").payload["blocks"]
    env[1].push(TYPED_PURPOSE, {"marks": [{"n": 1, "mark": "partly"}]})
    list(run_turn(deps, info, MessageIn(text="第一块算熟悉，这是什么意思？")))
    assert "这是什么意思" in chat.seen[0].messages[-1].text
    env[1].push(TYPED_PURPOSE, {"marks": [{"n": 1, "mark": "dont"}]})     # n counts over what is left
    list(run_turn(deps, info, MessageIn(text="其他都没碰过")))
    rough = repo.load("u1").state.drafts[res.draft.draft_id].rough
    assert rough[blocks[0]["id"]] == "partly" and rough[blocks[1]["id"]] == "dont"


def test_typed_over_the_grid_leaves_the_rest_on_a_grid_and_a_stop_ends_it(env, tmp_path):
    """Named blocks are marked and the rest stay on the grid (no one-by-one self-report); 不问了 ends the diagnosis at once."""
    repo = env[0]
    res = opened(env)
    deps, info, _store, chat = turn_env(env, tmp_path, ask_check_of(repo))
    ev = list(run_turn(deps, info, confirm(res)))
    blocks = next(e for e in ev if e.type == "ask").payload["blocks"]
    env[1].push(TYPED_PURPOSE, {"marks": [{"n": 1, "mark": "partly"}]})
    list(run_turn(deps, info, MessageIn(text="第一块算熟悉")))
    assert "调 ask_rough" in chat.seen[0].messages[-1].text
    snap = repo.load("u1")
    left = rough_blocks(snap, res.draft.draft_id)
    assert [b for b, _ in left[1]] == [b["id"] for b in blocks[1:]]
    chat.seen.clear()
    env[1].push(TYPED_PURPOSE, {"marks": [], "stop": True})
    list(run_turn(deps, info, MessageIn(text="不问了")))
    assert res.draft.draft_id in repo.load("u1").state.closed_drafts
    assert "摸底结束" in chat.seen[0].messages[-1].text


def test_chat_edits_fold_with_the_cards_code_and_report_what_was_refused(env):
    """Place moves the block first, an unknown name is reported next to what was done, and undo brings it back."""
    repo = env[0]
    res = opened(env)
    map_id = res.draft.map_id
    top = [n.name for n in repo.load("u1").state.maps[map_id].children(None) if n.kind == "region"]
    ops = [PathBlockOp(op="place", name=top[-1], position=1), PathBlockOp(op="drop", name="没有这一块")]
    got = edit_blocks(repo, "u1", map_id, ops, now=NOW, new_id=env[2], extra=lambda e: ())
    names = lambda: [n.name for n in repo.load("u1").state.maps[map_id].children(None) if n.kind == "region"]
    assert names()[0] == top[-1] and got.done and "没有这一块" in got.refused[0]
    edit_map(repo, "u1", map_id, got.undo, now=NOW, new_id=env[2])
    assert names() == top
    nothing = edit_blocks(repo, "u1", map_id, ops[1:], now=NOW, new_id=env[2], extra=lambda e: ())
    assert not nothing.done and names() == top


def test_a_beginners_goal_has_no_grid_and_a_diagnose_turn_says_so(env, tmp_path):
    """A stated beginner's goal finishes at once (no draft: the client goes straight on, no diagnose
    message at all); a diagnose message anyway is a clear refusal, nothing silent, no model call."""
    push_one_page(env[1], ["c1"])
    res = make(env, tuple(b.model_copy(update={"diagnose": False}) for b in BLOCKS))
    assert not res.open and open_pick(env[0].load("u1")) is None
    deps, info, store, chat = turn_env(env, tmp_path)
    ev = list(run_turn(deps, info, MessageIn(text="确认", intent="diagnose", draft_id="draft_gone")))
    assert not any(e.type == "ask" for e in ev) and chat.seen == []
    assert ev[-1].type == "error" and ev[-1].code == "refused" and ev[-1].detail == NO_DIAGNOSIS
    assert any(e.type == "turn_error" for e in store.entries("s1"))


def test_a_stale_draft_is_a_clear_refusal_not_silence(env, tmp_path):
    """The diagnosis closed between the 确认 and the message (he skipped it, undid the goal)."""
    repo, s, ids = env
    res = opened(env)
    stop_draft(repo, CountingGateway(s.gateway()), "u1", res.draft.draft_id, now=NOW, new_id=ids)
    deps, info, _store, chat = turn_env(env, tmp_path)
    ev = list(run_turn(deps, info, confirm(res)))
    assert ev[-1].type == "error" and ev[-1].detail == NO_DIAGNOSIS and chat.seen == []


def test_a_diagnose_turn_with_no_grid_left_has_the_model_write_the_question(env, tmp_path):
    """A draft whose grid is done (the same goes for a block's own diagnosis): no card from code;
    the one model call gets the pick in his message and writes ask_check, without confirm."""
    repo = env[0]
    res = opened(env)
    deps, info, _store, chat = turn_env(env, tmp_path, ask_check_of(repo))
    ev = list(run_turn(deps, info, confirm(res)))
    blocks = next(e for e in ev if e.type == "ask").payload["blocks"]
    list(run_turn(deps, info, MessageIn(text="标好了", reply_data={"marks": {b["id"]: "know" for b in blocks}})))
    chat.seen.clear()
    ev = list(run_turn(deps, info, confirm(res)))
    said = chat.seen[0].messages[-1].text
    assert NO_GRID in said and "pick_id：" in said and "confirm" not in names(chat.seen[0])
    assert [e.kind for e in ev if e.type == "ask"] == ["ask_check"]


def two_open_goals(env):
    first = opened(env)
    push_one_page(env[1], ["c2"])
    second = make(env, tuple(b.model_copy(update={"name": b.name + "（二）"}) for b in BLOCKS))
    assert first.open and second.open and first.draft.draft_id != second.draft.draft_id
    return first, second


def test_with_two_open_goals_each_confirm_reaches_its_own_draft(env, tmp_path):
    """The older card's 确认 opens the older goal's grid, though the newer draft is his newest."""
    first, second = two_open_goals(env)
    deps, info, _store, chat = turn_env(env, tmp_path)
    for res in (first, second):
        ask = next(e for e in run_turn(deps, info, confirm(res)) if e.type == "ask")
        assert ask.kind == "ask_rough" and ask.payload["draft_id"] == res.draft.draft_id
    assert chat.seen == []


def test_the_marks_and_the_question_stay_on_the_clicked_draft(env, tmp_path):
    """The grid of the older card, marked: recorded there, and the question the model is told is
    that draft's pick, while the newer draft stays untouched."""
    repo = env[0]
    first, second = two_open_goals(env)
    pick_of = lambda d: open_pick(repo.load("u1"), d)
    untouched = pick_of(second.draft.draft_id)
    deps, info, _store, chat = turn_env(env, tmp_path, lambda: ChatTurn(text="好"))
    ev = list(run_turn(deps, info, confirm(first)))
    block = next(e for e in ev if e.type == "ask").payload["blocks"][0]["id"]
    list(run_turn(deps, info, MessageIn(text="标好了", reply_data={"marks": {block: "dont"}})))
    said = chat.seen[0].messages[-1].text
    assert f"pick_id：{pick_of(first.draft.draft_id).pick_id}" in said
    assert untouched.pick_id not in said and pick_of(second.draft.draft_id) == untouched
    assert "next_check" not in names(chat.seen[0])


def test_a_closed_draft_is_refused_while_another_goal_is_open(env, tmp_path):
    """Not the newest open one instead: the older card's closed diagnosis is a clear refusal."""
    repo, s, ids = env
    first, second = two_open_goals(env)
    stop_draft(repo, CountingGateway(s.gateway()), "u1", first.draft.draft_id, now=NOW, new_id=ids)
    deps, info, store, chat = turn_env(env, tmp_path)
    ev = list(run_turn(deps, info, confirm(first)))
    assert not any(e.type == "ask" for e in ev) and chat.seen == []
    assert ev[-1].type == "error" and ev[-1].code == "refused" and ev[-1].detail == NO_DIAGNOSIS
    assert open_pick(repo.load("u1"), second.draft.draft_id) is not None
    row = next(e.payload for e in store.entries("s1") if e.type == "tool")
    assert row["args"] == {"draft_id": first.draft.draft_id} and row["error"] == NO_DIAGNOSIS


@pytest.mark.parametrize("body", [{"intent": "diagnose"}, {"intent": "chat", "draft_id": "d1"},
                                  {"intent": "diagnose", "draft_id": ""}])
def test_draft_id_belongs_to_a_diagnose_message_which_needs_one(body):
    with pytest.raises(ValidationError):
        MessageIn(text="确认", **body)


def test_a_refused_ask_check_keeps_its_reason_on_the_row_the_log_and_the_trace(env, tmp_path, caplog):
    """Live 2026-11-03: the first ask_check was refused and nothing said why afterwards."""
    repo = env[0]
    res = opened(env)
    pick = None

    def turns():
        nonlocal pick
        pick = open_pick(repo.load("u1"), res.draft.draft_id)
        yield ChatTurn(tool_calls=(ToolCall(id="bad", name="ask_check", args={
            "pick_id": pick.pick_id, "quiz": {"stem": "哪个对？", "options": ["一样", "一样"],
                                              "answer": 0}}),))
        yield ChatTurn(text="这题没写成。")

    script = turns()
    deps, info, store, _chat = turn_env(env, tmp_path, lambda: next(script))
    trace = Trace()
    deps = replace(deps, trace=trace)
    ev = list(run_turn(deps, info, confirm(res)))
    block = next(e for e in ev if e.type == "ask").payload["blocks"][0]["id"]
    with caplog.at_level("WARNING", logger="evowiki.assistant.loop"):
        list(run_turn(deps, info, MessageIn(text="标好了", reply_data={"marks": {block: "dont"}})))
    row = next(e.payload for e in store.entries("s1") if e.type == "tool"
               and e.payload["call_id"] == "bad")
    assert row["ok"] is False and row["error"] and row["error"] in row["content"]
    assert row["error"] in caplog.text and "ask_check" in caplog.text
    assert trace.results["bad"].error == row["error"]


def test_a_pick_id_that_is_not_the_open_one_is_told_the_open_one(env, tmp_path):
    repo = env[0]
    first, _second = two_open_goals(env)
    tool = AskCheckTool(repo, lambda _u: None, lambda: NOW, env[2])
    pick = open_pick(repo.load("u1"), first.draft.draft_id)
    r = tool.run({"pick_id": f"{first.draft.draft_id}:9", "quiz": QUIZ},
                 ToolContext(user_id="u1", session_id="s1", message_id="m1"))
    assert r.pause is None and r.error and pick.pick_id in r.error
    assert "next_check" not in r.error


def card():
    return ActionCard(action_id="act_g", kind="goal_new", title="「黑壳虾」分成 6 块",
                      status="proposed", confirm=True)


def session_with_a_confirmed_goal(store):
    store.append("s1", "e1", "user", {"message_id": "m0", "text": "我要学黑壳虾"}, NOW)
    store.append("s1", "e2", "tool", {"message_id": "m0", "call_id": "c0", "name": "new_goal",
                                      "effect": "propose", "args": {}, "ok": True, "summary": "",
                                      "content": "", "sources": [],
                                      "action": card().model_dump(mode="json")}, NOW)
    store.append("s1", "e3", "assistant", {"message_id": "a0", "text": "分好了。", "sources": [],
                                           "proposals": [], "actions": ["act_g"], "tool_calls": 1,
                                           "llm_calls": 1}, NOW)


def test_a_chat_turn_of_a_session_with_a_goal_waiting_says_so_and_can_start_it(env, tmp_path):
    """「开始学」 is no keyword: the turn's context says the goal's diagnosis is open and the tools are
    loaded; the model reads his words. ask_rough with that draft gives the grid."""
    res = opened(env)
    did = res.draft.draft_id
    start = lambda: ChatTurn(tool_calls=(ToolCall(id="r1", name="ask_rough",
                                                  args={"draft_id": did}),))
    deps, info, store, chat = turn_env(env, tmp_path, start, ledger=Ledger(did))
    session_with_a_confirmed_goal(store)
    ev = list(run_turn(deps, info, MessageIn(text="开始学")))
    line = WAITING_LINE.format(title="「黑壳虾」分成 6 块", draft_id=did)
    assert line in chat.seen[0].messages[-1].text
    assert {"ask_rough", "ask_check"} <= names(chat.seen[0])
    assert [e.kind for e in ev if e.type == "ask"] == ["ask_rough"]


def test_no_line_once_the_diagnosis_is_closed(env, tmp_path):
    repo, s, ids = env
    did = opened(env).draft.draft_id
    stop_draft(repo, CountingGateway(s.gateway()), "u1", did, now=NOW, new_id=ids)
    deps, info, store, chat = turn_env(env, tmp_path, lambda: ChatTurn(text="好"), ledger=Ledger(did))
    session_with_a_confirmed_goal(store)
    list(run_turn(deps, info, MessageIn(text="开始学")))
    assert "draft_id：" not in chat.seen[0].messages[-1].text
    assert "ask_rough" not in names(chat.seen[0])
