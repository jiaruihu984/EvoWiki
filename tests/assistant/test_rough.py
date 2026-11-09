"""ask_rough, the 摸底 grid (stage 3 phase 2): the opening diagnosis's blocks in one card, his marks
come back in the message's reply_data, each marked block is one self-report on its cells (the
self-report table), a block marked 不会 gets no 细问, and a stated beginner gets no grid. Memory
repo, scripted chat; nothing paid."""

import pytest

from evowiki.agent.tools import Pause, Reply, ToolContext
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.assistant.tools.check import block_of, open_pick
from evowiki.assistant.tools.rough import NO_DIAGNOSIS, AskRoughTool
from evowiki.assistant.wiki import WikiData
from evowiki.belief.aspects import Observe
from evowiki.goals.selfreport import SELF_CLAIM
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import MemoryRepo
from tests.assistant.test_wiki_adapter import gateway
from tests.wikiapp.goal_script import auto_probe, push_one_page
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_goal_blocks import BLOCKS, make


@pytest.fixture
def env():
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()

CTX = ToolContext(user_id="u1", session_id="s1", message_id="m1")


def tool(env):
    repo, s, ids = env
    return AskRoughTool(repo, lambda _u: CountingGateway(s.gateway()), lambda: NOW, ids)


def rough_obs(repo):
    return [e.observation for e in repo.aspect_log("u1") if isinstance(e, Observe)
            and e.observation.evidence_ref.startswith("rough:")]


def opened(env):
    push_one_page(env[1], ["c1"])
    res = make(env)
    assert res.open
    m = env[0].load("u1").state.maps[res.draft.map_id]
    return res, {n.name: n.id for n in m.children(None) if n.kind == "region"}


def test_the_grid_lists_the_diagnosed_blocks_and_records_one_self_report_per_marked_block(env):
    repo = env[0]
    res, ids = opened(env)
    r = tool(env).run({}, CTX)
    assert r.error is None and r.pause is not None and r.pause.kind == "ask_rough"
    assert r.pause.payload["draft_id"] == res.draft.draft_id
    assert [b["name"] for b in r.pause.payload["blocks"]] == [b.name for b in BLOCKS]
    marks = {ids["认识黑壳虾"]: "know", ids["把水养好"]: "partly", ids["日常喂养"]: "dont"}
    ctx = CTX.model_copy(update={"call_id": "c1"})
    out = tool(env).resume({}, ctx, r.pause, Reply(text="标好了", data={"marks": marks}))
    assert out.error is None, out.content
    obs = rough_obs(repo)
    by_block: dict[str, set] = {}
    for o in obs:
        by_block.setdefault(o.evidence_ref.rsplit(":", 1)[1], set()).add(
            (o.polarity, o.strength, o.kind))
    assert by_block == {ids["认识黑壳虾"]: {(*SELF_CLAIM["can_do"], "self")},
                        ids["把水养好"]: {(*SELF_CLAIM["partly"], "self")},
                        ids["日常喂养"]: {(*SELF_CLAIM["never"], "self")}}
    # the same call again: recorded once; a block marked 不会 is never the next pick
    tool(env).resume({}, ctx, r.pause, Reply(text="标好了", data={"marks": marks}))
    assert len(rough_obs(repo)) == len(obs)
    for _ in range(10):
        pick = open_pick(repo.load("u1"))
        if pick is None:
            break
        assert block_of(repo.load("u1"), pick)[0] != ids["日常喂养"]
        from tests.wikiapp.goal_script import answer_round
        answer_round(repo, CountingGateway(env[1].gateway()), "u1", res.draft.draft_id, [0],
                     now=NOW, new_id=env[2])
    # the grid is asked once
    assert tool(env).run({}, CTX).pause is None


def test_a_block_marked_on_the_grid_is_verified_with_a_quiz_never_asked_again(env):
    """He said 掌握 / 熟悉 on the grid: the 细问 on that block is a quiz that checks the claim
    (probe: no second self-report); an unmarked block may still get its coarse self-report."""
    repo = env[0]
    res, ids = opened(env)
    r = tool(env).run({}, CTX)
    marked = {ids["认识黑壳虾"]: "know", ids["把水养好"]: "partly"}
    ctx = CTX.model_copy(update={"call_id": "c3"})
    assert tool(env).resume({}, ctx, r.pause, Reply(text="标好了", data={"marks": marked})).error is None
    from tests.wikiapp.goal_script import answer_round
    seen = []
    for _ in range(12):
        pick = open_pick(repo.load("u1"))
        if pick is None:
            break
        block = block_of(repo.load("u1"), pick)[0]
        seen.append((block, pick.intent))
        if block in marked:
            assert pick.intent != "self_report", (block, pick)
        answer_round(repo, CountingGateway(env[1].gateway()), "u1", res.draft.draft_id, [0],
                     now=NOW, new_id=env[2])
    assert any(b in marked and i != "self_report" for b, i in seen), seen


