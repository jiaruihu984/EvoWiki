"""ask_check (stage 3 T11): code picks the node and the cell, the assistant writes the
question in its tool arguments, the learner's reply is recorded on THAT cell (no model call). Memory
repo, scripted chat; nothing paid."""

import pytest

from evowiki.agent.tools import Reply, ToolContext
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.assistant.tools.check import AskCheckTool, open_pick
from evowiki.assistant.tools.rough import AskRoughTool
from evowiki.assistant.wiki import WikiData
from evowiki.belief.aspects import Declare, Observe
from evowiki.goals.selfreport import SELF_CLAIM
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import MemoryRepo
from tests.assistant.test_wiki_adapter import gateway
from tests.wikiapp.flat_goal import flat_goal
from tests.wikiapp.goal_script import push_one_page
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import INTAKE_1, NOW, Ids

CTX = ToolContext(user_id="u1", session_id="s1", message_id="m1")


@pytest.fixture
def goal():
    """u1 with an open diagnosis (the z 分数 flat goal: two points, the first pick a quiz)."""
    repo, s, ids = MemoryRepo(["u1"]), Script(), Ids()
    s.push("goals.intake", INTAKE_1)
    push_one_page(s, ["c1", "c2"])
    res = flat_goal(repo, CountingGateway(s.gateway()), "u1", "看懂 z 分数", "我会算 z 分数",
                    now=NOW, new_id=ids)
    assert res.open
    return repo, s, ids, res.draft.draft_id


def tools(repo, s, ids):
    return AskCheckTool(repo, lambda _u: CountingGateway(s.gateway()), lambda: NOW, ids)


def quiz(answer=0):
    return {"stem": "x=7, μ=5, σ=2 时 z 是多少？", "options": ["1", "2", "0.5", "-1"],
            "answer": answer}


def test_ask_check_pauses_without_the_key_and_records_the_reply_on_the_picks_cell(goal):
    repo, s, ids, _did = goal
    ask = tools(repo, s, ids)
    pick = open_pick(repo.load("u1"))
    args = {"pick_id": pick.pick_id, "quiz": quiz()}
    r = ask.run(args, CTX)
    assert r.error is None and r.pause is not None
    payload = r.pause.payload
    assert payload["options"][-1] == "不知道" and set(payload["options"][:-1]) == {"1", "2", "0.5", "-1"}
    assert "answer" not in payload
    before = len(repo.aspect_log("u1"))
    done = ask.resume(args, CTX, r.pause, Reply(text="1"))
    assert done.error is None and "答对了" in done.content
    new = [e for e in repo.aspect_log("u1")[before:] if isinstance(e, Observe)]
    assert [(o.observation.cell, o.observation.polarity, o.observation.kind) for o in new] == [
        (pick.cell, "+", "quiz")]
    assert not any(isinstance(e, Declare) for e in repo.aspect_log("u1"))
    # the same pick again: already asked
    assert ask.run(args, CTX).error is not None


def test_ask_check_refuses_an_item_that_does_not_fit_the_pick(goal):
    repo, s, ids, _ = goal
    ask = tools(repo, s, ids)
    pick = open_pick(repo.load("u1"))
    for bad in ({"pick_id": pick.pick_id, "self_report": {"stem": "学过吗", "options": ["会", "不会"],
                                                          "means": ["know", "dont"]}},   # a quiz pick
                {"pick_id": pick.pick_id, "quiz": {**quiz(), "options": ["1", "1", "2"]}},
                {"pick_id": pick.pick_id, "quiz": {**quiz(), "stem": "$z$ 是多少"}},
                {"pick_id": "dr_x:1", "quiz": quiz()},
                {"pick_id": pick.pick_id, "quiz": quiz(), "self_report": None, "cell": "c2.term"}):
        assert ask.run(bad, CTX).error is not None, bad
    assert open_pick(repo.load("u1")).pick_id == pick.pick_id


