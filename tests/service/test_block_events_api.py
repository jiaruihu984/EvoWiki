"""EvoUI v2 result events and progress over HTTP and Postgres (migration 0030 on the disposable
test database): graded events, duplicates on resend, progress newer-only, GET progress, 422 / 404,
and the Pg store's race guard."""

from datetime import UTC, datetime

import pytest

from evowiki.deep.store import PgAssetStore
from evowiki.wikiapp.block_progress import PgProgressStore, Progress
from evowiki.wikiapp.repo import WikiConflict
from evowiki.wikiapp.wiki_schemas import BlockProgressOut, BlockResultsOut
from tests.service.conftest import H
from tests.service.test_wiki_api import organized_page, wiki  # noqa: F401 — fixture
from tests.wikiapp.test_block_events import ALL, V2, item
from tests.wikiapp.test_block_results import stored

AT = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


def _asset(wiki, services) -> str:  # noqa: F811
    pid = organized_page(wiki)
    mem, aid = stored(pid, block=V2)
    with services.pool.connection() as conn, conn.transaction():
        PgAssetStore(services.pool).writer(list(mem.rows.values()))(conn)
    return aid


def test_events_and_progress_routes(wiki, services):  # noqa: F811
    client, _ = wiki
    aid = _asset(wiki, services)
    url = f"/wiki/assets/{aid}/results"
    r = client.get(f"/wiki/assets/{aid}/progress", headers=H("u1"))
    assert r.status_code == 200 and r.json() == {"revision": 0, "data": None}
    data = {"v": 2, "revision": 9, "state": {}, "nodes": {"dq": {"current": 1}}}
    r = client.post(url, headers=H("u1"), json={"events": ALL, "progress": {"revision": 9,
                                                                            "data": data}})
    assert r.status_code == 200, r.text
    out = BlockResultsOut.model_validate(r.json())
    assert [o.status for o in out.events] == ["k", "k", "score", "k", "score", "k", "score", "k",
                                              "signal", "signal", "signal", "signal"]
    assert (out.right, out.total, out.progress_revision, out.progress_stale) == (6, 8, 9, False)
    assert out.signal_id
    r = client.post(f"/wiki/pages/{out.page_id}/checks/s2/followup", headers=H("u1"),
                    json={"choice": "unclear", "answer_ref": out.events[0].answer_ref})
    assert r.status_code == 200, r.text

    r = client.post(url, headers=H("u1"), json={"events": ALL, "progress": {"revision": 3,
                                                                            "data": {"v": 2}}})
    again = BlockResultsOut.model_validate(r.json())
    assert {o.status for o in again.events} == {"duplicate"} and again.signal_id is None
    assert (again.progress_revision, again.progress_stale) == (9, True)
    got = BlockProgressOut.model_validate(
        client.get(f"/wiki/assets/{aid}/progress", headers=H("u1")).json())
    assert got.revision == 9 and got.data == data

    assert client.post(url, headers=H("u1"),
                       json={"events": [item("nope", "quiz", 0, 0)]}).status_code == 422
    assert client.post(url, headers=H("u1"),
                       json={"events": [{"type": "item", "node": "dq"}]}).status_code == 422
    assert client.get("/wiki/assets/ia_nope/progress", headers=H("u1")).status_code == 404
    assert client.post("/wiki/assets/ia_nope/results", headers=H("u1"),
                       json={"progress": {"revision": 1, "data": {}}}).status_code == 404


def test_pg_store_newer_only_and_race_guard(wiki, services):  # noqa: F811
    aid = _asset(wiki, services)
    store = PgProgressStore(services.pool)
    assert store.put("u1", aid, Progress(0, {}), at=AT) == (0, False)
    assert store.put("u1", aid, Progress(4, {"a": 1}), at=AT) == (4, True)
    assert store.put("u1", aid, Progress(4, {"a": 2}), at=AT) == (4, False)
    assert store.put("u1", aid, Progress(5, {"a": 3}), at=AT) == (5, True)
    assert store.get("u1", aid) == Progress(5, {"a": 3})
    with services.pool.connection() as conn, conn.transaction():
        store.events_writer("u1", aid, ["dq:0:1:5"], at=AT)(conn)
    assert store.seen("u1", aid, {"dq:0:1:5", "dq:1:1:5"}) == {"dq:0:1:5"}
    with pytest.raises(WikiConflict), services.pool.connection() as conn, conn.transaction():
        store.events_writer("u1", aid, ["dq:1:1:5", "dq:0:1:5"], at=AT)(conn)
    assert store.seen("u1", aid, {"dq:1:1:5"}) == set()          # rolled back with the commit


def test_replayed_results_cannot_restore_suppressed_asset_evidence(wiki, services, monkeypatch):  # noqa: F811
    from evowiki.memory.forget import suppress_sources
    from evowiki.wikiapp.lineage import asset_artifact
    from evowiki.wikiapp.repo import PgRepo

    client, _ = wiki
    aid = _asset(wiki, services)
    original = PgRepo.commit
    calls = []

    def suppress_before_commit(repo, snap, commit, **kwargs):
        if not calls:
            calls.append(True)
            with services.pool.connection() as conn, conn.transaction():
                suppress_sources(conn, "u1", "block-replay-test", [asset_artifact("u1", aid)])
        return original(repo, snap, commit, **kwargs)

    monkeypatch.setattr(PgRepo, "commit", suppress_before_commit)
    with services.pool.connection() as conn:
        before = conn.execute("select count(*) as n from wiki_event where user_id='u1'").fetchone()["n"]
    response = client.post(f"/wiki/assets/{aid}/results", headers=H("u1"), json={"events": [ALL[0]]})
    assert response.status_code == 404, response.text
    with services.pool.connection() as conn:
        assert conn.execute("select count(*) as n from wiki_event where user_id='u1'").fetchone()["n"] == before
        assert conn.execute("select count(*) as n from block_event where user_id='u1' and asset_id=%s", (aid,)).fetchone()["n"] == 0


def test_result_replay_is_retryable_while_forget_publication_lock_is_held(wiki, services):  # noqa: F811
    client, _ = wiki
    aid = _asset(wiki, services)
    with services.pool.connection() as conn, conn.transaction():
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", ("memory:user:u1",))
        response = client.post(f"/wiki/assets/{aid}/results", headers=H("u1"), json={"events": [ALL[0]]})
        assert response.status_code == 409, response.text
    replay = client.post(f"/wiki/assets/{aid}/results", headers=H("u1"), json={"events": [ALL[0]]})
    assert replay.status_code == 200, replay.text
    assert replay.json()["events"][0]["status"] == "k"


def test_suppressed_asset_cannot_restore_saved_progress(wiki, services):  # noqa: F811
    from evowiki.memory.forget import suppress_sources
    from evowiki.wikiapp.lineage import asset_artifact

    client, _ = wiki
    aid = _asset(wiki, services)
    with services.pool.connection() as conn:
        suppress_sources(conn, "u1", "block-progress-test", [asset_artifact("u1", aid)])
    response = client.post(f"/wiki/assets/{aid}/results", headers=H("u1"),
                           json={"progress": {"revision": 8, "data": {"v": 2}}})
    assert response.status_code == 404, response.text
    assert PgProgressStore(services.pool).get("u1", aid) is None
