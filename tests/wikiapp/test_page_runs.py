"""Page runs (B1b Task 6): a page is written by a run the server owns. Inline runs (the caller's
deps, a virtual workflow named by the run id) and DBOS runs (workflow + stream + page_run row) carry
the same items; followers replay and attach; a follower that leaves does not cancel the run; the
body names its steps. Scripted LLMs only."""

import threading

import pytest

from evowiki.agent import durable
from evowiki.wikiapp import durable_repo, page_flows
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.organize import organize_stream
from evowiki.wikiapp.page_flows import PageDeps
from evowiki.wikiapp.page_runs import (
    FeedbackArgs,
    InlinePageRuns,
    PageJob,
    _registry,
    decode_item,
    encode_item,
    spawn_thread,
)
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.wiki_schemas import (
    ORGANIZE_PAYLOADS,
    REWRITE_PAYLOADS,
    ErrorEvent,
    OrganizeDoneEvent,
    RewriteDoneEvent,
)
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.fake_writer import WRITE, writing
from tests.wikiapp.flat_goal import flat_goal
from tests.wikiapp.goal_script import push_no_question
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_k1_replay import PAGE1, PAGE2, PAGE3
from tests.wikiapp.test_organize import BODY2, OUTLINE, page1, script_page, text
from tests.wikiapp.test_page import organized
from tests.wikiapp.test_paths import BIG_INTAKE, BIG_PATH, GOAL


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def names(items):
    return [n for n, _ in items]


def stream(env, pid, runs, kit=None, **kw):
    repo, s, ids = env
    return organize_stream(repo, CountingGateway(s.gateway()), "u1", pid, now=NOW, new_id=ids,
                           writing=kit or writing(env), runs=runs, **kw)


# ---- inline runs ----

def test_inline_first_write_has_the_items_calls_and_record_of_before(env):
    repo, s, _ = env
    pid = page1(env)
    script_page(s)
    runs = InlinePageRuns()
    items = list(stream(env, pid, runs))
    assert names(items) == ["planning", "outline", "writer", "section_delta",
                            "section_done", "section_delta", "section_done", "section_delta", "section_done", "done"]
    for name, payload in items:
        assert isinstance(payload, ORGANIZE_PAYLOADS[name])
    assert items[5][1].body == BODY2
    done = items[-1][1]
    assert isinstance(done, OrganizeDoneEvent)
    assert (done.page_id, done.llm_calls, done.prefetched) == (pid, 3, False)
    assert s.calls(ORGANIZE) == 1 and s.calls(WRITE) == 3     # the writer's outline, its writes
    rec = repo.load("u1").state.pages[pid]
    assert rec.page is not None and set(rec.page.texts) == {"s1", "s2", "s3"}
    assert repo.wiki_log("u1")[-2].body.action == "organized"
    [row] = runs.store._rows.values()
    assert (row.kind, row.status, row.consumed_at is not None) == ("organize", "done", True)
    assert row.run_id == f"page:u1:{pid}:1"


def test_inline_the_row_is_finished_when_the_end_item_is_yielded(env):
    _, s, _ = env
    pid = page1(env)
    script_page(s)
    runs = InlinePageRuns()
    seen = []
    for name, _p in stream(env, pid, runs):
        if name == "done":
            seen = [r.status for r in runs.store._rows.values()]
    assert seen == ["done"]


def test_a_failed_write_ends_with_an_error_and_a_failed_row(env):
    _, s, _ = env
    pid = page1(env)
    s.push(ORGANIZE, OUTLINE)
    s.push(WRITE, "UNAVAILABLE")
    runs = InlinePageRuns()
    items = list(stream(env, pid, runs))
    assert names(items)[-1] == "error" and items[-1][1].code == "llm_unavailable"
    [row] = runs.store._rows.values()
    assert row.status == "failed"


def test_a_finished_run_is_replayed_once_then_a_new_organize_starts_a_new_run(env):
    repo, s, _ = env
    pid = page1(env)
    runs = InlinePageRuns()
    script_page(s)
    first = list(stream(env, pid, runs))
    assert names(first)[-1] == "done"
    script_page(s)
    again = list(stream(env, pid, runs))                 # read to the end: an explicit reorganize
    assert names(again)[-1] == "done" and s.pending() == {}
    assert repo.wiki_log("u1")[-2].body.action == "reorganized"
    assert sorted(r.n for r in runs.store._rows.values()) == [1, 2]


def test_an_unread_finished_run_is_replayed_not_rewritten(env):
    _, s, _ = env
    pid = page1(env)
    runs = InlinePageRuns()
    script_page(s)
    a = stream(env, pid, runs)                           # inline: finished, nobody read it yet
    b = stream(env, pid, runs)
    assert names(list(b)) == names(list(a)) and s.pending() == {}


def test_fresh_a_finished_run_nobody_read_is_not_replayed_the_page_is_written_anew(env):
    """重新组织 (`fresh`) on a page whose write finished unread (watched through GET /wiki/progress after a
    reload): the page is written again, not the old run replayed (before: the first 重新组织 did nothing)."""
    repo, s, _ = env
    pid = page1(env)
    runs = InlinePageRuns()
    script_page(s)
    a = stream(env, pid, runs)                           # finished, nobody read it
    list(a)
    [old] = runs.store._rows.values()
    runs.store._update(old.run_id, consumed_at=None)     # as if its follower never read the done
    script_page(s)
    again = list(stream(env, pid, runs, fresh=True))
    assert names(again)[-1] == "done" and s.pending() == {}
    assert repo.wiki_log("u1")[-2].body.action == "reorganized"
    assert sorted(r.n for r in runs.store._rows.values()) == [1, 2]


