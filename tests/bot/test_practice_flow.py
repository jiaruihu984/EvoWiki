"""add_practice's practice run on DBOS (stage 4 T5): its own workflow `practice:<step key>`, every
model call and tool call a recorded step, the same key returning the same draft without a second
run. Real Postgres + DBOS, scripted writer only."""

import pytest

from evowiki.bot.practice import DeepPracticeWriter
from evowiki.deep.store import MemoryAssetStore, sha256
from evowiki.deep.writer import PageWriter
from evowiki.wikiapp import page_flows
from evowiki.wikiapp.organize import Writing
from evowiki.wikiapp.page_flows import PageDeps
from evowiki.wikiapp.page_runs import InlinePageRuns
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import PgRepo
from tests.deep.fakes import PRACTICE_BLOCK, DeepScript, FakeChecker, call, library, turn
from tests.runtime.helpers import make_user
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized


@pytest.fixture
def dbos_env(db_url, pool):
    from evowiki.agent import dbos_app

    make_user(pool, "u1")
    repo, ids, ds, checker = PgRepo(pool), Ids(), DeepScript(), FakeChecker()
    kit = Writing(writer=PageWriter(llm=ds.llm(), checker=checker, library=library()),
                  specs=WikiSpecSource(repo), assets=MemoryAssetStore())
    page_flows.install(PageDeps(repo=repo, gateway=lambda _u: None, writing=kit,
                                store=InlinePageRuns().store, clock=lambda: NOW, new_id=ids))
    pid = organized((repo, Script(), ids))
    dbos_app.launch(db_url, app_version="t")
    yield repo, ids, ds, checker, kit, pid
    dbos_app.shutdown()
    page_flows._deps = None


def test_the_practice_run_is_a_workflow_of_steps_and_a_replayed_key_gets_the_same_draft(dbos_env):
    from dbos import DBOS

    repo, ids, ds, checker, kit, pid = dbos_env
    ds.write(turn(call("write_block", block_id="kana", section_id="s2", why="练熟：认读假名",
                       block=PRACTICE_BLOCK)),
             turn(call("finish", interactive_id="kana")))
    w = DeepPracticeWriter(repo, lambda: kit, kit.assets, lambda: NOW, ids, on_dbos=True)
    d = w.compose("u1", pid, "card", "假名", "assistant:t1")
    assert d.section_id == "s2" and d.check.ok and d.block is not None
    steps = [st["function_name"] for st in DBOS.list_workflow_steps("practice:assistant:t1")]
    assert sum("deep.write" in s for s in steps) == 2           # each model call a step
    # write_block / finish are draft tools (replayed in the body); the render check is a step
    assert steps == ["repo.load", "harness.pack", "llm.converse:deep.write", "render.check_block",
                     "llm.converse:deep.write", "new_id"], steps
    served = len(ds.served)
    again = w.compose("u1", pid, "card", "假名", "assistant:t1")      # a replayed tool step
    assert again == d and len(ds.served) == served and checker.checked == ["kana"]


def test_a_drafted_app_block_is_stored_so_its_inline_preview_counts_as_generated(dbos_env):
    repo, ids, ds, _checker, kit, pid = dbos_env
    app = {"type": "evoui", "title": "t", "question": "练熟：q", "state": {}, "derived": {}, "fallback": "f",
           "body": {"kind": "app", "html": "<p>x</p><script>evowiki.ready()</script>"}}
    ds.write(turn(call("write_block", block_id="kana", section_id="s2", why="练熟：认读假名", block=app)),
             turn(call("finish", interactive_id="kana")))
    w = DeepPracticeWriter(repo, lambda: kit, kit.assets, lambda: NOW, ids, on_dbos=True)
    d = w.compose("u1", pid, "card", "假名", "assistant:app")
    assert d.block is not None and kit.assets.generated("u1", "evoui", sha256(d.block))
    assert not kit.assets.generated("someone-else", "evoui", sha256(d.block))
