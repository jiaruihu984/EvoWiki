"""Write-all (assistant-actions Task 7): the user's page write queue. Inline runs (a user's runs go one
at a time, lower priority number first, like the DBOS queue), a scripted writer, fake Bot settings /
ledger. Nothing here calls a model."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from evowiki.bot.actions import ActionRefused
from evowiki.bot.executors_write import BotWriteExecutor
from evowiki.bot.settings import BotSettings
from evowiki.wikiapp.events import PagesTrashed, WikiEvent
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import organize_stream
from evowiki.wikiapp.page import mark_done
from evowiki.wikiapp.page_flows import PageDeps
from evowiki.wikiapp.page_run_store import MemoryPageRunStore
from evowiki.wikiapp.page_runs import InlinePageRuns, PageJob
from evowiki.wikiapp.path_edits import edit_path
from evowiki.wikiapp.paths import ahead_pages, next_path_page, path_out, prefetch_out
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.wiki_schemas import PathMoveIn
from evowiki.wikiapp.write_queue import (
    WriteQueue,
    idle_candidates,
    next_idle,
    streak,
    write_status,
)
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.fake_writer import writing
from tests.wikiapp.flat_goal import flat_goal
from tests.wikiapp.goal_script import push_no_question
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_k1_replay import PAGE1, PAGE2, PAGE3
from tests.wikiapp.test_organize import text
from tests.wikiapp.test_paths import BIG_INTAKE, BIG_PATH, big_goal

WRITE = "goals.write"


THERE = datetime.max.replace(tzinfo=UTC)      # he never leaves


@dataclass
class FakeEnv:
    """`IdleEnv`: the Bot's settings, parked goals, caps and busy turn as plain fields."""

    cfg: BotSettings = field(default_factory=lambda: BotSettings(kinds={"idle_write": True}))
    parked_paths: set = field(default_factory=set)
    cap: str | None = None
    busy: bool = False
    seen: datetime | None = THERE
    skips: list = field(default_factory=list)

    def settings(self, user_id):
        return self.cfg

    def parked(self, user_id):
        return self.parked_paths

    def cap_hit(self, user_id, now):
        return self.cap

    def last_seen(self, user_id):
        return self.seen

    def turn_busy(self, user_id):
        return self.busy

    def note_skip(self, user_id, reason, detail, now):
        self.skips.append((reason, detail))


class FakeLedger:
    def __init__(self, level="auto_undo"):
        self.level_ = level
        self.rows: list[dict] = []
        self.proposals: list[dict] = []
        self.keys: dict[str, dict] = {}
        self.all: list = []
        self.updates: list[str] = []

    def level(self, user_id, scope):
        return self.level_

    def record(self, user_id, **kw):
        self.rows.append({"user_id": user_id, **kw})

    def asked_for(self, user_id, page_id, before):
        return None

    def get_by_key(self, key):
        return self.keys.get(key)

    def propose(self, user_id, **kw):
        assert kw["key"] not in self.keys, "a proposal key is used once"
        self.proposals.append(kw)
        self.keys[kw["key"]] = kw
        self.all.append(SimpleNamespace(action_id=f"a{len(self.proposals)}", kind=kw["kind"],
                                        content=kw["content"], status="proposed"))

    @property
    def pending(self):
        return [r for r in self.all if r.status == "proposed"]

    def list(self, user_id, status=None, limit=100):
        """(Like the ledger's: only the newest `limit` rows.)"""
        rows = [r for r in reversed(self.all) if status is None or r.status == status]
        return rows[:limit]

    def batches(self, user_id):
        return [r for r in reversed(self.all) if r.kind == "bot_write" and "picks" in r.content]

    def update_pending(self, user_id, action_id, *, summary, content):
        row = next(r for r in self.all if r.action_id == action_id)
        row.content = content
        self.updates.append(summary)
        return True

    def reject(self, user_id, action_id):
        next(r for r in self.all if r.action_id == action_id).status = "rejected"


def no_idle() -> FakeEnv:
    """The Bot's idle writing switched off: only what the test requests is written."""
    return FakeEnv(cfg=BotSettings(kinds={"idle_write": False}))


def script_page(s, outline, *, fail=None):
    s.push(ORGANIZE, outline)
    for sec in outline["sections"]:
        s.push(WRITE, fail or text(sec["id"]))


@pytest.fixture
def world():
    """A started big goal (three unwritten pages), a queue over inline runs that start only when
    the test runs the captured jobs."""
    repo, s, ids = MemoryRepo(["u1"]), Script(), Ids()
    res, _ = big_goal((repo, s, ids))
    return repo, s, ids, res.path_id


def build(repo, s, ids, *, jobs=None, env=None, ledger=None, tz=UTC, clock=None, cost=0.0,
          can_write=None, off_peak_writer=None):
    runs = InlinePageRuns(spawn=jobs.append if jobs is not None else (lambda w: w()),
                          store=MemoryPageRunStore())
    env = env or FakeEnv()
    holder = {}
    q = WriteQueue(runs, lambda: holder["deps"], repo, clock or (lambda: NOW), ids, env,
                   ledger=ledger, tz=tz, spawn=lambda w: w(), can_write=can_write,
                   off_peak_writer=off_peak_writer)
    holder["deps"] = PageDeps(
        repo=repo, gateway=lambda _u: CountingGateway(s.gateway()), writing=writing(repo, s, cost_usd=cost),
        store=runs.store, clock=clock or (lambda: NOW), new_id=ids, on_write=q.after_write,
        after=q.kick)
    return q, runs, env