def test_the_last_answer_names_the_block_the_goal_enters_next(env):
    """Review of side-07 (2026-10-29): the chat line 「从「X」开始」, the block 学点新的 enters and the row it
    highlights are one block — the diagnosis's last tool result names GoalMapOut.next_region (K's answer: a block
    is skipped only when every cell is likely or known)."""
    import re

    from evowiki.assistant.tools.check import AskCheckTool
    from evowiki.wikiapp.maps import map_out
    repo, s, ids_ = env
    res, ids = opened(env)
    r = tool(env).run({}, CTX)
    marks = {ids["认识黑壳虾"]: "know", ids["把水养好"]: "partly", ids["日常喂养"]: "dont", ids["繁殖"]: "dont"}
    out = tool(env).resume({}, CTX.model_copy(update={"call_id": "c4"}), r.pause, Reply(text="x", data={"marks": marks}))
    ask = AskCheckTool(repo, lambda _u: CountingGateway(s.gateway()), lambda: NOW, ids_)
    k = 0
    while (pick := open_pick(repo.load("u1"))) is not None:
        k += 1
        args = {"pick_id": pick.pick_id, "quiz": {"stem": f"第 {k} 题问的是哪一个？", "options": ["甲", "乙", "丙"], "answer": 0}}
        p = ask.run(args, CTX)
        assert p.pause is not None, p.content
        out = ask.resume(args, CTX.model_copy(update={"call_id": f"q{k}"}), p.pause, Reply(text="甲" if k == 1 else "不知道"))
    snap = repo.load("u1")
    m = map_out(snap.state, snap.k, res.draft.map_id, NOW)
    assert m.next_region is not None
    named = re.findall(r"从「(.+?)」开始", out.content)
    assert named and set(named) == {next(n.name for n in m.nodes if n.id == m.next_region)}, out.content
    # the block it names is not one the map shows known (掌握)
    assert next(n.state for n in m.nodes if n.id == m.next_region) != "known"


def _grid_then_quizzes(env, marks: dict, right) -> tuple:
    """The grid with `marks` (block name → mark), then each quiz answered right when `right(block name)`; returns
    the last tool result and the map as GET /wiki/maps serves it."""
    from evowiki.assistant.tools.check import AskCheckTool
    from evowiki.wikiapp.maps import map_out
    repo, s, ids_ = env
    res, ids = opened(env)
    r = tool(env).run({}, CTX)
    out = tool(env).resume({}, CTX.model_copy(update={"call_id": "g1"}), r.pause,
                           Reply(text="x", data={"marks": {ids[k]: v for k, v in marks.items()}}))
    ask = AskCheckTool(repo, lambda _u: CountingGateway(s.gateway()), lambda: NOW, ids_)
    k = 0
    while (pick := open_pick(repo.load("u1"))) is not None:
        k += 1
        name = next(n for n, i in ids.items() if i == block_of(repo.load("u1"), pick)[0])
        args = {"pick_id": pick.pick_id, "quiz": {"stem": f"第 {k} 题问的是哪一个？", "options": ["甲", "乙", "丙"], "answer": 0}}
        p = ask.run(args, CTX)
        out = ask.resume(args, CTX.model_copy(update={"call_id": f"q{k}"}), p.pause, Reply(text="甲" if right(name) else "乙"))
    snap = repo.load("u1")
    return out, map_out(snap.state, snap.k, res.draft.map_id, NOW), ids


def test_a_grid_mastered_block_whose_quiz_is_answered_right_is_known_and_skipped(env):
    """User ruling 2026-10-29 「算会，跳过（和 demo 一致）」: 掌握 on the grid + its verifying quiz right → the whole
    block is known (state known, 掌握 in 学点新的), next_region moves on and the start line names the next block."""
    out, m, ids = _grid_then_quizzes(env, {"认识黑壳虾": "know", "把水养好": "partly", "日常喂养": "dont", "繁殖": "dont"},
                                     lambda name: name == "认识黑壳虾")
    state = {n.id: n.state for n in m.nodes}
    assert state[ids["认识黑壳虾"]] == "known"
    assert m.next_region == ids["把水养好"] and state[ids["把水养好"]] != "known"   # 熟悉 marks unchanged by the rule
    assert "从「把水养好」开始" in out.content


def test_a_grid_mastered_block_whose_quiz_is_answered_wrong_is_left_to_k(env):
    out, m, ids = _grid_then_quizzes(env, {"认识黑壳虾": "know", "把水养好": "partly", "日常喂养": "dont", "繁殖": "dont"},
                                     lambda name: False)
    assert {n.id: n.state for n in m.nodes}[ids["认识黑壳虾"]] != "known"
    assert m.next_region == ids["认识黑壳虾"] and "从「认识黑壳虾」开始" in out.content


