"""`revise_page` (materials phase 6 §7 D): the assistant starts a whole-page draft from materials
through `start_revise` - the `page_revise` row (ask, writing) and a `source_revise` run. Real
Postgres for the ledger, a memory wiki, recorded (not executed) runs, `MemorySourceReader`; no LLM."""

from types import SimpleNamespace

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.assistant.tools import revise as revise_mod
from evowiki.assistant.tools.actions import ActionDeps, build_action_tools
from evowiki.assistant.tools.revise import NO_PAGE, NO_REVISE, RevisePageTool
from evowiki.bot.actions import ActionLedger
from evowiki.sources.reader import MemorySourceReader
from evowiki.wikiapp.events import PageSaved, WikiEvent
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.revise import KIND
from evowiki.wikiapp.source_revise import NOT_STARTED, USER_NOTE, start_revise
from tests.assistant.tools._source_fakes import sec, src
from tests.runtime.helpers import make_user
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized


class Runs:
    """Records the jobs it is asked to start; `fail`: the start raises (no run). Every started run
    is still writing (`store.get`)."""

    def __init__(self):
        self.jobs, self.fail = [], False
        self.store = SimpleNamespace(get=lambda run_id: SimpleNamespace(status="writing")
                                     if run_id in {f"run{i + 1}" for i in range(len(self.jobs))}
                                     else None)

    def start(self, job, deps, *, attach):
        if self.fail:
            raise RuntimeError("queue down")
        self.jobs.append(job)
        return f"run{len(self.jobs)}", True


class Env:
    def __init__(self, pool):
        self.repo, self.ids = MemoryRepo(["u1"]), Ids()
        self.pid = organized((self.repo, Script(), self.ids))
        make_user(pool, "u1")
        self.ledger = ActionLedger(pool, lambda: NOW, self.ids, {})
        self.runs = Runs()
        self.reader = MemorySourceReader({"u1": [(src("src1", "u1", "讲义"), [
            sec("src1-s1", "src1", 0, "z 分数", 1, 1, "〔p.1〕\nz 分数。")])]})
        self.calls = []
        self.deps = ActionDeps(repo=self.repo, ledger=self.ledger, clock=lambda: NOW,
                               new_id=self.ids, revise=self.revise)
        self.tool = RevisePageTool(self.deps)

    def revise(self, uid, page_id, source_ids, key):
        self.calls.append((uid, page_id, tuple(source_ids), key))
        return start_revise(ledger=self.ledger, repo=self.repo, runs=self.runs,
                            deps=lambda: SimpleNamespace(sources=self.reader), user_id=uid,
                            page_id=page_id, source_ids=source_ids, source="assistant", key=key,
                            now=NOW)

    def run(self, ctx=None, **args):
        return self.tool.run(args, ctx or ToolContext(user_id="u1", session_id="s1",
                                                      page_id=self.pid))

    def make_note(self):
        rec = self.repo.load("u1").state.pages[self.pid].model_copy(update={"page": None})
        self.repo.commit(self.repo.load("u1"), Commit(wiki=[WikiEvent(
            event_id=self.ids("wev"), ts=NOW, body=PageSaved(record=rec, action="test"))]))


@pytest.fixture
def env(pool):
    return Env(pool)


@pytest.fixture
def fixed_key(monkeypatch):
    monkeypatch.setattr(revise_mod, "_step_key", lambda ctx: "assistant:wf1:3")


def test_the_current_page_gets_a_writing_draft_row_and_a_confirm_card(env):
    res = env.run()
    assert res.error is None, res.content
    row = env.ledger.get("u1", res.action.action_id)
    assert (row.kind, row.status, row.level, row.source) == (KIND, "proposed", "ask", "assistant")
    assert row.content["state"] == "writing" and row.content["source_ids"] == []
    assert row.page_id == env.pid and row.content["run_id"] == "run1"
    assert res.action.confirm and res.action.status == "proposed"
    assert "确认替换" in res.content and "取消" in res.content and "确认前页面不会变" in res.content
    assert [j.revise.source_ids for j in env.runs.jobs] == [()]      # the run picks the materials


def test_named_materials_go_to_the_run_and_the_key_names_the_user(env, fixed_key):
    res = env.run(source_ids=["src1", "src1"])
    assert res.error is None, res.content
    assert env.runs.jobs[0].revise.source_ids == ("src1",)
    assert env.calls[0][3] == "revise:u1:assistant:wf1:3"


def test_a_replayed_step_returns_the_same_row_and_starts_nothing(env, fixed_key):
    first = env.run()
    again = env.run()
    assert again.action.action_id == first.action.action_id
    assert len(env.runs.jobs) == 1 and len(env.calls) == 1


def test_a_replay_of_a_run_that_never_started_shows_that_row_and_no_second_run(env, fixed_key):
    env.runs.fail = True
    first = env.run()
    assert first.error == NOT_STARTED
    rows = env.ledger.list("u1")
    assert len(rows) == 1 and rows[0].content["state"] == "failed"
    assert not rows[0].content.get("run_id")
    env.runs.fail = False
    again = env.run()
    assert again.error == NOT_STARTED and again.action.action_id == rows[0].action_id
    assert env.runs.jobs == [] and len(env.calls) == 1


def test_a_page_with_an_open_draft_returns_that_draft(env):
    first = env.run()
    second = env.run()
    assert second.error is None and second.action.action_id == first.action.action_id
    assert "已经有一份草稿" in second.content and len(env.runs.jobs) == 1


def test_refusals_write_nothing(env):
    assert env.run(ToolContext(user_id="u1", session_id="s1")).error == NO_PAGE
    assert env.run(source_ids=["nope"]).error
    assert env.run(page_id="no-such-page").error
    env.make_note()
    assert env.run().error == USER_NOTE
    assert env.ledger.list("u1") == [] and env.runs.jobs == []
    no = RevisePageTool(ActionDeps(repo=env.repo, ledger=env.ledger, clock=lambda: NOW,
                                   new_id=env.ids))
    assert no.run({}, ToolContext(user_id="u1", session_id="s1", page_id=env.pid)).error \
        == NO_REVISE


def test_the_tool_is_offered_chat_first_without_a_note_or_a_material_list(env):
    tools = {t.spec.name: t for t in build_action_tools(env.deps)}
    s = tools["revise_page"].spec
    assert set(s.parameters["properties"]) == {"page_id", "source_ids"}
    assert s.acts and "add_source" in s.description and "不要让用户从资料列表里挑" in s.description
    assert "按讲义改" in s.description and "老师讲的不一样" in s.description