def pages_of(repo, path_id):
    return repo.load("u1").state.paths[path_id].page_ids


def written(repo):
    st = repo.load("u1").state
    return [pid for pid in st.page_order if st.pages[pid].page is not None]


# ---- one at a time, the user first -------------------------------------------------------------

def test_two_requests_run_one_at_a_time(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    p2 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    script_page(s, PAGE2)
    a, b = q.request("u1", [p1, p2])
    assert len(jobs) == 1                       # only the first is running
    assert [r.run_id for r in runs.store.live_of("u1")] == [a, b]
    jobs.pop()()                                # page 1 ends -> page 2 starts
    assert len(jobs) == 1 and runs.store.get(a).status == "done"
    assert written(repo) == [p1]
    jobs.pop()()
    assert written(repo) == [p1, p2] and jobs == []


def test_a_user_request_goes_before_idle_work_that_has_not_begun(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    ps = [next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id for _ in range(3)]
    script_page(s, PAGE1)
    script_page(s, PAGE3)                                    # the user's page runs before page 2
    script_page(s, PAGE2)
    first = q.request("u1", [ps[0]], origin="idle")[0]       # running
    idle2 = q.request("u1", [ps[1]], origin="idle")[0]       # waits
    mine = q.request("u1", [ps[2]])[0]                       # the user's: goes before idle2
    jobs.pop()()
    jobs.pop()()
    order = [r.run_id for r in sorted(runs.store._rows.values(), key=lambda r: r.finished_at or NOW)
             if r.status == "done"]
    assert order[0] == first and mine in order and idle2 not in order[:2]
    assert runs.store.get(mine).status == "done" and runs.store.get(idle2).status == "writing"
    jobs.pop()()
    assert runs.store.get(idle2).status == "done"


def test_asking_for_a_page_the_idle_bot_queued_takes_it_over(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    ps = [next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id for _ in range(2)]
    script_page(s, PAGE1)
    script_page(s, PAGE2)
    q.request("u1", [ps[0]], origin="idle")                  # running
    queued_idle = q.request("u1", [ps[1]], origin="idle")[0]
    mine = q.request("u1", [ps[1]])[0]
    assert mine != queued_idle and runs.store.get(queued_idle).discarded_at is not None
    jobs.pop()()
    jobs.pop()()                                             # mine runs
    assert runs.store.get(mine).status == "done"
    jobs.pop()()                                             # the dropped idle one never writes
    assert runs.store.get(queued_idle).status == "failed"
    assert runs.store.get(queued_idle).failure == "cancelled"
    assert written(repo) == [ps[0], ps[1]] and jobs == []


def test_a_page_the_learner_stopped_is_not_written_by_the_idle_bot_until_asked_for(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs)
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    [stopped] = q.request("u1", [p1])
    assert q.cancel("u1", [p1]) == [stopped]
    script_page(s, PAGE2)
    script_page(s, PAGE3)
    while jobs:
        jobs.pop()()
    assert runs.store.get(stopped).failure == "cancelled"
    assert p1 not in written(repo) and len(written(repo)) == 2
    script_page(s, PAGE1)
    q.request("u1", [p1])
    jobs.pop()()
    assert p1 in written(repo)


# ---- which page the idle Bot writes -----------------------------------------------------------

def test_idle_pick_is_the_first_unwritten_page_and_follows_the_edited_order(world):
    repo, _s, ids, path_id = world
    snap = repo.load("u1")
    pick = next_idle(snap, "u1", BotSettings(kinds={"idle_write": True}))
    assert (pick.path_id, pick.index, pick.page_id) == (path_id, 1, None)
    edit_path(repo, "u1", path_id, PathMoveIn(op="move", **{"from": 2}, to=0), now=NOW, new_id=ids)
    pick = next_idle(repo.load("u1"), "u1", BotSettings(kinds={"idle_write": True}))
    assert pick.index == 3                                    # the learner put page 3 first


def test_idle_pick_skips_parked_goals_written_trashed_and_off(world):
    repo, s, ids, path_id = world
    snap = repo.load("u1")
    assert next_idle(snap, "u1", BotSettings(kinds={"idle_write": True}), parked={path_id}) is None      # paused goal
    off = BotSettings(kinds={"idle_write": False})
    assert next_idle(snap, "u1", off) is None
    # a written page and a trashed one (the learner undid the Bot's write) are not picked
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    list(organize_stream(repo, CountingGateway(s.gateway()), "u1", p1, now=NOW, new_id=ids,
                         writing=writing(repo, s), runs=InlinePageRuns(store=MemoryPageRunStore())))
    assert next_idle(repo.load("u1"), "u1", BotSettings(kinds={"idle_write": True})).index == 2
    p2 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    snap = repo.load("u1")
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=PagesTrashed(page_ids=(p2,)))]))
    assert next_idle(repo.load("u1"), "u1", BotSettings(kinds={"idle_write": True})).index == 3          # p2 is `no_auto`
    assert [c.page_id for c in idle_candidates(repo.load("u1").state, exclude={p2})] == [None]


