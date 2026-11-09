"""ask_user: the assistant asks the learner and WAITS for his answer: the turn ends on the question
(the card, then done; no wait, no timeout, no further model call). His next message - typed, or an
option he clicked, sent as his message - resumes that run as the ask_user call's result, in a new
turn (its own DBOS workflow); a message that does not answer it is answered as a new request, and
the question is dropped quietly. Scripted model only (no paid call)."""

import inspect

from evowiki.agent.tools import Pause, Reply, ToolContext
from evowiki.assistant import loop as assistant_loop
from evowiki.assistant import turn_runs
from evowiki.assistant.contracts import AskEvent, MessageIn
from evowiki.assistant.service import AssistantService
from evowiki.assistant.tools.ask_user import REPLIED, AskUserTool
from evowiki.llm.chat import ChatTurn
from tests.assistant.test_loop import call
from tests.assistant.test_turn_runs import MSG, Applier, build, dbos_env, workflow_id  # noqa: F401

CTX = ToolContext(user_id="u1", session_id="s1")
QUESTION = "你学过线性代数吗？"
ASK = ChatTurn(text="先问你一句。", tool_calls=(call("ask_user", 1, question=QUESTION,
                                                    options=["学过", "没学过"]),))


def replied(text: str) -> str:
    return REPLIED.format(q=QUESTION, a=text)


# ---- the tool ----

def test_the_tool_asks_one_plain_question_and_options_only_when_given():
    t = AskUserTool()
    assert t.spec.pauses and t.spec.name == "ask_user"
    r = t.run({"question": "  你学过\n线性代数吗？ "}, CTX)
    assert r.error is None and r.pause == Pause(
        kind="ask_user", payload={"question": "你学过 线性代数吗？", "options": []})
    r = t.run({"question": "哪种？", "options": ["A", "B", "A"]}, CTX)
    assert r.pause.payload["options"] == ["A", "B"]
    assert t.run({"question": ""}, CTX).error is not None
    assert t.run({"question": "q", "options": ["x"] * 7}, CTX).error is not None


def test_resume_frames_his_next_message_answer_or_not():
    t = AskUserTool()
    p = Pause(kind="ask_user", payload={"question": "q", "options": []})
    out = t.resume({}, CTX, p, Reply(text=" 学过一点 ")).content
    assert out == REPLIED.format(q="q", a="学过一点")
    assert "如果不是" in out and "不要再追问" in out       # not an answer: respond to it instead


def test_nothing_waits_and_nothing_answers_for_him():
    """(5) no timeout anywhere: no wait constant, no auto answer, no reply route."""
    assert not hasattr(assistant_loop, "ASK_WAIT_S") and not hasattr(turn_runs, "ASK_WAIT_S")
    assert not hasattr(assistant_loop, "NOT_ANSWERED")
    from evowiki.agent import durable
    assert not hasattr(durable, "wait_reply") and not hasattr(durable, "send_reply")
    assert not hasattr(AssistantService, "reply")
    assert "wait" not in inspect.signature(assistant_loop.run_turn).parameters


# ---- inline turns (the same run_turn the workflow runs) ----

def _svc(tmp_path, script):
    deps = build(tmp_path, script)
    return AssistantService(deps, Applier()), deps.gateway._providers["scripted"]


def _send(svc, text):
    return list(svc.send("u1", "s1", MessageIn(text=text)))


def test_ask_user_ends_the_turn_with_its_card_and_no_further_model_call(tmp_path):
    """(1)"""
    svc, prov = _svc(tmp_path, [ASK, ChatTurn(text="never")])
    evs = _send(svc, "帮我学 PCA")
    types = [e.type for e in evs]
    assert types[-1] == "done" and "tool_start" not in types
    [ask] = [e for e in evs if e.type == "ask"]
    assert isinstance(ask, AskEvent) and ask.call_id == "c1" and ask.kind == "ask_user"
    assert ask.payload == {"question": QUESTION, "options": ["学过", "没学过"]}
    assert len(prov.chat_calls) == 1
    st = svc._deps.store
    from evowiki.assistant.session import fold
    state = fold(st.info("s1"), st.entries("s1"))
    assert state.pending is not None and state.pending.call.id == "c1"
    # the session is free: nothing holds it (the next send is accepted below)


