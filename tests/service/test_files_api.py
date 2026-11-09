"""Side-effect-free reads for the plugin's files (assistant-actions Task 13): GET /wiki/files,
GET /wiki/writes without ids, GET /wiki/pages/{id}/run/stream. None of them opens a page, calls a
model, starts a run or marks a run read."""
from datetime import UTC, datetime

import pytest

from evowiki.service.deps import page_deps
from evowiki.wikiapp.page_runs import PageJob
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.wiki_schemas import OrganizeDoneEvent
from tests.service.conftest import H, parse_sse
from tests.service.test_wiki_api import new_page, wiki  # noqa: F401  (fixture)
from tests.service.test_writes_api import queue  # noqa: F401  (fixture)


def snapshot(services):
    return PgRepo(services.pool).load("u1").state.model_dump_json()


def live_run(services, pid):
    """A page run that is queued and never executed (its worker is not spawned): the items are put in
    its log by hand."""
    services.runs._spawn = lambda work: None
    job = PageJob(kind="organize", user_id="u1", page_id=pid, now=datetime.now(UTC))
    run_id, started = services.runs.start(job, page_deps(services), attach=True)
    assert started
    return run_id, services.runs._logs[run_id]


def done_event(pid):
    return OrganizeDoneEvent(page_id=pid, llm_calls=0, cost_usd=0.0, model="",
                             fallback=False, prefetched=False)


def test_files_lists_every_page_without_touching_anything(wiki, services):  # noqa: F811
    client, script = wiki
    pid = new_page(wiki)
    before, calls = snapshot(services), script.calls()
    r = client.get("/wiki/files", headers=H("u1"))
    assert r.status_code == 200, r.text
    [f] = r.json()
    assert f["page_id"] == pid and f["organized"] is False and f["trashed"] is False
    assert {"title", "folder", "parent_name"} <= set(f)
    assert snapshot(services) == before            # no PageOpened, nothing saved
    assert script.calls() == calls                 # no model call
    assert services.runs.store.live_of("u1") == []  # no write started
    assert client.get("/wiki/files", headers=H("u2")).status_code == 404       # not u1's pages


def test_writes_without_ids_lists_the_live_writes_only(wiki, services, queue):  # noqa: F811
    client, script = wiki
    pid = new_page(wiki)
    calls = script.calls()
    run_id, _ = live_run(services, pid)
    [w] = client.get("/wiki/writes", headers=H("u1")).json()
    assert (w["page_id"], w["run_id"], w["state"]) == (pid, run_id, "queued")
    services.runs.store.begin(run_id, datetime.now(UTC))
    [w] = client.get("/wiki/writes", headers=H("u1")).json()
    assert w["state"] == "planning"
    assert script.calls() == calls


def test_run_stream_follows_a_live_run_without_consuming_it(wiki, services):  # noqa: F811
    client, script = wiki
    pid = new_page(wiki)
    run_id, log = live_run(services, pid)
    log.emit("done", done_event(pid))
    # the run is finished in its log but its row is still live (the worker never ran): a watcher reads it
    before, calls = snapshot(services), script.calls()
    r = client.get(f"/wiki/pages/{pid}/run/stream?run_id={run_id}", headers=H("u1"))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert [n for n, _ in parse_sse(r.text)] == ["done"]
    row = services.runs.store.get(run_id)
    assert row.consumed_at is None and row.status == "writing"      # not consumed, not finished
    # it can be followed again from the start, and the real follower still gets it
    assert [n for n, _ in parse_sse(client.get(f"/wiki/pages/{pid}/run/stream?run_id={run_id}", headers=H("u1")).text)] == ["done"]
    assert snapshot(services) == before and script.calls() == calls
    assert [x.run_id for x in services.runs.store.live_of("u1")] == [run_id]   # no second run started


def test_run_stream_404_without_a_live_run(wiki, services):  # noqa: F811
    client, _ = wiki
    pid = new_page(wiki)
    before = snapshot(services)
    run_id = "missing"
    assert client.get(f"/wiki/pages/{pid}/run/stream?run_id={run_id}", headers=H("u1")).status_code == 404
    assert client.get("/wiki/pages/nope/run/stream?run_id=missing", headers=H("u1")).status_code == 404
    run_id, _ = live_run(services, pid)
    services.runs.store.discard(run_id, datetime.now(UTC))
    assert client.get(f"/wiki/pages/{pid}/run/stream?run_id={run_id}", headers=H("u1")).status_code == 404
    assert snapshot(services) == before
    assert services.runs.store.live_of("u1") == []


@pytest.mark.parametrize("path", ["/wiki/files", "/wiki/writes"])
def test_reads_need_a_user(wiki, path):  # noqa: F811
    client, _ = wiki
    assert client.get(path).status_code in (401, 422)


def test_peek_is_the_page_without_opening_it(wiki, services, queue):  # noqa: F811
    client, script = wiki
    pid = new_page(wiki)
    kicks = []
    queue.kick_soon = lambda uid: kicks.append(uid)
    before, calls = snapshot(services), script.calls()
    r = client.get(f"/wiki/pages/{pid}", headers=H("u1"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["page_id"] == pid and {"folder", "parent_name", "trashed", "sections"} <= set(out)
    assert snapshot(services) == before            # no PageOpened / PageSaved
    assert script.calls() == calls                 # no model call
    assert kicks == [] and services.runs.store.live_of("u1") == []
    # the same page, opened by the user, is what records the open
    assert client.post(f"/wiki/pages/{pid}/open", headers=H("u1")).status_code == 200
    assert snapshot(services) != before
    assert client.get("/wiki/pages/nope", headers=H("u1")).status_code == 404


def test_delayed_run_stream_never_substitutes_the_new_run(wiki, services):  # noqa: F811
    client, _ = wiki
    pid = new_page(wiki)
    first, first_log = live_run(services, pid)
    first_log.emit("done", done_event(pid))
    services.runs.store.finish(first, "done", datetime.now(UTC))
    services.runs.store.consume(first, datetime.now(UTC))
    second, second_log = live_run(services, pid)
    second_log.emit("done", done_event(pid))
    response = client.get(f"/wiki/pages/{pid}/run/stream?run_id={first}", headers=H("u1"))
    assert response.status_code == 200
    # The consumed inline log was pruned when R2 began: explicit replay failure, never R2 done.
    assert [name for name, _ in parse_sse(response.text)] == ["error"]
    assert services.runs.store.get(second).consumed_at is None
    assert client.get(f"/wiki/pages/{pid}/run/stream?run_id={first}", headers=H("u2")).status_code == 404
    assert client.get(f"/wiki/pages/{pid}/run/stream", headers=H("u1")).status_code == 422
