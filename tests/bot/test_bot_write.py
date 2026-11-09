"""`bot_write` in the real action ledger (assistant-actions Task 7): a page the Bot wrote is a row with
its cost that can be undone (the page goes to the trash and is not written again); at the `ask`
level the Bot proposes one 「要写 N 页」 row and one approval queues the whole batch. Postgres, a
seeded path, runs that never start (nothing here calls a model)."""

from datetime import UTC, datetime

import pytest

from evowiki.bot.actions import ActionLedger, NotDecidable, can_undo
from evowiki.bot.diffs import diff_for
from evowiki.bot.executors_write import BotWriteExecutor
from evowiki.bot.settings import BotSettings
from evowiki.store import bot as store
from evowiki.wikiapp.events import PagesTrashed, WikiEvent
from evowiki.wikiapp.goal import page_events_for
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.page_flows import PageDeps
from evowiki.wikiapp.page_run_store import MemoryPageRunStore
from evowiki.wikiapp.page_runs import InlinePageRuns
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.write_queue import WriteQueue
from tests.runtime.helpers import make_user
from tests.wikiapp.fake_writer import writing
from tests.wikiapp.script import Script
from tests.wikiapp.test_path_edits import NOW, PATH_ID, Ids, seed_events

ASK = BotSettings(kinds={"idle_write": True}, levels={"new_page": "ask"})


class Quiet:
    """`IdleEnv` with nothing in the way."""

    def settings(self, user_id):
        return BotSettings(kinds={"idle_write": True})

    def parked(self, user_id):
        return ()

    def cap_hit(self, user_id, now):
        return None

    def last_seen(self, user_id):
        return datetime.max.replace(tzinfo=UTC)

    def turn_busy(self, user_id):
        return False

    def note_skip(self, *a):
        raise AssertionError("not skipped")


@pytest.fixture
def world(pool):
    make_user(pool, "u1")
    ids, repo, script = Ids(), PgRepo(pool), Script()
    aspect, wiki = seed_events(ids)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    ledger = ActionLedger(pool, lambda: NOW, ids, {})
    jobs: list = []
    runs = InlinePageRuns(spawn=jobs.append, store=MemoryPageRunStore())
    holder = {}
    queue = WriteQueue(runs, lambda: holder["deps"], repo, lambda: NOW, ids, Quiet(),
                       ledger=ledger, spawn=lambda w: w())
    holder["deps"] = PageDeps(repo=repo, gateway=lambda _u: CountingGateway(script.gateway()),
                              writing=writing(repo, script), store=runs.store, clock=lambda: NOW,
                              new_id=ids, on_write=queue.after_write, after=queue.kick)
    def merged(uid, pid):
        with pool.connection() as conn:
            return store.merge_making(conn, uid, pid) is not None
    ex = BotWriteExecutor(repo, lambda: NOW, ids, queue.write_batch, queue.cancel,
                          merged_by=merged)
    ledger.register("bot_write", ex)
    ledger.register("user_write", ex)
    return type("W", (), {"pool": pool, "repo": repo, "ids": ids, "ledger": ledger,
                          "queue": queue, "runs": runs, "jobs": jobs})


def written_page(w, title="抽样分布"):
    """A page the Bot wrote (a wiki page with text) and its executed `bot_write` row."""
    snap = w.repo.load("u1")
    rec, evs = page_events_for(snap.state, snap.k, title=title, goal=title,
                               concepts=(), targets=(), now=NOW, new_id=w.ids, user_id="u1")
    w.repo.commit(snap, Commit(wiki=evs))
    snap = w.repo.load("u1")
    page = snap.state.pages[rec.page_id]
    assert page.page is None
    w.ledger.record("u1", kind="bot_write", scope="new_page", source="bot",
                    summary=f"写好了「{title}」", content={"page_id": rec.page_id},
                    key=f"bot_write:{rec.page_id}", result={"cost_usd": 0.02, "calls": 5},
                    undo={"page_id": rec.page_id}, page_id=rec.page_id)
    return rec.page_id


def test_a_bot_write_row_can_be_undone_and_the_page_is_in_the_trash(world):
    pid = written_page(world)
    [row] = world.ledger.list("u1", status="executed", writes=True)
    assert row.kind == "bot_write" and row.result == {"cost_usd": 0.02, "calls": 5}
    assert can_undo(row)
    undone = world.ledger.undo("u1", row.action_id)
    assert undone.status == "undone"
    assert pid in world.repo.load("u1").state.trashed_pages
    assert not can_undo(undone)