def test_the_next_message_resumes_the_paused_run(tmp_path):
    """(2) typed answer: the model gets the paused messages + the ask_user result with his text."""
    svc, prov = _svc(tmp_path, [ASK, ChatTurn(text="好，那先从向量讲起。")])
    _send(svc, "帮我学 PCA")
    first = prov.chat_calls[0]
    evs = _send(svc, "只学过一点矩阵")
    assert evs[-1].type == "done" and len(prov.chat_calls) == 2
    second = prov.chat_calls[1].messages
    # the paused request's messages, then the model's ask_user call and his message as its result
    assert second[:len(first.messages)] == first.messages
    asked, result = second[len(first.messages):]
    assert asked.role == "assistant" and [c.id for c in asked.tool_calls] == ["c1"]
    assert result.role == "tool" and result.tool_call_id == "c1"
    assert result.text == replied("只学过一点矩阵")
    assert "".join(e.text for e in evs if e.type == "text_delta") == "好，那先从向量讲起。"
    assert not any(e.type in ("tool_start", "tool_end", "ask") for e in evs)


def test_an_option_click_is_his_message_and_resumes_the_same_way(tmp_path):
    """(3) the plugin sends the option's text as his message (the normal send)."""
    svc, prov = _svc(tmp_path, [ASK, ChatTurn(text="好，那从头讲。")])
    _send(svc, "帮我学 PCA")
    _send(svc, "没学过")
    assert prov.chat_calls[1].messages[-1].text == replied("没学过")


def test_an_unrelated_message_takes_the_same_path_and_the_question_is_dropped(tmp_path):
    """(4) the model answers the new thing; nothing is asked again; the pending one is cleared."""
    svc, prov = _svc(tmp_path, [ASK, ChatTurn(text="协方差看两个量是不是一起变。"),
                                ChatTurn(text="不客气。")])
    _send(svc, "帮我学 PCA")
    evs = _send(svc, "协方差是什么？")
    assert prov.chat_calls[1].messages[-1].text == replied("协方差是什么？")
    assert not any(e.type == "ask" for e in evs)
    out = svc.get_session("u1", "s1")
    [card] = [a for m in out.messages for a in m.asks]
    assert card.status == "answered"                       # no longer answerable, no notice
    _send(svc, "谢谢")
    third = prov.chat_calls[2].messages
    assert third[-1].role == "user" and "谢谢" in third[-1].text      # a plain new turn
    assert not any(m.role == "tool" for m in third)


def test_reload_shows_the_pending_card_answerable(tmp_path):
    """(6) session GET: the card is waiting until his next message."""
    svc, _ = _svc(tmp_path, [ASK, ChatTurn(text="好。")])
    _send(svc, "帮我学 PCA")
    out = svc.get_session("u1", "s1")
    [_, answer] = out.messages
    assert answer.role == "assistant" and answer.text == "先问你一句。" and answer.tools == ()
    [card] = answer.asks
    assert (card.call_id, card.kind, card.status) == ("c1", "ask_user", "waiting")
    assert card.payload["options"] == ["学过", "没学过"]
    _send(svc, "学过")                                      # still answerable after a reload
    assert svc.get_session("u1", "s1").messages[1].asks[0].status == "answered"


def test_history_shows_what_was_asked_and_what_he_replied(tmp_path):
    """(7) a later turn's history: the question at the end of that answer, his reply as the next
    turn; an unanswered question at the end stays asked, with nothing answered for him."""
    svc, prov = _svc(tmp_path, [ASK, ChatTurn(text="那从头讲。"), ChatTurn(text="好的。"),
                                ASK.model_copy(update={"text": ""})])
    _send(svc, "帮我学 PCA")
    _send(svc, "没学过")
    _send(svc, "继续")
    hist = [(m.role, m.text) for m in prov.chat_calls[2].messages[:-1]]
    assert hist[0][0] == "user" and "帮我学 PCA" in hist[0][1]
    assert hist[1] == ("assistant", f"先问你一句。\n\n（问了他：「{QUESTION}」）")
    assert hist[2][0] == "user" and hist[2][1] == "没学过"
    assert hist[3] == ("assistant", "那从头讲。")
    # an unanswered question at the end: asked, and no 「没有回答」 sent as if he had said it
    _send(svc, "再考考我")
    from evowiki.assistant.session import fold
    st = svc._deps.store
    state = fold(st.info("s1"), st.entries("s1"))
    assert state.turns[-1].asks == (QUESTION,) and state.pending is not None
    assert all("没有回答" not in m.text for c in prov.chat_calls for m in c.messages)


def test_a_message_with_files_or_a_quote_is_a_new_request(tmp_path):
    svc, prov = _svc(tmp_path, [ASK, ChatTurn(text="好。")])
    _send(svc, "帮我学 PCA")
    list(svc.send("u1", "s1", MessageIn(text="看这段", selection={"text": "协方差矩阵"})))
    assert not any(m.role == "tool" for m in prov.chat_calls[1].messages)
    from evowiki.assistant.session import fold
    st = svc._deps.store
    assert fold(st.info("s1"), st.entries("s1")).pending is None