def test_a_second_follower_attaches_to_the_running_write(env):
    """Two clients on one page: the second replays what was written so far and follows the rest;
    the page is written once."""
    _, s, _ = env
    pid = page1(env)
    runs = InlinePageRuns(spawn=spawn_thread)
    script_page(s)
    kit = writing(env)
    gate = threading.Event()
    kit.writer.before_write = gate.wait                  # hold the writer until both follow
    a = stream(env, pid, runs, kit)
    b = stream(env, pid, runs, kit)
    first_a = next(a)
    gate.set()
    rest_a, all_b = [first_a, *a], list(b)
    assert names(rest_a) == names(all_b) and names(all_b)[-1] == "done"
    assert len(kit.writer.inputs) == 1 and s.pending() == {}
    assert len(runs.store._rows) == 1


def test_a_deferred_run_is_followed_by_both_streams(env):
    """`spawn` deferred: the second stream attaches to the same (not yet running) run."""
    _, s, _ = env
    pid = page1(env)
    jobs: list = []
    runs = InlinePageRuns(spawn=jobs.append)
    script_page(s)
    kit = writing(env)
    a, b = stream(env, pid, runs, kit), stream(env, pid, runs, kit)
    assert len(jobs) == 1
    jobs.pop()()
    assert names(list(a)) == names(list(b)) and len(kit.writer.inputs) == 1


def test_a_follower_that_leaves_does_not_cancel_the_run(env):
    repo, s, _ = env
    pid = page1(env)
    jobs: list = []
    runs = InlinePageRuns(spawn=jobs.append)
    script_page(s)
    it = stream(env, pid, runs)
    jobs.pop()()
    assert next(it)[0] == "planning"
    it.close()                                           # the client went away
    assert repo.load("u1").state.pages[pid].page is not None      # the page was written anyway
    [row] = runs.store._rows.values()
    assert row.status == "done" and row.consumed_at is None


def test_an_unknown_page_raises_before_any_run(env):
    runs = InlinePageRuns()
    with pytest.raises(LookupError):
        stream(env, "nope", runs)
    assert runs.store._rows == {}


# ---- item codec ----

def test_every_event_class_round_trips(env):
    _, s, _ = env
    pid = page1(env)
    script_page(s)
    items = list(stream(env, pid, InlinePageRuns()))
    items += [("error", ErrorEvent(code="conflict", retryable=True, detail="d")),
              ("done", RewriteDoneEvent(page_id=pid, llm_calls=2, cost_usd=0.5, model="m"))]
    for name, payload in items:
        assert decode_item(encode_item(name, payload)) == (name, payload)
    wire = encode_item("error", items[-2][1])
    assert wire == {"event": "error", "type": "ErrorEvent", "data": items[-2][1].model_dump(
        mode="json")}
    # the registry holds every organize / rewrite payload class (SectionAdded, Change: built by
    # the flows that emit them)
    for cls in {*ORGANIZE_PAYLOADS.values(), *REWRITE_PAYLOADS.values()}:
        assert cls.__name__ in _registry()


def test_an_unknown_item_type_is_refused():
    with pytest.raises(ValueError, match="unknown"):
        decode_item({"event": "x", "type": "os.system", "data": {}})
    with pytest.raises(ValueError):
        decode_item({"event": "done", "data": {}})


# ---- the body names its steps ----

def test_execute_in_a_virtual_workflow_names_its_steps(env, monkeypatch):
    repo, s, ids = env
    pid = page1(env)
    script_page(s)
    seen: list[str] = []
    real = durable.step_ran

    def spy(name, fn):
        seen.append(name)
        return real(name, fn)
    monkeypatch.setattr(durable, "step_ran", spy)
    monkeypatch.setattr(durable_repo, "step_ran", spy)
    deps = PageDeps(repo=repo, gateway=lambda _u: CountingGateway(s.gateway()),
                    writing=writing(env), store=InlinePageRuns().store, clock=lambda: NOW,
                    new_id=ids)
    row, _ = deps.store.open_run(user_id="u1", page_id=pid, kind="organize", origin="open",
                                 job={}, now=NOW, attach=True)
    job = PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW)
    out: list = []
    with durable.virtual_workflow(row.run_id):
        status = page_flows.execute(job, row.run_id, lambda n, p: out.append(n), deps)
    assert status == "done" and out[-1] == "done"
    assert seen[:2] == ["clock", "run.begin"] and "repo.load" in seen
    assert not any(n.startswith("llm.") for n in seen)    # no model call before the writer
    # the outline, each of the three sections as it is written (T3), then the final commit
    assert seen.count("repo.commit") == 5
    assert seen[-1] == "run.finish"   # (no after-write hook here: no run.after step)
    assert deps.store.get(row.run_id).status == "done"