def test_a_batch_proposal_has_no_undo_and_lists_its_pages(world):
    with world.pool.connection() as conn:
        store.put_settings(conn, "u1", ASK, NOW)
    assert world.queue.kick("u1") is None                          # ask: nothing is written
    [prop] = world.ledger.list("u1", status="proposed")
    assert (prop.kind, prop.scope, prop.level, prop.summary) == (
        "bot_write", "new_page", "ask", "要生成 3 页")
    assert [p["title"] for p in prop.content["picks"]] == ["第 1 页", "第 2 页", "第 3 页"]
    assert [ln["text"] for ln in diff_for(prop, None)] == ["第 1 页", "第 2 页", "第 3 页"]
    assert not can_undo(prop)
    assert world.queue.kick("u1") is None
    assert len(world.ledger.list("u1", status="proposed")) == 1    # the same set: asked once
    assert world.runs.store.live_of("u1") == []


def test_approving_the_batch_creates_and_queues_every_page_as_idle_work(world):
    with world.pool.connection() as conn:
        store.put_settings(conn, "u1", ASK, NOW)
    world.queue.kick("u1")
    [prop] = world.ledger.list("u1", status="proposed")
    done = world.ledger.approve("u1", prop.action_id)
    assert done.status == "executed" and done.result["requested"] == 3
    live = world.runs.store.live_of("u1")
    assert len(live) == 3 and {r.origin for r in live} == {"idle"}
    rec = world.repo.load("u1").state.paths[PATH_ID]
    assert all(p is not None for p in rec.page_ids)                # the rows got their pages
    assert len(world.jobs) == 1                                    # one at a time
    with pytest.raises(NotDecidable):
        world.ledger.undo("u1", done.action_id)                    # nothing to take back


def test_rejecting_the_batch_writes_nothing_and_is_not_asked_again(world):
    with world.pool.connection() as conn:
        store.put_settings(conn, "u1", ASK, NOW)
    world.queue.kick("u1")
    [prop] = world.ledger.list("u1", status="proposed")
    assert world.ledger.reject("u1", prop.action_id).status == "rejected"
    assert world.queue.kick("u1") is None
    assert world.ledger.list("u1", status="proposed") == [] and world.runs.store.live_of("u1") == []


def test_the_one_waiting_batch_is_updated_when_the_pages_to_write_change(world):
    from evowiki.wikiapp.paths import next_path_page
    with world.pool.connection() as conn:
        store.put_settings(conn, "u1", ASK, NOW)
    world.queue.kick("u1")
    [first] = world.ledger.list("u1", status="proposed")
    assert first.summary == "要生成 3 页"
    next_path_page(world.repo, "u1", PATH_ID, now=NOW, new_id=world.ids)          # page 1 is started
    world.repo.commit(world.repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=world.ids("wev"), ts=NOW, body=PagesTrashed(page_ids=tuple(
            world.repo.load("u1").state.paths[PATH_ID].page_ids[:1])))]))           # and out of reach
    world.queue.kick("u1")
    [now] = world.ledger.list("u1", status="proposed")
    assert now.action_id == first.action_id and now.summary == "要生成 2 页"
    assert len(now.content["picks"]) == 2


def test_batches_are_found_beyond_the_ledgers_newest_hundred_rows(world):
    """Over 100 other ledger rows: still one pending batch, a rejected set not asked again, no key
    clash."""
    from evowiki.wikiapp.paths import next_path_page
    for i in range(105):
        world.ledger.record("u1", kind="assistant_edit", scope="page_body", source="assistant",
                            summary=f"改了 x{i}", content={"page_id": f"x{i}"},
                            key=f"edit:x{i}", undo={"page_id": f"x{i}"})
    with world.pool.connection() as conn:
        store.put_settings(conn, "u1", ASK, NOW)
    world.queue.kick("u1")
    [first] = world.ledger.list("u1", status="proposed")
    for _ in range(105):          # newer rows push the batch out of the newest 100
        world.ledger.record("u1", kind="assistant_edit", scope="page_body", source="assistant",
                            summary="x", content={"page_id": "y"}, key=f"edit:y{_}",
                            undo={"page_id": "y"})
    assert first.action_id not in [r.action_id for r in world.ledger.list("u1", limit=100)]
    world.queue.kick("u1")
    assert [r.action_id for r in world.ledger.list("u1", status="proposed", limit=500)] == \
        [first.action_id]                                          # still the one
    next_path_page(world.repo, "u1", PATH_ID, now=NOW, new_id=world.ids)
    world.repo.commit(world.repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=world.ids("wev"), ts=NOW, body=PagesTrashed(page_ids=tuple(
            world.repo.load("u1").state.paths[PATH_ID].page_ids[:1])))]))
    world.queue.kick("u1")                                         # the set changed: updated
    [row] = world.ledger.batches("u1")
    assert row.action_id == first.action_id and len(row.content["picks"]) == 2
    world.ledger.reject("u1", row.action_id)
    world.queue.kick("u1")
    world.queue.kick("u1")
    assert [r.status for r in world.ledger.batches("u1")] == ["rejected"]    # never asked again