# ---- DBOS: the turn workflow ends on the question; the answer is its own workflow ----

def test_dbos_the_ask_ends_the_workflow_and_the_answer_resumes_in_a_new_one(dbos_env):  # noqa: F811
    from dbos import DBOS
    svc = dbos_env([ASK, ChatTurn(text="好，那先从向量讲起。")])
    evs = list(svc.send("u1", "s1", MSG))
    assert [e.type for e in evs][-1] == "done" and any(e.type == "ask" for e in evs)
    wid = workflow_id(svc)
    assert DBOS.retrieve_workflow(wid).get_result() == "done"     # returned: nothing waits
    assert not svc._runs.busy("s1")
    rest = list(svc.send("u1", "s1", MessageIn(text="没学过")))
    assert rest[-1].type == "done"
    ws = DBOS.list_workflows(workflow_id_prefix="turn:s1:", load_input=False, load_output=False)
    assert len(ws) == 2 and all(w.status == "SUCCESS" for w in ws)
    second = svc.turn_deps.gateway._providers["scripted"].chat_calls[1]
    assert second.messages[-1].text == replied("没学过")


def test_the_ask_backend_scripts_both_variants():
    """The real-Obsidian harness's backend (tests/e2e/ask_backend.py) scripts the option-click and
    the unrelated-question variants."""
    from tests.e2e import ask_backend
    assert {"option", "unrelated"} <= set(ask_backend.VARIANTS)


def test_a_rating_of_a_question_names_the_question_in_its_signal(tmp_path):
    """👍 / 👎 on a turn that ended on a question (the question card's own thumbs) is the same
    rating of that turn's message; the stored entry and the harness signal carry the question."""
    from evowiki.assistant.contracts import RatingIn
    from evowiki.wikiapp.signals import assistant_feedback, assistant_ratings, rated_answers
    svc, _ = _svc(tmp_path, [ASK])
    _send(svc, "帮我学 PCA")
    [user, answer] = svc.get_session("u1", "s1").messages
    out = svc.rate("u1", "s1", answer.message_id,
                   RatingIn(value="down", chips=["问过了"], note="上一轮刚问过"))
    assert out.value == "down"
    st = svc._deps.store
    [entry] = [e for e in st.entries("s1") if e.type == "rating"]
    assert entry.payload["turn_id"] == user.message_id
    assert entry.payload["asks"] == [{"call_id": "c1", "question": QUESTION}]
    assert (entry.payload["chips"], entry.payload["note"]) == (["问过了"], "上一轮刚问过")
    # the harness reads it at read time (round: assistant_feedback -> a turn case; observe:
    # rated_answers -> assistant_down_rate), with the question and the reason in the case text
    [(sig, run_id)] = assistant_feedback(st, None, "u1")
    assert run_id == f"assistant:s1:{user.message_id}" and sig.signal_id == f"arat:{answer.message_id}"
    assert sig.kind == "assistant_rated"
    assert sig.text == f"觉得这次提问不好：问过了。问题：{QUESTION}补充：上一轮刚问过"
    assert assistant_ratings(st, "u1") == [(sig, run_id)]
    assert [down for _, down in rated_answers(st, "u1")] == [True]
    from evowiki.harness.cases import assistant_cases
    [case], unusable = assistant_cases([(sig, run_id)], "u1", check=lambda c: None)
    assert unusable == () and (case.kind, case.run_id, case.signal_ids) == (
        "assistant", run_id, (sig.signal_id,))
    assert svc.get_session("u1", "s1").messages[1].rating.value == "down"


def test_the_words_a_question_comes_with_are_the_turns_text(tmp_path):
    """A call that explains, then asks (the 冲突 Skill: say what the entries are, then ask which
    to keep): its words are not taken back as between-rounds narration; they stream before the
    card and are stored once."""
    words = "这两条一条说先举例，一条说不要举例。"
    svc, _ = _svc(tmp_path, [ChatTurn(text=words, tool_calls=ASK.tool_calls)])
    evs = _send(svc, "处理待确认的记忆")
    types = [e.type for e in evs]
    shown = "".join(e.text for e in evs if e.type == "text_delta")
    assert shown == words and types.index("text_delta") < types.index("ask")
    from evowiki.assistant.session import fold
    st = svc._deps.store
    state = fold(st.info("s1"), st.entries("s1"))
    assert state.messages[-1].text.count(words) == 1