def _feedback_steps(env, monkeypatch, push):
    """Runs a 「再讲深一点」 on s3 of an organized page as a feedback job in a virtual workflow;
    (items, step names, status)."""
    repo, s, ids = env
    pid = organized(env)
    push(s)
    seen: list[str] = []
    real = durable.step_ran

    def spy(name, fn):
        seen.append(name)
        return real(name, fn)
    monkeypatch.setattr(durable, "step_ran", spy)
    monkeypatch.setattr(durable_repo, "step_ran", spy)
    deps = PageDeps(repo=repo, gateway=lambda _u: CountingGateway(s.gateway()),
                    writing=writing(env), store=InlinePageRuns().store, clock=lambda: NOW,
                    new_id=ids)
    job = PageJob(kind="feedback", user_id="u1", page_id=pid, now=NOW, origin="open",
                  feedback=FeedbackArgs(section_id="s3", kind="deeper"))
    # `organized()` ran as n=1 of this page in its own store, and effect keys are
    # `<run_id>:<step>` on the shared repo: burn n=1 here so the feedback run is n=2
    deps.store.open_run(user_id="u1", page_id=pid, kind="organize", origin="open", job={},
                        now=NOW, attach=False)
    row, _ = deps.store.open_run(user_id="u1", page_id=pid, kind="feedback", origin="open",
                                 job={}, now=NOW, attach=False)
    assert row.n == 2
    out: list = []
    with durable.virtual_workflow(row.run_id):
        status = page_flows.execute(job, row.run_id, lambda n, p: out.append((n, p)), deps)
    return out, seen, status


def test_a_feedback_rewrite_in_a_workflow_has_one_commit_step(env, monkeypatch):
    out, seen, status = _feedback_steps(
        env, monkeypatch, lambda s: s.push(WRITE, text("s3", "标准差讲深一点")))
    assert status == "done" and out[-1][0] == "done"
    assert "repo.load" in seen and seen.count("repo.commit") == 1
    assert seen[-1] == "run.finish"   # (no after-write hook here: no run.after step)
    saved = [e.body for e in env[0].wiki_log("u1") if e.body.type == "page_saved"]
    assert saved[-1].action == "feedback"                  # the rewrite landed
    assert "标准差讲深一点" in saved[-1].record.page.texts["s3"].body


def test_a_failed_feedback_rewrite_commits_only_the_signal_once(env, monkeypatch):
    out, seen, status = _feedback_steps(env, monkeypatch, lambda s: s.push(WRITE, "not json"))
    assert status == "failed" and out[-1][0] == "error"
    assert seen.count("repo.commit") == 1
    assert [b.body.type for b in env[0].wiki_log("u1")[-1:]] == ["signal_recorded"]


def test_execute_without_a_gateway_says_llm_unavailable(env):
    repo, _, ids = env
    pid = page1(env)
    deps = PageDeps(repo=repo, gateway=lambda _u: None, writing=writing(env),
                    store=InlinePageRuns().store, clock=lambda: NOW, new_id=ids)
    row, _ = deps.store.open_run(user_id="u1", page_id=pid, kind="organize", origin="open",
                                 job={}, now=NOW, attach=True)
    out: list = []
    with durable.virtual_workflow(row.run_id):
        status = page_flows.execute(
            PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW), row.run_id,
            lambda n, p: out.append((n, p)), deps)
    assert status == "failed" and out[-1][1].code == "llm_unavailable"


def test_a_refine_run_without_its_job_fails_closed(env):
    repo, _, ids = env
    pid = page1(env)
    deps = PageDeps(repo=repo, gateway=lambda _u: None, writing=None,
                    store=InlinePageRuns().store, clock=lambda: NOW, new_id=ids)
    row, _ = deps.store.open_run(user_id="u1", page_id=pid, kind="refine", origin="open",
                                 job={}, now=NOW, attach=False)
    out: list = []
    with durable.virtual_workflow(row.run_id):
        status = page_flows.execute(
            PageJob(kind="refine", user_id="u1", page_id=pid, now=NOW), row.run_id,
            lambda n, p: out.append((n, p)), deps)
    assert status == "failed" and out[-1][1].code == "internal"
    assert "carries its job" in out[-1][1].detail


def test_installed_fails_closed_without_install(monkeypatch):
    monkeypatch.setattr(page_flows, "_deps", None)
    with pytest.raises(RuntimeError, match="install"):
        page_flows.installed()


# ---- DBOS (Postgres) ----

@pytest.fixture
def dbos_env(db_url, pool):
    from evowiki.agent import dbos_app
    from evowiki.wikiapp.page_run_store import PgPageRunStore
    from evowiki.wikiapp.repo import PgRepo
    from tests.runtime.helpers import make_user

    make_user(pool, "u1")
    repo, s, ids = PgRepo(pool), Script(), Ids()
    store = PgPageRunStore(pool)
    kit = writing((repo, s))
    page_flows.install(PageDeps(
        repo=repo, gateway=lambda _u: CountingGateway(s.gateway()), writing=kit, store=store,
        clock=lambda: NOW, new_id=ids))
    dbos_app.launch(db_url, app_version="t")
    yield repo, s, ids, kit, store
    dbos_app.shutdown()
    page_flows._deps = None


def test_dbos_run_yields_the_inline_items_commits_once_and_finishes_the_row(dbos_env):
    from dbos import DBOS

    from evowiki.wikiapp.page_runs import DbosPageRuns

    repo, s, ids, _kit, store = dbos_env
    pid = page1((repo, s, ids))
    script_page(s)
    runs = DbosPageRuns(store)
    job = PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW)
    run_id, started = runs.start(job, page_flows.installed(), attach=True)
    assert started and run_id == f"page:u1:{pid}:1"
    items = list(runs.follow(run_id))
    assert names(items) == ["planning", "outline", "writer", "section_delta",
                            "section_done", "section_delta", "section_done", "section_delta", "section_done", "done"]
    assert items[-1][1].llm_calls == 3
    assert DBOS.retrieve_workflow(run_id).get_result() == "done"
    row = store.get(run_id)
    assert row.status == "done" and row.consumed_at is not None and row.finished_at is not None
    steps = [st["function_name"] for st in DBOS.list_workflow_steps(run_id)]
    assert steps.count("repo.commit") == 5        # outline, s1–s3 as written, the final one
    assert steps[:2] == ["clock", "run.begin"]
    assert "run.finish" in steps and "run.after" not in steps     # (no hook in these deps)
    assert repo.load("u1").state.pages[pid].page is not None
    # the stream is replayable: a second follower reads the same items from the start
    assert names(list(runs.follow(run_id))) == names(items)