@pytest.mark.parametrize("setting", ["archived", "parked"])
def test_idle_pick_skips_paths_of_inactive_parent_map(world, setting):
    from evowiki.goals.regions import GoalMap, MapNode
    from evowiki.wikiapp.events import GoalMapSaved, GoalSettings, GoalSettingsSet

    repo, _, ids, path_id = world
    m = GoalMap(map_id="parent-map", goal="学统计", created_at=NOW, nodes=(
        MapNode(id="child", kind="concept", name="统计", path_id=path_id),))
    wiki = [WikiEvent(event_id=ids("wev"), ts=NOW, body=GoalMapSaved(record=m, action="planned"))]
    if setting == "archived":
        wiki.append(WikiEvent(event_id=ids("wev"), ts=NOW, body=GoalSettingsSet(
            settings=GoalSettings(goal_id=m.map_id, archived=True))))
    repo.commit(repo.load("u1"), Commit(wiki=wiki))
    parked = {m.map_id} if setting == "parked" else ()
    assert idle_candidates(repo.load("u1").state, parked=parked) == []


def test_next_page_of_each_path_comes_before_the_rest_of_a_path(world):
    repo, s, ids, path_id = world
    # a second goal: its first page comes before the first goal's second page
    s.push("goals.intake", BIG_INTAKE)
    push_no_question(s)
    s.push("goals.path", BIG_PATH)
    res2 = flat_goal(repo, CountingGateway(s.gateway()), "u1", "学另一个统计的东西", "", now=NOW,
                       new_id=ids)
    st = repo.load("u1").state
    cands = idle_candidates(st)
    assert [(c.path_id, c.index) for c in cands[:2]] == [(path_id, 1), (res2.path_id, 1)]


# ---- kick ------------------------------------------------------------------------------------

def test_kick_writes_the_next_page_and_goes_on_until_everything_is_written(world):
    repo, s, ids, path_id = world
    q, runs, _ = build(repo, s, ids)
    for outline in (PAGE1, PAGE2, PAGE3):
        script_page(s, outline)
    run_id = q.kick("u1")
    assert run_id is not None
    assert len(written(repo)) == 3 and all(p is not None for p in pages_of(repo, path_id))
    assert {r.origin for r in runs.store._rows.values()} == {"idle"}
    assert q.kick("u1") is None                      # nothing left


def test_a_written_ahead_page_is_opened_by_next_without_a_new_run(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs)
    script_page(s, PAGE1)
    script_page(s, PAGE2)
    q.kick("u1")
    jobs.pop()()                                   # page 1 written by the idle Bot
    # (kick after the run queued page 2 as well: it is the next thing in the queue)
    p1 = pages_of(repo, path_id)[0]
    snap = repo.load("u1")
    assert snap.state.pages[p1].page is not None
    before = len(runs.store._rows)
    out = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids, runs=runs.store)
    assert (out.page_id, out.prefetched, out.index) == (p1, True, 1)
    assert len(runs.store._rows) == before         # nothing was started
    # organize replays the finished run (no model call) and consumes it
    items = list(organize_stream(repo, CountingGateway(s.gateway()), "u1", p1, now=NOW,
                                 new_id=ids, writing=writing(repo, s), runs=runs))
    assert items[-1][0] == "done" and items[-1][1].prefetched
    out = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids, runs=runs.store)
    assert out.page_id != p1 and out.index == 2    # the next new page, not the one read already


def test_path_out_shows_the_page_written_ahead_and_next_index(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs)
    script_page(s, PAGE1)
    q.kick("u1")
    snap = repo.load("u1")
    ahead = ahead_pages(runs.store, "u1", snap.state)
    rec = snap.state.paths[path_id]
    pf = prefetch_out(ahead, rec)
    assert (pf.index, pf.status) == (1, "writing")
    view = path_out(snap.state, snap.k, path_id, NOW, prefetch=pf, ahead=ahead)
    assert view.next_index == 1 and view.prefetch.status == "writing"
    jobs.pop()()
    snap = repo.load("u1")
    ahead = ahead_pages(runs.store, "u1", snap.state)
    assert prefetch_out(ahead, snap.state.paths[path_id]).status == "ready"


def test_kick_does_nothing_when_busy_off_paused_or_without_a_writer(world):
    repo, s, ids, path_id = world
    q, runs, env = build(repo, s, ids)
    env.busy = True
    assert q.kick("u1") is None                      # an assistant turn is running
    env.busy = False
    env.cfg = BotSettings(kinds={"idle_write": False})
    assert q.kick("u1") is None
    env.cfg = BotSettings(kinds={"idle_write": True}, paused="always")
    assert q.kick("u1") is None
    env.cfg = BotSettings(kinds={"idle_write": True})
    env.seen = None
    assert q.kick("u1") is None                      # an account nobody has in front of him
    env.seen = THERE
    env.parked_paths = {path_id}
    assert q.kick("u1") is None                      # the only goal is paused
    assert runs.store._rows == {} and written(repo) == []


def test_kick_waits_while_a_page_run_is_queued_or_writing(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs)
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    q.request("u1", [p1])                            # the user's own page, running
    assert q.kick("u1") is None and len(runs.store._rows) == 1


