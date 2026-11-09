"""One wiki snapshot per assistant turn (`wikiapp.repo.TurnRepo`, opened by `TurnDeps.turn_scope`):
a turn's ports (tool list, read tools, record_evidence, an applied edit) replay the user's logs once,
a write that lands meanwhile is still seen, and nothing is remembered outside a turn."""

import itertools

import pytest

from evowiki.agent.tools import ProposalDraft
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.assistant.tools.evidence import RecordEvidenceTool
from evowiki.assistant.wiki import WikiApplier, WikiData, WikiTools
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.wikiapp.events import Guideline, GuidelineAdded, WikiEvent
from evowiki.wikiapp.repo import Commit, MemoryRepo, TurnRepo
from tests.assistant.test_wiki_adapter import clock, gateway
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized

EV = itertools.count()


@pytest.fixture
def seeded():
    """u1 with one organized page (test_wiki_adapter's `wiki`, its organized page only)."""
    env = (MemoryRepo(["u1"]), Script(), Ids())
    return env[0], organized(env)


class Counting(MemoryRepo):
    """A MemoryRepo that counts full loads (each one replays both logs)."""

    loads = 0

    def load(self, user_id):
        self.loads += 1
        return super().load(user_id)


def counting(mem: MemoryRepo) -> Counting:
    return Counting.of_logs("u1", mem.wiki_log("u1"), mem.aspect_log("u1"))


def typical_turn(repo, tmp_path, pid, *, scope):
    """Tool list, two read tools (read_k, read_page), record_evidence's commit, the answer, then
    the accepted edit applied - all for u1 under `scope`."""
    gw, _ = gateway(tmp_path, [
        ChatTurn(tool_calls=(ToolCall(id="c1", name="read_k", args={"concepts": ["c1"]}),
                             ToolCall(id="c2", name="read_page", args={}),
                             ToolCall(id="c3", name="record_evidence", args={"events": [
                                 {"cell": "c1.interpret", "kind": "ask", "polarity": "-",
                                  "quote": "z=-1 是什么意思"}]}))),
        ChatTurn(text="比均值低一个标准差。")])
    wiki_tools = WikiTools(repo)
    store = MemorySessionStore()
    info = SessionInfo(session_id="s1", user_id="u1", page_id=pid, note_concept_id=None,
                       created_at=NOW)
    store.create(info)
    deps = TurnDeps(gateway=gw, tools=lambda i, now: [*wiki_tools(i, now),
                                                      RecordEvidenceTool(repo, clock, lambda p: f"rx-{p}-{next(EV)}")],
                    store=store, data=WikiData(repo, clock), new_id=Ids(), clock=clock,
                    turn_scope=scope)
    draft = ProposalDraft(kind="add_section", page_id=pid, heading="补充：z=-1",
                          text="z=-1 表示比均值低一个标准差。", why="刚问过")
    with scope("u1"):
        ev = list(run_turn(deps, info, MessageIn(text="z=-1 是什么意思？")))
        applied = WikiApplier(repo, clock, Ids()).apply("u1", draft)
    return ev, applied


def test_a_turn_replays_the_logs_once(seeded, tmp_path):
    mem, pid = seeded
    plain = counting(mem)
    ev, applied = typical_turn(plain, tmp_path, pid, scope=TurnRepo(plain).turn)
    assert next(e for e in ev if e.type == "observed").observations == 1 and applied
    before = plain.loads                   # the ports on the plain repo: a replay per read

    inner = counting(mem)
    shared = TurnRepo(inner)
    ev, applied = typical_turn(shared, tmp_path, pid, scope=shared.turn)
    assert next(e for e in ev if e.type == "observed").observations == 1 and applied
    assert inner.loads == 1 < before, before
    # same outcome either way: the edit and the observation landed
    assert inner.load("u1").state.to_json() != mem.load("u1").state.to_json()
    assert len(inner.aspect_log("u1")) == len(plain.aspect_log("u1"))


def test_a_write_meanwhile_is_seen_and_nothing_outlives_the_turn(seeded):
    mem, _ = seeded
    inner = counting(mem)
    tr = TurnRepo(inner)
    tr.load("u1"), tr.load("u1")
    assert inner.loads == 2                # no turn open: plain delegate
    with tr.turn("u1"):
        a = tr.load("u1")
        assert tr.load("u1") is a and inner.loads == 3
        # another writer (an action executor on its own repo) lands a guideline
        inner.commit(inner.load("u1"), Commit(wiki=[WikiEvent(
            event_id="g_meanwhile", ts=NOW, body=GuidelineAdded(guideline=Guideline(
                guideline_id="g9", text="多举例", source="user")))]))
        b = tr.load("u1")                  # head moved: one fresh load
        assert b is not a and "g9" in {g.guideline_id for g in b.state.guidelines}
        c = tr.commit(b, Commit(wiki=[WikiEvent(
            event_id="g_mine", ts=NOW, body=GuidelineAdded(guideline=Guideline(
                guideline_id="g10", text="先结论", source="user")))]))
        n = inner.loads
        assert tr.load("u1") is c and inner.loads == n       # the commit's snapshot is kept
    n = inner.loads
    tr.load("u1")
    assert inner.loads == n + 1            # closed: forgotten