def test_dbos_the_row_is_finished_when_the_end_item_is_yielded(dbos_env):
    from evowiki.wikiapp.page_runs import DbosPageRuns

    repo, s, ids, _kit, store = dbos_env
    pid = page1((repo, s, ids))
    script_page(s)
    runs = DbosPageRuns(store)
    job = PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW)
    run_id, _ = runs.start(job, page_flows.installed(), attach=True)
    seen = None
    for name, _p in runs.follow(run_id):
        if name == "done":
            seen = store.get(run_id).status
    assert seen == "done"


def test_dbos_a_second_start_attaches_to_the_running_run(dbos_env):
    from evowiki.wikiapp.page_runs import DbosPageRuns

    repo, s, ids, kit, store = dbos_env
    pid = page1((repo, s, ids))
    script_page(s)
    gate = threading.Event()
    kit.writer.before_write = gate.wait
    runs = DbosPageRuns(store)
    job = PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW)
    a, new_a = runs.start(job, page_flows.installed(), attach=True)
    b, new_b = runs.start(job, page_flows.installed(), attach=True)    # re-ensures, starts nothing
    assert (a, new_a, new_b) == (b, True, False)
    gate.set()
    assert names(list(runs.follow(a))) == names(list(runs.follow(b)))
    assert len(kit.writer.inputs) == 1 and s.pending() == {}


def test_dbos_a_failed_write_ends_with_error_and_a_failed_row(dbos_env):
    from dbos import DBOS

    from evowiki.wikiapp.page_runs import DbosPageRuns

    repo, s, ids, _kit, store = dbos_env
    pid = page1((repo, s, ids))
    s.push(ORGANIZE, OUTLINE)
    s.push(WRITE, "UNAVAILABLE")
    runs = DbosPageRuns(store)
    run_id, _ = runs.start(PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW), page_flows.installed(), attach=True)
    items = list(runs.follow(run_id))
    assert items[-1][0] == "error" and items[-1][1].code == "llm_unavailable"
    assert DBOS.retrieve_workflow(run_id).get_result() == "failed"
    assert store.get(run_id).status == "failed"


# ---- write-all: runs go through the user's queue, the idle Bot commits what it writes ----

def script_outline(s, outline):
    s.push(ORGANIZE, outline)
    for sec in outline["sections"]:
        s.push(WRITE, text(sec["id"]))


def path_pages(env, n=3):
    """A three-page path; the first `n` pages started (page records, not written yet)."""
    repo, s, ids = env
    s.push("goals.intake", BIG_INTAKE)
    push_no_question(s)
    s.push("goals.path", BIG_PATH)
    res = flat_goal(repo, CountingGateway(s.gateway()), "u1", GOAL, "", now=NOW, new_id=ids)
    return res.path_id, [next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id
                         for _ in range(n)]


def path_with_first_page(env):
    """A three-page path; page 1 started (not organized yet)."""
    path_id, [p1] = path_pages(env, 1)
    return path_id, p1


def test_discarding_an_inline_run_marks_the_row(env):
    runs = InlinePageRuns()
    row, _ = runs.store.open_run(user_id="u1", page_id="p9", kind="organize", origin="idle",
                                 job={}, now=NOW, attach=True)
    runs.discard(row.run_id, NOW)
    assert runs.store.get(row.run_id).discarded_at == NOW
    assert runs.store.live_of("u1") == []


def test_an_idle_run_commits_its_page_and_keeps_its_cost_for_the_status(env):
    repo, s, ids = env
    _, [p1] = path_pages(env, 1)
    script_outline(s, PAGE1)
    runs = InlinePageRuns()
    deps = PageDeps(repo=repo, gateway=lambda _u: CountingGateway(s.gateway()), writing=writing(env),
                    store=runs.store, clock=lambda: NOW, new_id=ids)
    run_id, _ = runs.start(PageJob(kind="organize", user_id="u1", page_id=p1, now=NOW,
                                   origin="idle"), deps, attach=True)
    row = runs.store.get(run_id)
    assert row.origin == "idle" and row.status == "done" and row.began_at == NOW
    assert (row.llm_calls, row.cost_usd, row.progress) == (3, 0.0, {
        "section": 3, "of": 3, "cost_usd": 0.0, "outline_calls": 0, "section_calls": 3})
    assert repo.load("u1").state.pages[p1].page is not None       # committed, nothing held
    # a page written ahead stays replayable to its first reader for a day, then it is old news
    assert runs.store.live_of("u1") == []
    items = list(runs.follow(run_id))
    assert items[-1][0] == "done" and items[-1][1].prefetched


