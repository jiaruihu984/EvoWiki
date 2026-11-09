"""Planning progress events (assistant-actions Task 9; one pass since stage 4, the writer plans in
its own loop): `planning{pass:1}` → `outline` → sections, written from the workflow body;
`GET /wiki/writes` shows `planning` + `pass` until the outline is out."""

import pytest

from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import organize_stream
from evowiki.wikiapp.page_run_store import MemoryPageRunStore, PageRunRow
from evowiki.wikiapp.page_runs import InlinePageRuns, decode_item, encode_item
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.wiki_schemas import ORGANIZE_PAYLOADS, PlanningEvent
from evowiki.wikiapp.write_queue import write_status
from tests.wikiapp.fake_writer import writing
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_organize import page1, script_page
from tests.wikiapp.test_write_queue import PAGE1, build, no_idle, world  # noqa: F401
from tests.wikiapp.test_write_queue import script_page as script_outline


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


class SpyStore(MemoryPageRunStore):
    def __init__(self):
        super().__init__()
        self.progress_log: list[dict] = []
        self.rows_seen: list = []

    def set_progress(self, run_id, progress):
        self.progress_log.append(dict(progress))
        self.rows_seen.append(self._rows[run_id])
        super().set_progress(run_id, progress)


def run(env, pid, store):
    repo, s, ids = env
    return list(organize_stream(repo, CountingGateway(s.gateway()), "u1", pid, now=NOW,
                                new_id=ids, writing=writing(env),
                                runs=InlinePageRuns(store=store)))


def test_one_planning_event_comes_before_the_outline(env):
    pid = page1(env)
    script_page(env[1])
    items = run(env, pid, SpyStore())
    names = [n for n, _ in items]
    assert names[:3] == ["planning", "outline", "writer"]
    assert [p.pass_ for n, p in items if n == "planning"] == [1]
    for n, p in items:
        assert isinstance(p, ORGANIZE_PAYLOADS[n])


def test_planning_event_wire_name_is_pass_and_survives_the_stream_codec():
    ev = PlanningEvent(pass_=2)
    assert ev.model_dump(mode="json") == {"pass": 2}
    assert ev.model_dump_json() == '{"pass":2}'                       # the SSE / stream wire key
    assert encode_item("planning", ev)["data"] == {"pass": 2}
    name, back = decode_item(encode_item("planning", ev))
    assert (name, back) == ("planning", ev)


def test_an_unread_finished_run_replays_the_same_planning_events(env):
    """A finished run nobody read yet is replayed from its stream: the same items, no new calls."""
    pid = page1(env)
    script_page(env[1])
    repo, s, ids = env
    runs = InlinePageRuns(store=SpyStore())
    kw = {"now": NOW, "new_id": ids, "writing": writing(env), "runs": runs}
    a = organize_stream(repo, CountingGateway(s.gateway()), "u1", pid, **kw)
    b = organize_stream(repo, CountingGateway(s.gateway()), "u1", pid, **kw)
    dump = lambda items: [(n, p.model_dump(mode="json")) for n, p in items]
    first = dump(a)
    assert first == dump(b) and first[0] == ("planning", {"pass": 1})
    assert s.pending() == {}


def test_writes_status_reads_planning_pass_from_the_stored_row_mid_run(world):  # noqa: F811
    """`WriteQueue.status` (what GET /wiki/writes serves) between the run's own progress writes."""
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    [pid] = [next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id]
    script_outline(s, PAGE1)
    seen: list[dict] = []
    real = runs.store.set_progress

    def spy(run_id, progress):
        real(run_id, progress)
        [w] = q.status("u1", [pid])
        seen.append(w)
    runs.store.set_progress = spy
    q.request("u1", [pid])
    jobs.pop()()
    first, second = seen[:2]
    assert (first["state"], first["pass"], first["kind"]) == ("planning", 1, "organize")
    assert second["state"] == "writing" and second["pass"] is None


def test_runs_that_do_not_plan_report_writing_with_their_kind():
    base = {"run_id": "r", "user_id": "u", "page_id": "p", "n": 1, "origin": "open", "job": {},
            "started_at": NOW, "status": "writing", "began_at": NOW}
    for kind in ("feedback", "refine", "rsi_rewrite", "reorder_rewrite"):
        w = write_status(PageRunRow(**base, kind=kind), ())
        assert (w["state"], w["kind"], w["pass"]) == ("writing", kind, None)
    assert write_status(PageRunRow(**base, kind="organize"), ())["state"] == "planning"