def test_three_failures_of_one_kind_in_a_row_stop_idle_writing_for_the_day(world):
    repo, s, ids, _path_id = world
    q, runs, _ = build(repo, s, ids)
    for outline in (PAGE1, PAGE2, PAGE3):      # each page fails: not retried the same day
        s.push(ORGANIZE, outline)
        s.push(WRITE, "UNAVAILABLE")
    q.kick("u1")
    rows = sorted(runs.store._rows.values(), key=lambda r: r.finished_at)
    assert len(rows) == 3 and {r.status for r in rows} == {"failed"}
    assert {r.failure for r in rows} == {"llm_unavailable"}
    assert streak(sorted(rows, key=lambda r: r.finished_at, reverse=True)) == 3
    assert q.kick("u1") is None and len(runs.store._rows) == 3


def test_a_different_failure_breaks_the_streak():
    from evowiki.wikiapp.page_run_store import PageRunRow
    def row(code, n):
        return PageRunRow(run_id=f"r{n}", user_id="u", page_id=f"p{n}", n=1, kind="organize",
                          origin="idle", job={}, status="failed", started_at=NOW,
                          finished_at=NOW + timedelta(minutes=n), failure=code)
    rows = [row("budget_exceeded", 3), row("llm_unavailable", 2), row("llm_unavailable", 1)]
    assert streak(rows) == 1
    assert streak([row("x", 2), row("x", 1)]) == 2
    assert streak([]) == 0


def test_a_cap_stops_idle_writing_and_is_recorded(world):
    repo, s, ids, _path_id = world
    q, runs, env = build(repo, s, ids)
    env.cap = "day"
    assert q.kick("u1") is None
    assert env.skips and env.skips[0][0] == "cap_reached" and "今天" in env.skips[0][1]
    assert runs.store._rows == {}
    env.cap, env.skips = "month", []
    assert q.kick("u1") is None and "这个月" in env.skips[0][1]


def test_a_failed_page_is_not_tried_again_the_same_day_but_the_next_one_is(world):
    repo, s, ids, path_id = world
    q, runs, _ = build(repo, s, ids)
    s.push(ORGANIZE, PAGE1)
    s.push(WRITE, "UNAVAILABLE")
    script_page(s, PAGE2)
    script_page(s, PAGE3)
    q.kick("u1")
    st = repo.load("u1").state
    done = [r for r in runs.store._rows.values() if r.status == "done"]
    assert len(done) == 2 and len([r for r in runs.store._rows.values() if r.status == "failed"]) == 1
    first = st.pages[pages_of(repo, path_id)[0]]      # its outline stays, under the writing mark
    assert not first.has_body and first.page.texts == {}


# ---- the ledger ------------------------------------------------------------------------------

def test_an_idle_write_is_a_bot_write_row_with_its_cost_and_undo(world):
    repo, s, ids, path_id = world
    ledger = FakeLedger()
    q, _runs, _ = build(repo, s, ids, ledger=ledger)
    script_page(s, PAGE1)
    script_page(s, PAGE2)
    script_page(s, PAGE3)
    q.kick("u1")
    assert len(ledger.rows) == 3
    row = ledger.rows[0]
    p1 = pages_of(repo, path_id)[0]
    assert (row["kind"], row["scope"], row["source"]) == ("bot_write", "new_page", "bot")
    assert row["page_id"] == p1 and row["undo"] == {"page_id": p1}
    assert row["result"] == {"cost_usd": 0.0, "calls": 3} and row["key"].startswith("bot_write:")
    assert "第 1 页" in row["why"]


def test_a_page_written_for_the_user_is_a_user_write_row_not_a_bot_write(world):
    repo, s, ids, path_id = world
    ledger = FakeLedger()
    q, _, _ = build(repo, s, ids, ledger=ledger, env=no_idle())
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    q.request("u1", [p1])
    assert written(repo) == [p1]
    [row] = ledger.rows
    assert (row["kind"], row["source"], row["key"].startswith("user_write:")) == ("user_write", "bot", True)
    assert row["undo"] == {"page_id": p1} and row["content"]["origin"] == "open"


def test_undoing_a_bot_write_puts_the_page_back_and_it_is_not_written_again(world, monkeypatch):
    repo, s, ids, _path_id = world
    q, _runs, _env = build(repo, s, ids, ledger=FakeLedger(), env=no_idle())
    script_page(s, PAGE1)
    pick = next_idle(repo.load("u1"), "u1", BotSettings(kinds={"idle_write": True}))
    p1 = q._create("u1", pick)
    q.request("u1", [p1], origin="idle")                 # the Bot's write (no chain: the setting is off)
    assert repo.load("u1").state.pages[p1].page is not None

    marks = []
    monkeypatch.setattr("evowiki.bot.executors_write.store.set_action_status",
                        lambda conn, aid, **kw: marks.append(aid))
    ex = BotWriteExecutor(repo, lambda: NOW, ids, lambda u, picks: [], q.cancel)
    ex.undo("u1", SimpleNamespace(action_id="a1", undo={"page_id": p1}))
    st = repo.load("u1").state
    assert p1 in st.trashed_pages and marks == ["a1"]
    assert [c.index for c in idle_candidates(st)] == [2, 3]          # `no_auto`: page 1 is skipped
    # 重新写 = a request: it writes again and comes back out of the trash
    script_page(s, PAGE1)
    q.request("u1", [p1])
    st = repo.load("u1").state
    assert p1 not in st.trashed_pages and st.pages[p1].page is not None