def test_dbos_runs_of_one_user_go_one_at_a_time_and_the_users_own_goes_first(dbos_env):
    import time

    from evowiki.wikiapp.page_runs import DbosPageRuns
    repo, s, ids, kit, store = dbos_env
    runs = DbosPageRuns(store)
    _, [p1, p2, p3] = path_pages((repo, s, ids))
    script_outline(s, PAGE1)
    script_outline(s, PAGE3)
    script_outline(s, PAGE2)
    gate = threading.Event()
    kit.writer.before_write = gate.wait             # the first run holds the queue until released

    def job(pid, origin):
        return PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW, origin=origin)
    deps = page_flows.installed()
    # a: a write the user waits for (an idle one would be preempted by c, see below)
    a, _ = runs.start(job(p1, "open"), deps, attach=True)
    for _ in range(300):                             # the queue polls once a second
        if store.get(a).began_at is not None:
            break
        time.sleep(0.05)
    assert store.get(a).began_at is not None
    b, _ = runs.start(job(p2, "idle"), deps, attach=True)       # queued behind a ...
    c, _ = runs.start(job(p3, "open"), deps, attach=True)       # ... and the user's own goes first
    assert store.get(b).began_at is None and store.get(c).began_at is None
    gate.set()
    for rid in (a, b, c):
        assert list(runs.follow(rid))[-1][0] == "done"
    ra, rb, rc = (store.get(r) for r in (a, b, c))
    assert ra.finished_at <= rc.began_at and rc.finished_at <= rb.began_at
    steps = [st["function_name"] for st in __import__("dbos").DBOS.list_workflow_steps(a)]
    assert steps[:2] == ["clock", "run.begin"]
    assert "run.finish" in steps and "run.after" not in steps     # (no hook in these deps)


def test_dbos_the_users_write_preempts_a_running_bot_write_which_runs_again_after(dbos_env):
    """The Bot writes p1 ahead (idle, begun, held mid-write); the user opens p2: the Bot's run is
    stopped (failed `preempted`, not the learner's 「停止」) and the user's starts at once, without
    waiting for it. Afterwards the Bot writes p1 again (a new run). A write the user waits for is
    never preempted (the test above)."""
    import time

    from evowiki.wikiapp.page_runs import DbosPageRuns
    from evowiki.wikiapp.write_queue import NOT_FAILURES, WriteQueue
    repo, s, ids, kit, store = dbos_env
    runs = DbosPageRuns(store)
    _, [p1, p2, _p3] = path_pages((repo, s, ids))
    script_outline(s, PAGE2)          # the user's write runs first ...
    s.push(ORGANIZE, PAGE1)           # (the stopped run, released, still takes its plan_page: the
                                      # fake writer reads the script outside any DBOS step)
    script_outline(s, PAGE1)          # ... then the Bot's p1 again
    gate, held = threading.Event(), threading.Event()
    calls = [0]

    def hold_first():
        calls[0] += 1
        if calls[0] == 1:             # the Bot's first p1 run: held before its first model call
            held.set()
            gate.wait(60)
    kit.writer.before_write = hold_first

    def job(pid, origin):
        return PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW, origin=origin)
    deps = page_flows.installed()
    bot, _ = runs.start(job(p1, "idle"), deps, attach=True)
    assert held.wait(30), "the Bot's write never began"
    user, _ = runs.start(job(p2, "open"), deps, attach=True)
    try:
        assert list(runs.follow(user))[-1][0] == "done"     # while the Bot's run is still held
        rb, ru = store.get(bot), store.get(user)
        assert rb.status == "failed" and rb.failure == "preempted" and rb.discarded_at is not None
        assert ru.status == "done" and ru.began_at is not None
        assert "preempted" in NOT_FAILURES            # not a failure of the Bot's idle writing
        wq = WriteQueue.__new__(WriteQueue)
        wq._runs = runs
        assert p1 not in wq._stopped("u1", repo.load("u1").state)   # the Bot writes it again
    finally:
        gate.set()
    for _ in range(200):              # the stopped run takes its plan_page, then DBOS stops it
        if s.pending().get(ORGANIZE, 0) == 1:
            break
        time.sleep(0.05)
    again, started = runs.start(job(p1, "idle"), deps, attach=True)
    assert started and again != bot
    assert list(runs.follow(again))[-1][0] == "done"
    assert store.get(again).began_at >= ru.finished_at
    for _ in range(100):              # the stopped workflow ends (DBOS refuses its next step)
        if __import__("dbos").DBOS.get_workflow_status(bot).status == "CANCELLED":
            break
        time.sleep(0.05)
    assert __import__("dbos").DBOS.get_workflow_status(bot).status == "CANCELLED"