def test_one_colour_a_missed_quiz_refutes_the_mark_and_a_grid_known_block_is_known_on_the_graph_too(env):
    """掌握 / 熟悉 + quiz missed: the block reads 不会 on the row and the graph (he saw 熟悉 after a wrong answer).
    掌握 + quiz right: known on the map and on the graph, whatever its other cells say."""
    from evowiki.wikiapp.graph_out import graph_out
    _, m, ids = _grid_then_quizzes(env, {"认识黑壳虾": "know", "把水养好": "partly", "日常喂养": "dont", "繁殖": "dont"},
                                   lambda name: name == "认识黑壳虾")
    nodes = {n.id: n for n in m.nodes}
    water, shrimp = nodes[ids["把水养好"]], nodes[ids["认识黑壳虾"]]
    assert max(water.mix, key=lambda b: water.mix[b]) == "gap", water.mix
    assert shrimp.mix["known"] == 1.0
    snap = env[0].load("u1")
    bands = {n.id: n.band for n in graph_out(snap.state, snap.k, NOW).nodes}
    assert bands[ids["认识黑壳虾"]] == "known" and bands[ids["把水养好"]] == "gap"


def test_unmarked_blocks_and_no_words_record_nothing(env):
    repo = env[0]
    opened(env)
    r = tool(env).run({}, CTX)
    ctx = CTX.model_copy(update={"call_id": "c2"})
    out = tool(env).resume({}, ctx, r.pause, Reply(text=""))
    assert out.error is None and rough_obs(repo) == []
    out = tool(env).resume({}, ctx, r.pause, Reply(text="", data={"marks": {"ghost": "know"}}))
    assert out.error is None and rough_obs(repo) == []


def test_a_stated_beginner_gets_no_grid(env):
    push_one_page(env[1], ["c1"])
    make(env, tuple(b.model_copy(update={"diagnose": False}) for b in BLOCKS))
    r = tool(env).run({}, CTX)                # no open diagnosis: says so, no card
    assert r.pause is None and r.error == NO_DIAGNOSIS


def test_the_grid_end_to_end_a_pause_then_a_message_with_reply_data(env, tmp_path):
    """The diagnose turn ends on the grid; his next message carries reply_data (text = his bubble):
    the marks are recorded per block. reply_data on a message that resumes nothing is ignored."""
    repo, _s, ids_ = env
    res, ids = opened(env)
    rough = tool(env)
    turns = iter([ChatTurn(text="好，接着细问。"), ChatTurn(text="好的。")])
    gw, prov = gateway(tmp_path, [])
    prov._chat = lambda m, r: next(turns)
    info = SessionInfo(session_id="s1", user_id="u1", page_id=None, note_concept_id=None,
                       created_at=NOW)
    store = MemorySessionStore()
    store.create(info)
    deps = TurnDeps(gateway=gw, tools=[rough], store=store,
                    data=WikiData(repo, lambda: NOW), new_id=ids_, clock=lambda: NOW)
    ev = list(run_turn(deps, info, MessageIn(text="开始诊断", intent="diagnose", draft_id=res.draft.draft_id)))
    ask = next(e for e in ev if e.type == "ask")
    assert ask.kind == "ask_rough" and {b["id"] for b in ask.payload["blocks"]} == set(ids.values())
    marks = {ids["繁殖"]: "dont"}
    ev = list(run_turn(deps, info, MessageIn(text="繁殖：不会", reply_data={"marks": marks})))
    assert ev[-1].type == "done"
    obs = rough_obs(repo)
    assert obs and {o.evidence_ref.rsplit(":", 1)[1] for o in obs} == {ids["繁殖"]}
    assert all((o.polarity, o.strength) == SELF_CLAIM["never"] for o in obs)
    # nothing waits now: reply_data is ignored, no mark is made up
    list(run_turn(deps, info, MessageIn(text="把水养好：会",
                                        reply_data={"marks": {ids["把水养好"]: "know"}})))
    assert len(rough_obs(repo)) == len(obs)


def test_reply_data_is_given_only_to_the_ask_rough_it_resumes():
    from evowiki.assistant.loop import _reply
    from evowiki.assistant.session import PendingAsk
    from evowiki.llm.chat import ChatMessage
    msg = MessageIn(text="好", reply_data={"marks": {"b1": "know"}})
    pend = lambda name: PendingAsk(
        message_id="m0", call=ToolCall(id="c", name=name, args={}),
        pause=Pause(kind="ask_user", payload={}),
        messages=(ChatMessage(role="user", text="x"),))
    assert _reply(msg, pend("ask_rough")).data == {"marks": {"b1": "know"}}
    assert _reply(msg, pend("ask_user")).data == {}


@pytest.mark.parametrize("bad", [{"marks": {"b1": "maybe"}}, {"other": 1}])
def test_reply_data_has_one_shape(bad):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        MessageIn(text="x", reply_data=bad)


def test_the_marks_are_worded_as_the_grid_columns_say_them(env):
    """The grid columns read 掌握 / 熟悉 / 不会; the model hears the same words, and WAITING_LINE
    hands it no diagnosis state to repeat."""
    from evowiki.assistant.tools.rough import MARK_ZH
    from evowiki.assistant.turn_skills import WAITING_LINE
    assert MARK_ZH == {"know": "掌握", "partly": "熟悉", "forgot": "学过但忘了", "dont": "不会"}   # forgot: typed only
    desc = tool(env).spec.description
    assert "掌握 / 熟悉 / 不会" in desc and "学过一点" not in desc
    assert "进行中" not in WAITING_LINE and "没做完" not in WAITING_LINE
