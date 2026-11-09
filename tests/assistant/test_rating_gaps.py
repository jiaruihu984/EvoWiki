"""A 👎 「我说过不会」 / 「没学过」 on a diagnosis question answers it as a gap (assistant/rating_gaps.py):
the pick's cell gets the gap evidence through answer_check, next_check never picks that cell again,
and rating twice records once. Memory repo, scripted chat; nothing paid."""

import pytest

from evowiki.assistant.contracts import MessageIn, RatingIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.rating_gaps import RatingGaps, says_gap
from evowiki.assistant.service import AssistantService
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.assistant.tools.check import open_pick
from evowiki.assistant.wiki import WikiData
from evowiki.belief.aspects import Observe
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import MemoryRepo
from tests.assistant.test_check_tools import quiz, tools
from tests.wikiapp.flat_goal import flat_goal
from tests.wikiapp.goal_script import push_one_page
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import INTAKE_1


@pytest.fixture
def goal():
    """u1 with an open diagnosis (test_check_tools' z 分数 flat goal: the first pick a quiz)."""
    repo, s, ids = MemoryRepo(["u1"]), Script(), Ids()
    s.push("goals.intake", INTAKE_1)
    push_one_page(s, ["c1", "c2"])
    res = flat_goal(repo, CountingGateway(s.gateway()), "u1", "看懂 z 分数", "我会算 z 分数",
                    now=NOW, new_id=ids)
    assert res.open
    return repo, s, ids, res.draft.draft_id
from evowiki.assistant.tools.rough import AskRoughTool
from tests.assistant.test_wiki_adapter import gateway
from tests.wikiapp.test_goal import NOW, Ids


class _NoApplier:
    def apply(self, *a, **k):  # pragma: no cover - never called here
        raise AssertionError


def _asked(goal, tmp_path):
    """A turn that ended on the ask_check question of the open pick; returns the service, the
    session id, the answer's message id and the pick."""
    repo, s, ids, did = goal
    ask = tools(repo, s, ids)
    pick = open_pick(repo.load("u1"))
    turns = iter([ChatTurn(tool_calls=(ToolCall(id="a1", name="ask_check", args={
        "pick_id": pick.pick_id, "quiz": quiz()}),))])
    gw, prov = gateway(tmp_path, [])
    prov._chat = lambda m, r: next(turns)
    info = SessionInfo(session_id="s1", user_id="u1", page_id=None, note_concept_id=None,
                       created_at=NOW)
    store = MemorySessionStore()
    store.create(info)
    rough = AskRoughTool(repo, lambda _u: CountingGateway(s.gateway()), lambda: NOW, ids)
    deps = TurnDeps(gateway=gw, tools=[ask, rough], store=store,
                    data=WikiData(repo, lambda: NOW), new_id=Ids(), clock=lambda: NOW)
    ev = list(run_turn(deps, info, MessageIn(text="开始诊断", intent="diagnose", draft_id=did)))
    assert any(e.type == "ask" for e in ev) and ev[-1].type == "done"
    answer = next(e.payload["message_id"] for e in store.entries("s1") if e.type == "assistant")
    svc = AssistantService(deps, _NoApplier(), rating_gaps=RatingGaps(
        repo, lambda _u: CountingGateway(s.gateway()), lambda: NOW, ids))
    return svc, answer, pick


def _gap_obs(repo, cell):
    return [e for e in repo.aspect_log("u1") if isinstance(e, Observe)
            and e.observation.cell == cell and e.observation.polarity == "-"]


def test_says_gap_reads_the_chip_or_the_note():
    assert says_gap({"value": "down", "chips": ["我说过不会"], "note": ""})
    assert says_gap({"value": "down", "chips": [], "note": "这个我没学过啊"})
    assert not says_gap({"value": "down", "chips": ["问得看不懂"], "note": ""})
    assert not says_gap({"value": "up", "chips": [], "note": "没学过"})


@pytest.mark.parametrize("body", [RatingIn(value="down", chips=("我说过不会",)),
                                  RatingIn(value="down", note="没学过")])
def test_a_down_rating_that_says_he_never_learned_it_records_a_gap_once(goal, tmp_path, body):
    repo = goal[0]
    svc, answer, pick = _asked(goal, tmp_path)
    svc.rate("u1", "s1", answer, body)
    assert len(_gap_obs(repo, pick.cell)) == 1            # 不知道 on the pick's cell
    after = open_pick(repo.load("u1"))
    assert after is None or after.pick_id != pick.pick_id
    assert after is None or after.cell != pick.cell        # next_check does not ask it again
    svc.rate("u1", "s1", answer, body)                     # posted twice: recorded once
    assert len(_gap_obs(repo, pick.cell)) == 1


def test_another_down_rating_records_nothing(goal, tmp_path):
    repo = goal[0]
    svc, answer, pick = _asked(goal, tmp_path)
    before = len(repo.aspect_log("u1"))
    svc.rate("u1", "s1", answer, RatingIn(value="down", chips=("问得看不懂",)))
    assert len(repo.aspect_log("u1")) == before and open_pick(repo.load("u1")) == pick