def test_dbos_runs_queued_across_a_restart_are_dispatched_in_order(db_url, pool):
    """Two runs of one user sit enqueued in the system database while no process listens (a stop
    between the enqueue and the dispatch); the next launch registers the queue after launch (as the
    service's `after_launch` hook) and both run, the first one first."""
    from dbos import DBOSClient

    from evowiki.agent import dbos_app
    from evowiki.wikiapp import page_runs
    from evowiki.wikiapp.page_run_store import PgPageRunStore
    from evowiki.wikiapp.page_runs import DbosPageRuns, _row_args
    from evowiki.wikiapp.repo import PgRepo
    from tests.runtime.helpers import make_user

    make_user(pool, "u1")
    repo, s, ids = PgRepo(pool), Script(), Ids()
    store = PgPageRunStore(pool)
    _, [p1, p2] = path_pages((repo, s, ids), 2)
    script_outline(s, PAGE1)
    script_outline(s, PAGE2)
    page_flows.install(PageDeps(
        repo=repo, gateway=lambda _u: CountingGateway(s.gateway()), writing=writing((repo, s)),
        store=store, clock=lambda: NOW, new_id=ids))
    dbos_app.launch(db_url, app_version="t")            # first life: only creates the schema
    dbos_app.shutdown()
    client = DBOSClient(system_database_url=dbos_app.dbos_config(db_url, app_version="t")[
        "system_database_url"], serializer=dbos_app.dbos_config(db_url, app_version="t")[
        "serializer"])
    ids_enqueued = []
    for pid in (p1, p2):
        job = PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW)
        row, created = store.open_run(attach=True, **_row_args(job))
        assert created
        client.enqueue({"queue_name": "pages", "workflow_name": "evowiki.page_run",
                        "workflow_id": row.run_id, "queue_partition_key": "u1",
                        "priority": page_runs.USER_PRIORITY, "app_version": "t"}, row.job)
        ids_enqueued.append(row.run_id)
    client.destroy()
    try:
        dbos_app.launch(db_url, app_version="t")        # the restart
        page_runs.register_queue()                      # what the after_launch hook does
        runs = DbosPageRuns(store)
        for rid in ids_enqueued:
            assert list(runs.follow(rid))[-1][0] == "done"
        a, b = (store.get(r) for r in ids_enqueued)
        assert a.finished_at <= b.began_at and a.status == b.status == "done"
        assert repo.load("u1").state.pages[p1].page is not None
        assert repo.load("u1").state.pages[p2].page is not None
    finally:
        dbos_app.shutdown()
        page_flows._deps = None


def test_dbos_discarding_a_queued_run_cancels_its_workflow(dbos_env):
    import time

    from dbos import DBOS

    from evowiki.wikiapp.page_runs import DbosPageRuns
    repo, s, ids, kit, store = dbos_env
    runs = DbosPageRuns(store)
    _, [p1, p2] = path_pages((repo, s, ids), 2)
    script_outline(s, PAGE1)
    gate = threading.Event()
    kit.writer.before_write = gate.wait
    deps = page_flows.installed()

    def job(pid, origin):
        return PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW, origin=origin)
    a, _ = runs.start(job(p1, "open"), deps, attach=True)
    for _ in range(300):
        if store.get(a).began_at is not None:
            break
        time.sleep(0.05)
    b, _ = runs.start(job(p2, "idle"), deps, attach=True)       # queued behind a
    runs.discard(b, NOW)
    gate.set()
    assert list(runs.follow(a))[-1][0] == "done"
    assert DBOS.retrieve_workflow(b).get_status().status == "CANCELLED"
    row = store.get(b)
    assert row.discarded_at is not None and row.status == "failed" and row.failure == "cancelled"
    assert s.pending() == {}


def test_dbos_discarding_a_running_run_stops_it_before_its_commit(dbos_env, monkeypatch):
    """「停止」 while the writer is outlining (its plan_page): nothing is committed, the row says
    cancelled. (The scripted writer makes no gateway call; the real one's model calls are each a
    step, so a cancelled workflow stops at the next one.)"""
    from dbos import DBOS

    from evowiki.wikiapp.page_runs import DbosPageRuns
    repo, s, ids, _kit, store = dbos_env
    pid = page1((repo, s, ids))
    script_page(s)
    inside, gate, ended = threading.Event(), threading.Event(), threading.Event()
    answer = s.take

    def held(purpose, prompt="take"):
        if purpose == ORGANIZE and not inside.is_set():
            inside.set()
            gate.wait(10)
        return answer(purpose, prompt)
    monkeypatch.setattr(s, "take", held)
    execute = page_flows.execute

    def watched(*a, **k):
        try:
            return execute(*a, **k)
        finally:
            ended.set()
    monkeypatch.setattr(page_flows, "execute", watched)
    runs = DbosPageRuns(store)
    calls = s.calls()
    run_id, _ = runs.start(PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW), page_flows.installed(), attach=True)
    assert inside.wait(30)
    assert runs.discard(run_id, NOW)
    gate.set()
    assert ended.wait(30)
    assert s.calls() >= calls + 1                           # the outline that was being taken
    assert DBOS.retrieve_workflow(run_id).get_status().status == "CANCELLED"
    row = store.get(run_id)
    assert (row.status, row.failure, row.discarded_at) == ("failed", "cancelled", NOW)
    assert repo.load("u1").state.pages[pid].page is None    # back to unwritten


def test_a_feedback_page_that_changed_since_the_check_is_a_retryable_conflict(env):
    """`_feedback_target` raising (section gone / page changed after the eager check) is a
    conflict the client may retry, not an llm failure."""
    repo, s, ids = env
    pid = organized(env)
    deps = PageDeps(repo=repo, gateway=lambda _u: CountingGateway(s.gateway()),
                    writing=writing(env), store=InlinePageRuns().store, clock=lambda: NOW,
                    new_id=ids)
    row, _ = deps.store.open_run(user_id="u1", page_id=pid, kind="feedback", origin="open",
                                 job={}, now=NOW, attach=False)
    out: list = []
    with durable.virtual_workflow(row.run_id):
        status = page_flows.execute(
            PageJob(kind="feedback", user_id="u1", page_id=pid, now=NOW,
                    feedback=FeedbackArgs(section_id="no-such-section", kind="deeper")),
            row.run_id, lambda n, p: out.append((n, p)), deps)
    assert status == "failed" and out[-1][0] == "error"
    assert (out[-1][1].code, out[-1][1].retryable) == ("conflict", True)


