"""Replays of the runs most signals point at (final review I1): a page's first write (organize gives the
writer a fresh outline with no texts) and a 「再讲深一点」 rewrite (kchange gives it the feedback outline).
Neither page is in the run's snapshot, so the run traces the page it was given; `select_cases` checks
the section against it and `replay` writes from it. Real organize / feedback paths on a `pool`,
scripted providers only."""

from dataclasses import replace
from datetime import timedelta

import pytest

from evowiki.deep.store import MemoryAssetStore, PgAssetStore, PgJobStore
from evowiki.deep.writer import PageWriter
from evowiki.harness.cases import select_cases
from evowiki.harness.diff import PackDiff
from evowiki.harness.pack import EMPTY
from evowiki.harness.replay import ReplayDeps, replay
from evowiki.store import trace
from evowiki.teach.contracts import Signal, TeachingSpec
from evowiki.wikiapp.kchange import feedback_rewrite_stream
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import Writing, organize_stream
from evowiki.wikiapp.refine_service import DeepService
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import PgRepo
from tests.deep.fakes import DeepScript, FakeChecker, finish, library, section, turn
from tests.runtime.helpers import make_user
from tests.wikiapp.runs import runs_for
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_organize import OUTLINE, page1

LATER = NOW + timedelta(days=1)


class World:
    def __init__(self, pool):
        make_user(pool, "u1")
        self.pool, self.repo, self.s, self.ids = pool, PgRepo(pool), Script(), Ids()
        self.env = (self.repo, self.s, self.ids)

    def traced(self, ds: DeepScript):
        return replace(ds.llm(), trace=trace.PgTraceSink(self.pool, lambda: NOW))

    def writing(self, ds: DeepScript) -> Writing:
        return Writing(writer=PageWriter(llm=self.traced(ds), checker=FakeChecker(),
                                         library=library()),
                       specs=WikiSpecSource(self.repo), assets=MemoryAssetStore())

    def first_write(self) -> str:
        pid = page1(self.env)
        ds = DeepScript().plan(outline=OUTLINE).write(*[
            turn(section(x["id"], 1, heading=x["heading"], role=x["role"]))
            for x in OUTLINE["sections"] if x["role"] != "check"], turn(finish(1)))
        items = list(organize_stream(self.repo, CountingGateway(self.s.gateway()), "u1", pid,
                                     now=NOW, new_id=self.ids, writing=self.writing(ds),
                                     runs=runs_for(self.repo)))
        assert items[-1][0] == "done", items[-1]
        return pid

    def cases(self, pid: str, sid: str):
        sig = Signal(signal_id="g1", kind="page_feedback", at=LATER, page_id=pid, section_id=sid,
                     chips=("unclear",))
        with self.pool.connection() as conn:
            return select_cases(conn, "u1", [sig], TeachingSpec(version=1, items=()),
                                now=LATER + timedelta(hours=1), repo=self.repo)

    def replay(self, case, ds: DeepScript):
        svc = DeepService(repo=self.repo, jobs=PgJobStore(self.pool),
                          assets=PgAssetStore(self.pool), specs=WikiSpecSource(self.repo),
                          checker=FakeChecker(), llm=self.traced(ds), clock=lambda: NOW,
                          new_id=self.ids, library=library())
        s = case.snapshot
        spec = WikiSpecSource(self.repo).spec(
            self.repo.load_at("u1", s.wiki_seq, s.aspect_seq, s.infer_version))
        return replay(case, PackDiff(), base_pack=EMPTY, base_spec=spec,
                      deps=ReplayDeps(deep=svc, repo=self.repo))

    def run_input(self, run_id: str) -> dict:
        with self.pool.connection() as conn:
            return trace.run(conn, run_id)["input"]


@pytest.fixture
def world(pool):
    return World(pool)


def test_a_first_write_is_no_replay_case_since_the_writer_outlines_it(world):
    """Stage 4: a first write's input has no page (the writer outlines it with plan_page inside
    the round), so its trace carries no page to replay from and the frozen harness
    (`select_cases`) yields no case for it. Known gap, reported with stage 4; rewrites still
    replay (below)."""
    pid = world.first_write()
    out = world.cases(pid, "s2")
    assert out.problem == ()
    runs = [r for r in _runs(world) if r["input"].get("mode") == "page"]
    assert runs and all(r["input"]["page"] is None for r in runs)


def _runs(world) -> list[dict]:
    with world.pool.connection() as conn:
        ids = [r["run_id"] for r in conn.execute("select run_id from agent_run where user_id = 'u1'")]
        return [trace.run(conn, rid) for rid in ids]


def test_a_feedback_rewrite_is_replayed_from_its_feedback_outline(world):
    pid = world.first_write()
    stored = world.repo.load("u1").state.pages[pid].page
    assert {x.id: x.role for x in stored.outline.sections}["s3"] == "recap"
    ds = DeepScript().plan().write(turn(section("s3", 1, heading="标准差提醒")), turn(finish(1)))
    items = list(feedback_rewrite_stream(world.repo, CountingGateway(world.s.gateway()), "u1", pid,
                                         "s3", "deeper", now=NOW + timedelta(hours=1),
                                         new_id=world.ids, writing=world.writing(ds),
                                         runs=runs_for(world.repo)))
    assert items[-1][0] == "done", items[-1]
    (case,) = world.cases(pid, "s3").problem
    assert case.mode == "feedback" and case.instruction
    roles = {x["id"]: x["role"] for x in case.page["outline"]["sections"]}
    assert roles["s3"] == "explain"                 # 「再讲深一点」 made it an explain section
    assert case.page["texts"]["s3"]                  # written against the text it had
    res = world.replay(case, DeepScript().plan().write(
        turn(section("s3", 2, heading="标准差提醒")), turn(finish(2))))
    assert "s3 精修正文" in res.text
    got = world.run_input(res.run_id)
    assert got["page"]["outline"] == case.page["outline"] and got["mode"] == "feedback"