def test_undo_of_a_finished_page_is_refused(world):
    repo, s, ids, path_id = world
    q, _, _ = build(repo, s, ids, env=no_idle())
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    q.request("u1", [p1])
    mark_done(repo, "u1", p1, now=NOW, new_id=ids)
    ex = BotWriteExecutor(repo, lambda: NOW, ids, lambda u, picks: [], q.cancel)
    with pytest.raises(ActionRefused, match="学完"):
        ex.undo("u1", SimpleNamespace(action_id="a1", undo={"page_id": p1}))


def test_ask_level_proposes_one_batch_and_waits_then_the_approval_writes_them(world):
    repo, s, ids, _path_id = world
    ledger = FakeLedger(level="ask")
    q, runs, _ = build(repo, s, ids, ledger=ledger)
    assert q.kick("u1") is None
    [prop] = ledger.proposals
    assert prop["summary"] == "要生成 3 页" and len(prop["content"]["picks"]) == 3
    assert q.kick("u1") is None and len(ledger.proposals) == 1           # the same set: once
    assert runs.store._rows == {} and written(repo) == []
    for outline in (PAGE1, PAGE2, PAGE3):
        script_page(s, outline)
    run_ids = q.write_batch("u1", prop["content"]["picks"])
    assert len(run_ids) == 3 and len(written(repo)) == 3
    assert {runs.store.get(r).origin for r in run_ids} == {"idle"}


def test_a_merge_gets_told_when_its_page_commits(world):
    repo, s, ids, path_id = world
    q, _, _ = build(repo, s, ids, env=no_idle())
    seen = []
    q.on_committed(lambda uid, pid: seen.append((uid, pid)))
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    q.request("u1", [p1])
    assert seen == [("u1", p1)]


def test_a_failed_run_tells_the_merge_nothing(world):
    repo, s, ids, path_id = world
    q, _, _ = build(repo, s, ids, env=no_idle())
    seen = []
    q.on_committed(lambda uid, pid: seen.append(pid))
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    s.push(ORGANIZE, PAGE1)
    s.push(WRITE, "UNAVAILABLE")
    q.request("u1", [p1])
    assert seen == []


# ---- status ----------------------------------------------------------------------------------

def test_status_goes_queued_to_writing_to_done(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, _runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    ps = [next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id for _ in range(2)]
    script_page(s, PAGE1)
    script_page(s, PAGE2)
    q.request("u1", ps)
    by = {w["page_id"]: w for w in q.status("u1")}
    assert by[ps[0]]["state"] == "queued" and by[ps[1]]["state"] == "queued"
    jobs.pop()()
    out = {w["page_id"]: w for w in q.status("u1", ps)}
    assert out[ps[0]]["state"] == "done" and out[ps[0]]["of"] == 3
    assert out[ps[1]]["state"] == "queued"            # waiting behind
    jobs.pop()()
    assert {w["state"] for w in q.status("u1", ps)} == {"done"}
    assert q.status("u1") == []                       # nothing queued or writing any more


def test_the_cost_so_far_and_the_call_split_are_on_the_row_while_it_writes(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle(), cost=0.3)
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    seen: list[dict] = []
    store = runs.store
    real = store.set_progress
    store.set_progress = lambda run_id, progress: (seen.append(dict(progress)), real(run_id, progress))[1]
    [run_id] = q.request("u1", [p1])
    jobs.pop()()
    costs = [p["cost_usd"] for p in seen if "cost_usd" in p]
    assert costs == sorted(costs) and costs[-1] == pytest.approx(0.3) and any(0 < c < 0.3 for c in costs)
    prog = store.get(run_id).progress
    # no outline-side call any more: the writer outlines in its own loop
    assert prog["section_calls"] == 3 and prog["outline_calls"] == 0 and prog["of"] == 3
    assert q.status("u1", [p1])[0]["cost_usd"] == pytest.approx(0.3)     # the run's final cost


def test_status_of_a_failed_and_an_undone_write():
    from evowiki.wikiapp.page_run_store import PageRunRow
    base = {"run_id": "r", "user_id": "u", "page_id": "p", "n": 1, "kind": "organize",
            "origin": "idle", "job": {}, "started_at": NOW}
    failed = PageRunRow(**base, status="failed", failure="x")
    assert write_status(failed, ()) ["state"] == "failed"
    done = PageRunRow(**base, status="done", cost_usd=0.02)
    assert write_status(done, ())["state"] == "done" and write_status(done, ())["cost_usd"] == 0.02
    assert write_status(done, {"p"})["state"] == "undone"
    planning = PageRunRow(**base, status="writing", began_at=NOW)
    assert write_status(planning, ())["state"] == "planning"
    writing_ = PageRunRow(**base, status="writing", began_at=NOW,
                          progress={"section": 2, "of": 4})
    w = write_status(writing_, ())
    assert (w["state"], w["section"], w["of"]) == ("writing", 2, 4)
    dropped = PageRunRow(**base, status="writing", discarded_at=NOW)
    assert write_status(dropped, ()) is None


def test_the_job_remembers_the_pages_before_it(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    ps = [next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id for _ in range(2)]
    q.request("u1", [ps[1]])
    job = PageJob.model_validate(runs.store.get(f"page:u1:{ps[1]}:1").job)
    assert job.before == (ps[0],) and job.path_id == path_id
    assert job.origin == "open" and job.kind == "organize"


# ---- fix round 1 ------------------------------------------------------------------------------

def test_a_page_written_ahead_is_opened_not_rewritten_however_long_it_waited(world):
    repo, s, ids, path_id = world
    q, runs, _ = build(repo, s, ids)
    script_page(s, PAGE1)
    script_page(s, PAGE2)
    script_page(s, PAGE3)
    q.kick("u1")
    rows = len(runs.store._rows)
    p1 = pages_of(repo, path_id)[0]
    later = NOW + timedelta(days=400)
    out = next_path_page(repo, "u1", path_id, now=later, new_id=ids, runs=runs.store)
    assert (out.page_id, out.prefetched) == (p1, True)
    items = list(organize_stream(repo, CountingGateway(s.gateway()), "u1", p1, now=later,
                                 new_id=ids, writing=writing(repo, s), runs=runs))
    assert items[-1][0] == "done" and len(runs.store._rows) == rows        # replayed, no new run
    assert s.pending() == {}


def test_dropped_runs_are_not_failures_of_the_writing(world):
    repo, s, ids, _path_id = world
    q, runs, _ = build(repo, s, ids, env=None)
    for code, n in (("cancelled", 1), ("unfinishable", 2), ("cancelled", 3)):
        row, _ = runs.store.open_run(user_id="u1", page_id=f"x{n}", kind="organize", origin="idle",
                                     job={}, now=NOW, attach=False)
        runs.store.finish(row.run_id, "failed", NOW + timedelta(minutes=n), failure=code)
    for outline in (PAGE1, PAGE2, PAGE3):
        script_page(s, outline)
    assert q.kick("u1") is not None                      # not three failures in a row
    assert len(written(repo)) == 3


def test_a_second_rewrite_of_a_trashed_page_joins_the_run_in_flight(world):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, jobs=jobs, env=no_idle())
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    q.request("u1", [p1])
    jobs.pop()()
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                        body=PagesTrashed(page_ids=(p1,)))]))
    script_page(s, PAGE1)
    a = q.request("u1", [p1])[0]
    b = q.request("u1", [p1])[0]                          # a double click
    assert a == b and len([r for r in runs.store._rows.values() if r.status == "writing"]) == 1
    jobs.pop()()
    assert p1 not in repo.load("u1").state.trashed_pages