# ---- refine / rsi_rewrite kinds ----

class _Deep:
    """DeepService stand-in: `run` ends with `stage` (or raises)."""

    def __init__(self, stage="done", failure=None, raises=False):
        self.stage, self.failure, self.raises, self.ran = stage, failure, raises, []

    def durable_copy(self):
        return self

    def run(self, uid, job, scope=None):
        from types import SimpleNamespace
        self.ran.append((uid, job.job_id, scope))
        if self.raises:
            raise RuntimeError("refine broke")
        return SimpleNamespace(job=job.model_copy(update={"stage": self.stage,
                                                          "failure": self.failure}))


def _refine_run(env, deep, kind, spec_version=None):
    from evowiki.deep.contracts import RefineJob
    from evowiki.wikiapp.page_runs import RefineArgs
    repo, _, ids = env
    pid = organized(env)
    deps = PageDeps(repo=repo, gateway=lambda _u: None, writing=None,
                    store=InlinePageRuns().store, clock=lambda: NOW, new_id=ids, deep=deep)
    rj = RefineJob(job_id="rf1", page_id=pid, trigger="rsi", stage="queued", started_at=NOW)
    row, _ = deps.store.open_run(user_id="u1", page_id=pid, kind=kind, origin="background",
                                 job={}, now=NOW, attach=False)
    out: list = []
    with durable.virtual_workflow(row.run_id):
        status = page_flows.execute(
            PageJob(kind=kind, user_id="u1", page_id=pid, now=NOW, origin="background",
                    refine=RefineArgs(job=rj.model_dump(mode="json"), spec_version=spec_version)),
            row.run_id, lambda n, p: out.append((n, p)), deps)
    return status, out, deps.store.get(row.run_id), pid


def test_a_refine_run_ends_with_done_and_its_row_done(env):
    deep = _Deep()
    status, out, row, _ = _refine_run(env, deep, "refine")
    assert status == "done" and [n for n, _ in out] == ["done"] and row.status == "done"
    assert deep.ran and deep.ran[0][:2] == ("u1", "rf1")


def test_a_failed_refine_ends_with_an_error_and_records_nothing(env):
    status, out, row, _ = _refine_run(env, _Deep("failed", "review_failed"), "refine")
    assert status == "failed" and out[-1][0] == "error" and row.status == "failed"
    assert out[-1][1].code == "llm_failed"
    assert not [e for e in env[0].wiki_log("u1") if e.body.type == "page_rewrite_failed"]


@pytest.mark.parametrize("deep", [_Deep("failed", "review_failed"), _Deep(raises=True)])
def test_a_failed_rsi_rewrite_is_recorded_on_the_page_once(env, deep):
    status, out, _, pid = _refine_run(env, deep, "rsi_rewrite", spec_version=4)
    assert status == "failed" and out[-1][0] == "error"
    [rec] = [e.body for e in env[0].wiki_log("u1") if e.body.type == "page_rewrite_failed"]
    assert (rec.page_id, rec.spec_version) == (pid, 4)


def test_a_good_rsi_rewrite_records_no_failure(env):
    status, _, _, _ = _refine_run(env, _Deep(), "rsi_rewrite", spec_version=4)
    assert status == "done"
    assert not [e for e in env[0].wiki_log("u1") if e.body.type == "page_rewrite_failed"]


def test_inline_logs_of_consumed_runs_are_dropped_at_the_next_start(env):
    pid = page1(env)
    script_page(env[1])
    runs = InlinePageRuns()
    list(stream(env, pid, runs))
    assert list(runs._logs) == [f"page:u1:{pid}:1"]
    script_page(env[1])
    list(stream(env, pid, runs))          # a reorganize: run 2; run 1 was consumed
    assert list(runs._logs) == [f"page:u1:{pid}:2"]


def _closed_log_for(runs, kind, finish=None, discard=False):
    from evowiki.wikiapp.page_runs import _Log
    row, _ = runs.store.open_run(user_id="u1", page_id=f"p-{kind}-{finish}-{discard}", kind=kind,
                                 origin="open", job={}, now=NOW, attach=False)
    lg = runs._logs[row.run_id] = _Log()
    lg.close()
    if finish:
        runs.store.finish(row.run_id, finish, NOW)
    if discard:
        runs.store.discard(row.run_id, NOW)
    return row.run_id


def test_inline_prune_drops_failed_discarded_and_never_attachable_logs(env):
    runs = InlinePageRuns()
    failed = _closed_log_for(runs, "organize", finish="failed")
    discarded = _closed_log_for(runs, "organize", discard=True)
    refine = _closed_log_for(runs, "refine", finish="done")
    live = _closed_log_for(runs, "organize")          # still `writing`: kept
    runs._prune()
    assert set(runs._logs) == {live}
    assert {failed, discarded, refine}.isdisjoint(runs._logs)


# ---- stuck `writing` rows (final review finding 1) ----

def _dbos_row(store, pid, *, now=NOW, attach=True):
    """A `writing` row (run 1) for the page, as `DbosPageRuns.start` would have inserted it."""
    from evowiki.wikiapp.page_runs import _row_args
    job = PageJob(kind="organize", user_id="u1", page_id=pid, now=now)
    row, created = store.open_run(**{**_row_args(job), "attach": attach})
    assert created
    return job, row