class _Done:
    cost_usd, llm_calls = 0.02, 5


def _finished(w, pid, origin):
    """A page run that ended `done` and its after-write step (the per-page ledger row)."""
    from evowiki.wikiapp.page_runs import PageJob
    row, _ = w.runs.store.open_run(user_id="u1", page_id=pid, kind="organize", origin=origin,
                                   job={}, now=NOW, attach=False)
    w.runs.store.begin(row.run_id, NOW)
    w.runs.store.set_progress(row.run_id, {"outline_calls": 2, "section_calls": 3})
    w.runs.store.finish(row.run_id, "done", NOW, cost_usd=0.02, llm_calls=5)
    job = PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW, origin=origin)
    w.queue.after_write(job, row.run_id, "done", _Done())
    return row.run_id


def _page(w, title):
    snap = w.repo.load("u1")
    rec, evs = page_events_for(snap.state, snap.k, title=title, goal=title,
                               concepts=(), targets=(), now=NOW, new_id=w.ids, user_id="u1")
    w.repo.commit(snap, Commit(wiki=evs))
    return rec.page_id


def test_every_page_write_gets_its_own_row_and_ledger_record(world):
    from evowiki.bot.why import recent
    from evowiki.wikiapp.events import PageOpened
    p1, p2 = _page(world, "抽样分布"), _page(world, "协方差")
    r1, r2 = _finished(world, p1, "idle"), _finished(world, p2, "open")
    rows = {r.run_id: r for r in recent(world.pool, "u1", now=NOW, writes=world.queue)}
    idle, asked = rows[r1], rows[r2]
    assert (idle.kind, idle.result, idle.title, idle.cost_usd) == (
        "idle_write", "written", "创建了「抽样分布」页面", 0.02)
    assert (asked.kind, asked.write.origin) == ("user_write", "open")
    for r in (idle, asked):
        w = r.write
        assert (w.can_undo, w.undone, w.opened, w.calls, w.outline_calls, w.section_calls) == (
            True, False, False, 5, 2, 3)
    kinds = {a.run_id: a.kind for a in world.ledger.list("u1", status="executed", writes=True)}
    assert kinds == {r1: "bot_write", r2: "user_write"}
    world.repo.commit(world.repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=world.ids("wev"), ts=NOW, body=PageOpened(page_id=p1))]))
    assert {r.run_id: r for r in recent(world.pool, "u1", now=NOW, writes=world.queue)}[r1].write.opened


def test_undo_of_a_write_row_is_per_page_and_the_row_stays_struck_through(world):
    from evowiki.bot.why import recent
    p1, p2 = _page(world, "抽样分布"), _page(world, "协方差")
    r1, r2 = _finished(world, p1, "open"), _finished(world, p2, "open")
    row1 = {r.run_id: r for r in recent(world.pool, "u1", now=NOW, writes=world.queue)}[r1]
    world.ledger.undo("u1", row1.write.action_id)
    rows = {r.run_id: r for r in recent(world.pool, "u1", now=NOW, writes=world.queue)}
    assert (rows[r1].write.undone, rows[r1].write.can_undo) == (True, False)
    assert (rows[r2].write.undone, rows[r2].write.can_undo) == (False, True)
    state = world.repo.load("u1").state
    assert p1 in state.trashed_pages and p2 not in state.trashed_pages


def test_a_page_trashed_by_other_means_is_not_an_undone_write(world):
    from evowiki.bot.why import recent
    p1 = _page(world, "抽样分布")
    r1 = _finished(world, p1, "idle")
    world.repo.commit(world.repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=world.ids("wev"), ts=NOW, body=PagesTrashed(page_ids=(p1,)))]))
    [row] = recent(world.pool, "u1", now=NOW, writes=world.queue)
    assert row.run_id == r1 and not row.write.undone


def test_a_rewrite_is_its_own_row_with_its_own_undo(world):
    from evowiki.bot.why import recent
    p1 = _page(world, "抽样分布")
    r1 = _finished(world, p1, "idle")
    first = world.ledger.list("u1", status="executed", writes=True)[0]
    world.ledger.undo("u1", first.action_id)
    r2 = _finished(world, p1, "open")                  # 重新写
    rows = {r.run_id: r for r in recent(world.pool, "u1", now=NOW, writes=world.queue)}
    assert rows[r1].write.undone and not rows[r2].write.undone and rows[r2].write.can_undo
    assert rows[r1].write.action_id != rows[r2].write.action_id