def _flaky(world, fails):
    repo, s, ids, path_id = world
    jobs: list = []
    q, runs, _ = build(repo, s, ids, env=no_idle())
    calls = []

    def hook(job, run_id, status, done):
        calls.append(status)
        if len(calls) <= fails:
            raise RuntimeError("ledger down")
    import dataclasses
    base = q._deps()
    q._deps = lambda: dataclasses.replace(base, on_write=hook)
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    run_id = q.request("u1", [p1])[0]
    return runs, run_id, calls, jobs


def test_a_failing_after_write_step_is_retried_and_the_run_still_ends_done(world):
    runs, run_id, calls, _ = _flaky(world, 2)
    assert calls == ["done"] * 3
    assert runs.store.get(run_id).status == "done"
    assert list(runs.follow(run_id))[-1][0] == "done"


def test_an_after_write_step_that_never_works_fails_the_run_visibly(world):
    runs, run_id, calls, _ = _flaky(world, 99)
    assert len(calls) == 3
    row = runs.store.get(run_id)
    assert (row.status, row.failure) == ("failed", "after_write")
    last = list(runs.follow(run_id))[-1]
    assert last[0] == "error" and "收尾" in last[1].detail


def test_a_run_records_all_it_spent_even_when_it_fails(world):
    repo, s, ids, path_id = world
    q, runs, _ = build(repo, s, ids, env=no_idle(), cost=0.25)
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    s.push(ORGANIZE, PAGE1)
    s.push(WRITE, "UNAVAILABLE")
    run_id = q.request("u1", [p1])[0]
    row = runs.store.get(run_id)
    assert row.status == "failed" and row.cost_usd == pytest.approx(0.25)
    p2 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE2)
    ok = runs.store.get(q.request("u1", [p2])[0])
    assert ok.status == "done" and ok.cost_usd == pytest.approx(0.25)


def test_the_counting_gateway_adds_up_what_its_calls_cost():
    from types import SimpleNamespace
    gw = CountingGateway(SimpleNamespace(
        metered=False, call=lambda role, req, schema: ("x", SimpleNamespace(cost=0.5))))
    gw.call("generator", None, None)
    gw.call("generator", None, None)
    assert (gw.calls, gw.cost) == (2, 1.0)


def test_every_path_change_lets_the_idle_bot_pick_again():
    from evowiki.service.deps import _kick_on_path_changes
    from evowiki.wikiapp.path_edits import notify_path_changed
    seen = []
    _kick_on_path_changes(SimpleNamespace(kick_soon=seen.append))
    rec = SimpleNamespace(path=SimpleNamespace(path_id="p"))
    notify_path_changed("u1", rec, rec, "assistant")
    notify_path_changed("u2", rec, rec, "bot")
    assert seen == ["u1", "u2"]
    _kick_on_path_changes(SimpleNamespace(kick_soon=lambda u: None))      # replaces, not stacks