def _ended_in_error(run_id):
    """The row's workflow exists and ended ERROR (a bad job fails before `run.finish`)."""
    from dbos import DBOS, SetWorkflowID
    with SetWorkflowID(run_id):
        h = DBOS.start_workflow(page_flows.page_workflow, {"not": "a job"})
    with pytest.raises(Exception, match="PageJob|validation"):
        h.get_result()
    assert DBOS.retrieve_workflow(run_id).get_status().status == "ERROR"


def _second_run_is_written(runs, store, repo, s, pid, job, first):
    script_page(s)
    run_id, started = runs.start(job, page_flows.installed(), attach=True)
    assert started and run_id == f"page:u1:{pid}:2"
    assert names(list(runs.follow(run_id)))[-1] == "done"
    assert store.get(first.run_id).status == "failed"
    assert store.get(run_id).status == "done"
    assert repo.load("u1").state.pages[pid].page is not None


def test_dbos_a_row_whose_workflow_ended_in_error_starts_the_next_run(dbos_env):
    from evowiki.wikiapp.page_runs import DbosPageRuns
    repo, s, ids, _kit, store = dbos_env
    pid = page1((repo, s, ids))
    job, first = _dbos_row(store, pid)
    _ended_in_error(first.run_id)
    _second_run_is_written(DbosPageRuns(store), store, repo, s, pid, job, first)


def test_dbos_a_row_whose_workflow_belongs_to_an_old_app_version_starts_the_next_run(
        dbos_env, db_url):
    import psycopg

    from evowiki.wikiapp.page_runs import DbosPageRuns
    repo, s, ids, _kit, store = dbos_env
    pid = page1((repo, s, ids))
    job, first = _dbos_row(store, pid)
    _ended_in_error(first.run_id)
    with psycopg.connect(db_url, autocommit=True) as c:      # as if the old version left it pending
        c.execute("update dbos.workflow_status set status = 'PENDING', "
                  "application_version = 'old' where workflow_uuid = %s", (first.run_id,))
    _second_run_is_written(DbosPageRuns(store), store, repo, s, pid, job, first)


def test_dbos_a_young_row_without_a_workflow_is_attached_and_its_workflow_started(dbos_env):
    from datetime import UTC, datetime

    from evowiki.wikiapp.page_runs import DbosPageRuns
    repo, s, ids, _kit, store = dbos_env
    pid = page1((repo, s, ids))
    script_page(s)
    # just inserted by a concurrent request: attach and (re)start its workflow, never finish it
    job, young = _dbos_row(store, pid, now=datetime.now(UTC))
    run_id, started = DbosPageRuns(store).start(job, page_flows.installed(), attach=True)
    assert (run_id, started) == (young.run_id, False)
    assert names(list(DbosPageRuns(store).follow(run_id)))[-1] == "done"


def test_dbos_an_old_row_without_a_workflow_is_failed_and_the_next_run_opened(dbos_env):
    from evowiki.wikiapp.page_runs import DbosPageRuns
    repo, s, ids, _kit, store = dbos_env
    pid = page1((repo, s, ids))
    job, old = _dbos_row(store, pid)             # started_at = NOW (2026-01-01), nobody started it
    _second_run_is_written(DbosPageRuns(store), store, repo, s, pid, job, old)


def test_dbos_a_pending_workflow_of_this_version_is_attached_not_failed(dbos_env):
    from evowiki.wikiapp.page_runs import DbosPageRuns
    repo, s, ids, kit, store = dbos_env
    pid = page1((repo, s, ids))
    script_page(s)
    gate = threading.Event()
    kit.writer.before_write = gate.wait
    runs = DbosPageRuns(store)
    job = PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW)
    a, _ = runs.start(job, page_flows.installed(), attach=True)
    assert runs.sweep() == 0 and store.get(a).status == "writing"
    assert runs.start(job, page_flows.installed(), attach=True) == (a, False)
    gate.set()
    assert names(list(runs.follow(a)))[-1] == "done"


def test_dbos_sweep_closes_every_writing_row_that_can_never_finish(dbos_env):
    from evowiki.wikiapp.page_runs import DbosPageRuns
    repo, s, ids, _kit, store = dbos_env
    pid = page1((repo, s, ids))
    _, row_a = _dbos_row(store, pid)
    _ended_in_error(row_a.run_id)
    _, row_b = _dbos_row(store, pid, attach=False)            # run 2: old row, no workflow
    assert DbosPageRuns(store).sweep() == 2
    assert {store.get(r.run_id).status for r in (row_a, row_b)} == {"failed"}
    assert store.writing_rows() == []


def test_follow_ends_early_when_a_stream_item_cannot_be_decoded(dbos_env, monkeypatch):
    from evowiki.wikiapp import page_runs
    from evowiki.wikiapp.page_runs import DbosPageRuns
    repo, s, ids, _kit, store = dbos_env
    pid = page1((repo, s, ids))
    script_page(s)
    runs = DbosPageRuns(store)
    job = PageJob(kind="organize", user_id="u1", page_id=pid, now=NOW)
    run_id, _ = runs.start(job, page_flows.installed(), attach=True)
    got = list(runs.follow(run_id))
    assert got[-1][0] == "done"
    real = page_runs.decode_item
    seen = []

    def flaky(d):
        seen.append(d)
        if len(seen) == 3:
            raise ValueError("unknown stream item type 'X'")
        return real(d)
    monkeypatch.setattr(page_runs, "decode_item", flaky)
    items = list(runs.follow(run_id))
    assert len(items) == 3 and items[-1][0] == "error" and items[-1][1].retryable   # 2 + end