def test_write_rows_are_not_cut_off_by_the_ledgers_window(world):
    from evowiki.bot.why import recent
    p1 = _page(world, "抽样分布")
    r1 = _finished(world, p1, "idle")
    for i in range(130):                               # newer rows push it out of any newest-N window
        world.ledger.record("u1", kind="user_write", scope="new_page", source="bot",
                            summary="x", content={"page_id": f"x{i}"}, key=f"user_write:z{i}",
                            undo={"page_id": f"x{i}"})
    [row] = recent(world.pool, "u1", now=NOW, writes=world.queue)
    assert row.run_id == r1 and row.write.can_undo


def test_recent_shows_a_page_still_writing_with_its_cost_so_far(world):
    from evowiki.bot.why import recent
    pid = _page(world, "抽样分布")
    row, _ = world.runs.store.open_run(user_id="u1", page_id=pid, kind="organize", origin="idle",
                                       job={}, now=NOW, attach=False)
    world.runs.store.begin(row.run_id, NOW)
    world.runs.store.set_progress(row.run_id, {"section": 1, "of": 4, "cost_usd": 0.013})
    [r] = recent(world.pool, "u1", now=NOW, writes=world.queue)
    assert (r.kind, r.result, r.title, r.cost_usd) == ("idle_write", "writing", "正在创建「抽样分布」页面", 0.013)
    [st] = world.queue.status("u1", [pid])
    assert (st["state"], st["section"], st["cost_usd"]) == ("writing", 1, 0.013)
    assert recent(world.pool, "u1", now=NOW) == []             # no write queue: triggers only


def test_per_page_write_records_stay_out_of_the_ledger_window_and_the_change_list(world):
    from evowiki.bot.change_source import LedgerChangeSource
    world.ledger.record("u1", kind="assistant_edit", scope="page_body", source="assistant",
                        summary="改了一页", content={"page_id": "e"}, key="edit:e",
                        undo={"page_id": "e"})
    for i in range(130):                                # a split's pages, and then some
        world.ledger.record("u1", kind="user_write", scope="new_page", source="bot",
                            summary=f"写好了「{i}」", content={"page_id": f"w{i}"},
                            key=f"user_write:{i}", undo={"page_id": f"w{i}"}, run_id=f"r{i}")
    world.ledger.record("u1", kind="bot_write", scope="new_page", source="bot", summary="idle",
                        content={"page_id": "i"}, key="bot_write:ri", undo={"page_id": "i"},
                        run_id="ri")
    window = world.ledger.list("u1", limit=100)
    assert [r.kind for r in window] == ["assistant_edit"]
    assert len(world.ledger.list("u1", limit=500, writes=True)) == 132
    assert [c.summary for c in LedgerChangeSource(world.ledger).list("u1")] == ["改了一页"]


def test_the_batch_approval_row_stays_in_the_window(world):
    with world.pool.connection() as conn:
        store.put_settings(conn, "u1", ASK, NOW)
    world.queue.kick("u1")
    assert [r.summary for r in world.ledger.list("u1", status="proposed")] == ["要生成 3 页"]


def test_only_the_first_run_of_a_page_is_attributed_to_the_assistant_action_that_made_it(world):
    from evowiki.bot.why import recent
    pid = _page(world, "抽样分布")
    world.ledger.record("u1", kind="page_split", scope="new_page", source="assistant",
                        summary="拆成 3 页", content={}, key="split1",
                        result={"made": [{"key": "q1", "page_id": pid, "n": 1, "folder": ""}]})
    r1 = _finished(world, pid, "open")
    r2 = _finished(world, pid, "open")                              # a later open / 重新写
    rows = {r.run_id: r.write.asked for r in recent(world.pool, "u1", now=NOW, writes=world.queue)}
    assert rows == {r1: "拆成 3 页", r2: None}


def test_the_write_row_of_a_merged_page_cannot_be_undone_alone(world):
    pid = _page(world, "合并页")
    world.ledger.record("u1", kind="page_merge", scope="delete_merge", source="assistant",
                        summary="合并两页", content={}, key="merge1",
                        result={"made": [{"key": None, "page_id": pid, "n": None, "folder": ""}]})
    run_id = _finished(world, pid, "open")
    [row] = [r for r in world.ledger.list("u1", writes=True) if r.run_id == run_id]
    with pytest.raises(NotDecidable, match="要撤销的是那次合并"):
        world.ledger.undo("u1", row.action_id)
    assert pid not in world.repo.load("u1").state.trashed_pages