def test_one_waiting_batch_follows_the_pages_still_to_write(world):
    repo, s, ids, path_id = world
    ledger = FakeLedger(level="ask")
    q, _, _ = build(repo, s, ids, ledger=ledger)
    q.kick("u1")
    assert [p["summary"] for p in ledger.proposals] == ["要生成 3 页"]
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    q.request("u1", [p1])                                 # the learner asks for page 1 himself
    q.kick("u1")
    assert len(ledger.proposals) == 1 and ledger.updates == ["要生成 2 页"]
    assert len(ledger.pending) == 1 and len(ledger.pending[0].content["picks"]) == 2


def test_no_model_or_writer_means_no_idle_write(world):
    repo, s, ids, _ = world
    q, runs, _ = build(repo, s, ids, can_write=lambda uid: False)
    assert q.kick("u1") is None and runs.store._rows == {}


def _ask_world(world):
    repo, s, ids, path_id = world
    ledger = FakeLedger(level="ask")
    q, _runs, _ = build(repo, s, ids, ledger=ledger)
    return repo, s, ids, path_id, ledger, q


def _start_page_one(repo, s, ids, path_id, q):
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    q.request("u1", [p1])
    return p1


def test_a_rejected_set_is_never_asked_again_even_after_the_pending_row_was_updated(world):
    repo, s, ids, path_id, ledger, q = _ask_world(world)
    q.kick("u1")                                           # 3 pages proposed
    _start_page_one(repo, s, ids, path_id, q)
    q.kick("u1")                                           # the row now says 2 pages (updated)
    assert ledger.updates == ["要生成 2 页"]
    ledger.reject("u1", ledger.pending[0].action_id)       # the learner says no
    q.kick("u1")
    q.kick("u1")
    assert ledger.pending == [] and len(ledger.proposals) == 1


def test_a_set_that_changes_back_does_not_leave_a_stale_pending_row(world):
    repo, _s, ids, path_id, ledger, q = _ask_world(world)
    q.kick("u1")
    first = {p["key"] for p in ledger.pending[0].content["picks"]}
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    snap = repo.load("u1")
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=PagesTrashed(page_ids=(p1,)))]))
    q.kick("u1")                                           # B: page 1 is out of reach
    assert len(ledger.pending) == 1 and len(ledger.pending[0].content["picks"]) == 2
    snap = repo.load("u1")
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=PagesTrashed(page_ids=(p1,), trashed=False))]))
    q.kick("u1")                                           # back to A (page 1 unwritten again)
    [row] = ledger.pending
    assert {p["key"] for p in row.content["picks"]} == first and len(ledger.proposals) == 1


def test_a_double_rewrite_from_two_threads_is_one_run(world):
    import threading
    repo, s, ids, path_id = world
    q, runs, _ = build(repo, s, ids, jobs=[], env=no_idle())
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                        body=PagesTrashed(page_ids=(p1,)))]))
    out = []
    ts = [threading.Thread(target=lambda: out.append(q.request("u1", [p1])[0])) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(out)) == 1 and len(runs.store._rows) == 1


def test_an_idle_page_whose_bookkeeping_failed_is_just_opened(world):
    repo, s, ids, path_id = world
    q, runs, _ = build(repo, s, ids, env=no_idle())
    base = q._deps()
    import dataclasses

    def broken(job, run_id, status, done):
        raise RuntimeError("down")
    q._deps = lambda: dataclasses.replace(base, on_write=broken)
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    run_id = q.request("u1", [p1], origin="idle")[0]
    assert runs.store.get(run_id).failure == "after_write"
    assert repo.load("u1").state.pages[p1].page is not None            # committed
    out = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids, runs=runs.store)
    assert (out.page_id, out.prefetched) == (p1, True)
    rows = len(runs.store._rows)
    items = list(organize_stream(repo, CountingGateway(s.gateway()), "u1", p1, now=NOW,
                                 new_id=ids, writing=writing(repo, s), runs=runs))
    assert [n for n, _ in items] == ["done"] and len(runs.store._rows) == rows   # no new write
    assert s.pending() == {}


def test_undoing_a_bot_write_drops_its_unread_run_so_the_trashed_page_is_not_replayed(
        world, monkeypatch):
    repo, s, ids, path_id = world
    q, runs, _ = build(repo, s, ids, ledger=FakeLedger(), env=no_idle())
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    run_id = q.request("u1", [p1], origin="idle")[0]
    monkeypatch.setattr("evowiki.bot.executors_write.store.set_action_status",
                        lambda conn, aid, **kw: None)
    ex = BotWriteExecutor(repo, lambda: NOW, ids, lambda u, picks: [], q.cancel, q.forget)
    ex.undo("u1", SimpleNamespace(action_id="a1", undo={"page_id": p1}))
    assert runs.store.get(run_id).consumed_at is not None
    script_page(s, PAGE1)
    items = list(organize_stream(repo, CountingGateway(s.gateway()), "u1", p1, now=NOW,
                                 new_id=ids, writing=writing(repo, s), runs=runs))
    assert items[-1][0] == "done" and not items[-1][1].prefetched      # written anew
    assert p1 not in repo.load("u1").state.trashed_pages


