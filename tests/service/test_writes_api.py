"""GET /wiki/writes (write-all, assistant-actions Task 7): where each page's write is, read from the
user's page runs; the write queue is wired as `build_services` does it (inline runs here)."""
from datetime import UTC

import pytest

from evowiki.service.deps import page_deps
from evowiki.wikiapp.repo import PgRepo
from evowiki.wikiapp.write_queue import PgIdleEnv, WriteQueue
from tests.service.conftest import H
from tests.service.test_wiki_api import new_page, script_page, wiki  # noqa: F401  (fixture)


@pytest.fixture
def queue(services, wiki):  # noqa: F811
    services.write_queue = WriteQueue(
        services.runs, lambda: page_deps(services), PgRepo(services.pool), services.clock,
        services.new_id, PgIdleEnv(services.pool, UTC, services.new_id), spawn=lambda w: w())
    return services.write_queue


def test_writes_lists_nothing_while_nothing_is_queued(wiki, queue):  # noqa: F811
    client, _ = wiki
    pid = new_page(wiki)
    assert client.get("/wiki/writes", headers=H("u1")).json() == []
    assert client.get("/wiki/writes", params={"page_ids": pid}, headers=H("u1")).json() == []


def test_writes_reports_a_written_page_with_its_sections_and_cost(wiki, queue):  # noqa: F811
    client, script = wiki
    pid = new_page(wiki)
    script_page(script)
    [run_id] = queue.request("u1", [pid])
    r = client.get("/wiki/writes", params={"page_ids": f"{pid},nope"}, headers=H("u1"))
    assert r.status_code == 200, r.text
    [w] = r.json()
    assert w["page_id"] == pid and w["state"] == "done" and "pass" in w
    assert (w["section"], w["of"], w["cost_usd"]) == (3, 3, 0.0)
    assert w["started_at"] is not None
    assert run_id.startswith(f"page:u1:{pid}:")
    # nothing is queued or writing any more
    assert client.get("/wiki/writes", headers=H("u1")).json() == []


def test_writes_says_unwritten_only_when_asked(wiki, queue):  # noqa: F811
    client, script = wiki
    blank = new_page(wiki)
    params = {"page_ids": f"{blank},nope", "unwritten": "true"}
    assert [(w["page_id"], w["state"]) for w in client.get(
        "/wiki/writes", params=params, headers=H("u1")).json()] == [(blank, "unwritten")]
    assert client.get("/wiki/writes", params={"page_ids": blank}, headers=H("u1")).json() == []
    script_page(script)
    queue.request("u1", [blank])
    assert [w["state"] for w in client.get(
        "/wiki/writes", params=params, headers=H("u1")).json()] == ["done"]


def test_writes_without_a_write_queue_is_empty(wiki):  # noqa: F811
    client, _ = wiki
    pid = new_page(wiki)
    assert pid and client.get("/wiki/writes", headers=H("u1")).json() == []


def test_run_cancel_stops_the_pages_live_run_and_only_the_owners(wiki, services):  # noqa: F811
    from tests.service.test_files_api import live_run
    client, script = wiki
    pid = new_page(wiki)
    run_id, _ = live_run(services, pid)
    calls = script.calls()
    assert client.post(f"/wiki/pages/{pid}/run/cancel", headers=H("u2")).status_code == 404
    assert client.post("/wiki/pages/nope/run/cancel", headers=H("u1")).status_code == 404
    assert services.runs.store.get(run_id).discarded_at is None
    r = client.post(f"/wiki/pages/{pid}/run/cancel", headers=H("u1"))
    assert r.status_code == 204, r.text
    assert services.runs.store.get(run_id).discarded_at is not None
    assert services.runs.store.live_of("u1") == [] and script.calls() == calls
    assert client.post(f"/wiki/pages/{pid}/run/cancel", headers=H("u1")).status_code == 404