def test_free_text_or_an_empty_reply_records_nothing_and_the_pick_stays(goal):
    repo, s, ids, _ = goal
    ask = tools(repo, s, ids)
    pick = open_pick(repo.load("u1"))
    args = {"pick_id": pick.pick_id, "quiz": quiz()}
    r = ask.run(args, CTX)
    before = len(repo.aspect_log("u1"))
    assert "没有记" in ask.resume(args, CTX, r.pause, Reply(text="我想想")).content
    assert "没有回答" in ask.resume(args, CTX, r.pause, Reply(text=" ")).content
    assert len(repo.aspect_log("u1")) == before and open_pick(repo.load("u1")) == pick


def test_a_self_report_is_typed_self_evidence_on_the_cells_it_covers():
    from evowiki.goals.probe import CheckPick, ProbeRound, answered_question, probe_observations
    pick = CheckPick(pick_id="d:1", intent="self_report", node="R", name="块",
                     covers=("a.term", "b.procedure"))
    q = answered_question(pick, stem="学过吗？", options=["会", "记不清", "没学过", "说不清"],
                          means=["know", "partly", "dont", "unsure"])
    r = ProbeRound(questions=(q,), done=False)
    cells = {"R": pick.covers}
    for choice, claim in ((0, "can_do"), (1, "partly"), (2, "never")):
        obs = probe_observations(r, (choice,), draft_id="d", round_index=1, cells=cells)
        assert {(o.cell, o.polarity, o.strength, o.kind) for o in obs} == {
            (c, *SELF_CLAIM[claim], "self") for c in pick.covers}
    assert probe_observations(r, (3,), draft_id="d", round_index=1, cells=cells) == ()


def test_the_whole_diagnosis_in_the_chat_then_the_page(goal, tmp_path):
    """The turn starts the diagnosis (a flat goal has no grid: ask_rough's result is the pick), the
    model writes ask_check until the pick is gone, then the goal is planned. Each question ends its
    turn; his next message (the option he clicked) resumes it, its result naming the next pick. The
    only model calls are the turns' own and the path model's at the end."""
    repo, s, ids, did = goal
    ask = tools(repo, s, ids)
    asked: list[str] = []

    def script():
        while (pick := open_pick(repo.load("u1"))) is not None:
            asked.append(pick.pick_id)
            yield ChatTurn(tool_calls=(ToolCall(id=f"a{len(asked)}", name="ask_check", args={
                "pick_id": pick.pick_id, "quiz": quiz()}),))
        yield ChatTurn(text="好，从第一页开始。")

    turns = script()
    gw, prov = gateway(tmp_path, [])
    prov._chat = lambda m, r: next(turns)            # lazily: each pick read when it is asked
    info = SessionInfo(session_id="s1", user_id="u1", page_id=None, note_concept_id=None,
                       created_at=NOW)
    store = MemorySessionStore()
    store.create(info)
    rough = AskRoughTool(repo, lambda _u: CountingGateway(s.gateway()), lambda: NOW, ids)
    deps = TurnDeps(gateway=gw, tools=[ask, rough], store=store,
                    data=WikiData(repo, lambda: NOW), new_id=Ids(), clock=lambda: NOW)
    ev = list(run_turn(deps, info, MessageIn(text="开始诊断", intent="diagnose", draft_id=did)))
    for _ in range(20):                 # his reply to each question: the option 「1」
        if open_pick(repo.load("u1")) is None:
            break
        assert ev[-1].type == "done" and any(e.type == "ask" for e in ev)
        ev += list(run_turn(deps, info, MessageIn(text="1")))
    assert ev[-1].type == "done" and asked
    st = repo.load("u1").state
    assert did in st.closed_drafts and st.drafts[did].rounds[-1].done
    assert len(st.drafts[did].rounds) == len(asked) + 1
    assert all(len(r.questions) == 1 for r in st.drafts[did].rounds[:-1])
    asks = [e for e in ev if e.type == "ask"]
    assert asks and all("answer" not in e.payload for e in asks)


def test_a_zero_base_goal_asks_nothing():
    """D07: a stated beginner's blocks are taught as they are — the diagnosis has no pick."""
    from tests.wikiapp.test_goal_blocks import BLOCKS, make
    env = (MemoryRepo(["u1"]), Script(), Ids())
    repo = env[0]
    push_one_page(env[1], ["c1"])
    blocks = tuple(b.model_copy(update={"diagnose": False}) for b in BLOCKS)
    res = make(env, blocks)
    assert not res.open and open_pick(repo.load("u1")) is None