def test_a_trashed_page_or_a_dropped_row_is_not_opened_by_the_bookkeeping_shortcut(world):
    import dataclasses
    repo, s, ids, path_id = world
    q, runs, _ = build(repo, s, ids, env=no_idle())
    base = q._deps()

    def broken(job, run_id, status, done):
        raise RuntimeError("down")
    q._deps = lambda: dataclasses.replace(base, on_write=broken)
    p1 = next_path_page(repo, "u1", path_id, now=NOW, new_id=ids).page_id
    script_page(s, PAGE1)
    q.request("u1", [p1], origin="idle")
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                                        body=PagesTrashed(page_ids=(p1,)))]))
    script_page(s, PAGE1)
    items = list(organize_stream(repo, CountingGateway(s.gateway()), "u1", p1, now=NOW,
                                 new_id=ids, writing=writing(repo, s), runs=runs))
    assert items[-1][0] == "done" and not items[-1][1].prefetched      # written, not "opened"


# ---- off-peak (spec 2026-10-21-offpeak-writes) -------------------------------------------------

PEAK = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)          # Thursday 17:00 Beijing
OFF_PEAK = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)     # Thursday 20:00 Beijing
DEEPSEEK = lambda _uid: True                            # the writer has an off-peak rate


def test_at_peak_a_deepseek_writer_writes_only_the_next_page_and_the_rest_off_peak(world):
    repo, s, ids, path_id = world
    now = [PEAK]
    q, runs, _ = build(repo, s, ids, clock=lambda: now[0], off_peak_writer=DEEPSEEK)
    for outline in (PAGE1, PAGE2, PAGE3):
        script_page(s, outline)
    assert q.kick("u1") is not None
    assert written(repo) == [pages_of(repo, path_id)[0]]   # page 1 (the reader's next page) only
    assert q.kick("u1") is None and len(written(repo)) == 1
    now[0] = OFF_PEAK                                      # the window opens: the Bot's tick
    assert q.kick("u1") is not None
    assert len(written(repo)) == 3
    assert {r.origin for r in runs.store._rows.values()} == {"idle"}


def test_a_writer_without_an_off_peak_rate_is_never_held(world):
    repo, s, ids, _ = world
    q, _, _ = build(repo, s, ids, clock=lambda: PEAK, off_peak_writer=lambda _uid: False)
    for outline in (PAGE1, PAGE2, PAGE3):
        script_page(s, outline)
    q.kick("u1")
    assert len(written(repo)) == 3


def test_the_next_page_moves_with_the_reader(world):
    repo, s, ids, path_id = world
    q, runs, _ = build(repo, s, ids, clock=lambda: PEAK, off_peak_writer=DEEPSEEK)
    script_page(s, PAGE1)
    q.kick("u1")
    p1 = pages_of(repo, path_id)[0]
    # the reader reads page 1 (its run is consumed): page 2 is the next page, written at peak
    list(organize_stream(repo, CountingGateway(s.gateway()), "u1", p1, now=PEAK, new_id=ids,
                         writing=writing(repo, s), runs=runs))
    script_page(s, PAGE2)
    assert q.kick("u1") is not None
    assert len(written(repo)) == 2 and q.kick("u1") is None      # page 3 waits


def test_a_held_page_the_user_asks_for_is_written_at_once(world):
    repo, s, ids, path_id = world
    q, runs, _ = build(repo, s, ids, clock=lambda: PEAK, off_peak_writer=DEEPSEEK)
    script_page(s, PAGE1)
    q.kick("u1")                                           # page 1 written ahead
    p2 = next_path_page(repo, "u1", path_id, now=PEAK, new_id=ids).page_id   # (no runs: page 2)
    assert p2 != pages_of(repo, path_id)[0]
    script_page(s, PAGE2)
    [run_id] = q.request("u1", [p2])
    assert runs.store.get(run_id).status == "done" and len(written(repo)) == 2


def test_a_held_write_starts_only_with_twenty_minutes_of_window_ahead(world):
    repo, s, ids, _ = world
    now = [datetime(2026, 10, 8, 0, 45, tzinfo=UTC)]       # 08:45 Beijing: peak at 09:00
    q, _, _ = build(repo, s, ids, clock=lambda: now[0], off_peak_writer=DEEPSEEK)
    for outline in (PAGE1, PAGE2, PAGE3):
        script_page(s, outline)
    q.kick("u1")
    assert len(written(repo)) == 1
    now[0] = datetime(2026, 10, 8, 0, 30, tzinfo=UTC)      # 08:30: a page fits before 09:00
    q.kick("u1")
    assert len(written(repo)) == 3


def test_ask_level_at_peak_queues_the_next_page_and_leaves_the_rest_to_off_peak(world):
    repo, s, ids, _ = world
    now = [PEAK]
    ledger = FakeLedger(level="ask")
    q, _, _ = build(repo, s, ids, clock=lambda: now[0], ledger=ledger, off_peak_writer=DEEPSEEK)
    assert q.kick("u1") is None
    [prop] = ledger.proposals
    assert len(prop["content"]["picks"]) == 3              # the learner approves all three
    for outline in (PAGE1, PAGE2, PAGE3):
        script_page(s, outline)
    ledger.all[0].status = "approved"
    run_ids = q.write_batch("u1", prop["content"]["picks"])
    ledger.all[0].status = "executed"
    assert len(run_ids) == 1 and len(written(repo)) == 1
    assert q.kick("u1") is None
    assert len(ledger.proposals) == 1 and ledger.updates == []   # not asked again
    now[0] = OFF_PEAK
    q.kick("u1")
    assert len(written(repo)) == 3 and len(ledger.proposals) == 1
