"""Page revisions from materials over HTTP (materials phase 6 §7 D): the start routes, the draft as
`ActionOut.revise` (track changes in the page), approve / undo through the ledger. Real Postgres,
inline page runs, a scripted page writer (`ReviseWriter`), sources seeded in the tables; no LLM."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from evowiki.bot.actions import ActionLedger
from evowiki.bot.change_source import LISTED_SOURCES
from evowiki.deep.store import MemoryAssetStore
from evowiki.service.app import create_app
from evowiki.service.deps import register_revise
from evowiki.sources import store as source_store
from evowiki.wikiapp.events import PageRecord, PageSaved, PagesTrashed, WikiEvent
from evowiki.wikiapp.organize import Writing
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.revise import KIND
from evowiki.wikiapp.source_revise import (
    BUSY,
    NO_RUN_GRACE,
    STUCK,
    TRASHED,
    USER_NOTE,
    open_draft,
)
from tests.assistant.tools._source_fakes import seed_pg
from tests.runtime.helpers import T0
from tests.service.conftest import H, signup
from tests.wikiapp.script import Script
from tests.wikiapp.test_organize import add_page
from tests.wikiapp.test_page import organized
from tests.wikiapp.test_source_revise import ReviseWriter

U = H("u1")


class Env:
    def __init__(self, services, client):
        self.svc, self.client = services, client
        signup(client)
        self.repo = PgRepo(services.pool)
        self.pid = organized((self.repo, Script(), services.new_id))
        ledger = ActionLedger(services.pool, services.clock, services.new_id, executors={})
        services.actions = ledger
        register_revise(services, ledger)
        self.writer = ReviseWriter()
        services.page_writing = Writing(writer=self.writer, specs=WikiSpecSource(self.repo),
                                        assets=MemoryAssetStore())
        services.gateway = Script().gateway()           # the run checks one is configured
        title = self.rec().title
        with services.pool.connection() as conn:
            seed_pg(conn, "src1", "u1", [f"{title}：z=-1 表示低一个标准差。"], services.embedder)

    def rec(self, pid=None) -> PageRecord:
        return self.repo.load("u1").state.pages[pid or self.pid]

    def revise(self, pid=None, **body):
        return self.client.post(f"/wiki/pages/{pid or self.pid}/revise", json=body, headers=U)

    def action(self, action_id):
        rows = self.client.get("/wiki/bot/actions", headers=U).json()
        return next(r for r in rows if r["action_id"] == action_id)

    def second_page(self) -> str:
        """Another organized page (a copy of the first one's body)."""
        pid = add_page(self.repo, self.svc.new_id, title="z 分数（复习）")
        snap = self.repo.load("u1")
        rec = snap.state.pages[pid].model_copy(update={"page": self.rec().page})
        self.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=self.svc.new_id("wev"), ts=T0,
                                                      body=PageSaved(record=rec, action="t"))]))
        return pid


@pytest.fixture
def env(services, client):
    return Env(services, client)


def test_a_revision_writes_a_draft_the_page_shows_as_track_changes(env):
    before = page_fingerprint(env.rec())
    r = env.revise(source_ids=["src1"])
    assert r.status_code == 202, r.text
    got = r.json()
    assert got["run_id"] and got["page_id"] == env.pid
    a = env.action(got["action_id"])                      # inline run: written already
    assert (a["kind"], a["source"], a["level"], a["status"]) == (KIND, "user", "ask", "proposed")
    rv = a["revise"]
    assert rv["state"] == "ready" and rv["run_id"] == got["run_id"] and a["confirm"] is True
    assert rv["sources"] == [{"source_id": "src1", "title": "src1", "deleted": False}]
    assert {s["section_id"] for s in rv["sections"]} == {"s1", "s2", "s3"}
    s2 = next(s for s in rv["sections"] if s["section_id"] == "s2")
    # the writer cites 「讲义」, no material of this draft: removed by the check, one issue each
    assert s2["status"] == "changed" and s2["new_body"] == "新的s2" and s2["old_body"]
    assert rv["unchanged"] == 0 and rv["rebuild"] is False and rv["cite_issues"] == 3
    assert a["diff"]                                       # the 改动对比 from the same view
    assert page_fingerprint(env.rec()) == before          # nothing committed yet
    inp = env.writer.inputs[0]
    assert inp.revise is not None and "低一个标准差" in inp.sources[0].text


def test_approve_takes_no_draft_then_swaps_the_page_and_undo_puts_it_back(env):
    aid = env.revise(source_ids=["src1"]).json()["action_id"]
    before = env.rec().page
    r = env.client.post(f"/wiki/bot/actions/{aid}/approve", json={"draft": {"x": 1}}, headers=U)
    assert r.status_code == 409                            # the draft is not editable
    r = env.client.post(f"/wiki/bot/actions/{aid}/approve", headers=U)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "executed" and r.json()["can_undo"] is True
    assert env.rec().page.texts["s2"].body == "新的s2"
    executed = r.json()["revise"]                          # diffed against the page it replaced
    assert {s["section_id"] for s in executed["sections"]} == {"s1", "s2", "s3"}
    r = env.client.post(f"/wiki/bot/actions/{aid}/undo", headers=U)
    assert r.status_code == 200 and r.json()["status"] == "undone"
    assert env.rec().page == before


def test_a_ready_draft_of_a_page_changed_since_is_stale(env):
    aid = env.revise(source_ids=["src1"]).json()["action_id"]
    assert env.action(aid)["revise"]["stale"] is False
    snap = env.repo.load("u1")
    rec = env.rec()
    page = rec.page.model_copy(update={"texts": {**rec.page.texts, "s2": rec.page.texts["s2"]
                                                 .model_copy(update={"body": "别处改过"})}})
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.svc.new_id("wev"), ts=T0, body=PageSaved(
        record=rec.model_copy(update={"page": page}), action="t"))]))
    assert env.action(aid)["revise"]["stale"] is True
    r = env.client.post(f"/wiki/bot/actions/{aid}/approve", headers=U)   # what the bar spares
    assert r.json()["status"] == "failed"


def test_redraft_writes_a_new_draft_from_the_same_materials_against_the_page_now(env):
    """重试 on a stale materials draft (a whole-page swap is never re-anchored): the old draft is
    dropped and a new one is written from the same materials; it fits the page as it is now."""
    aid = env.revise(source_ids=["src1"]).json()["action_id"]
    snap = env.repo.load("u1")
    rec = env.rec()
    page = rec.page.model_copy(update={"texts": {**rec.page.texts, "s2": rec.page.texts["s2"]
                                                 .model_copy(update={"body": "别处改过"})}})
    env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.svc.new_id("wev"), ts=T0, body=PageSaved(
        record=rec.model_copy(update={"page": page}), action="t"))]))
    r = env.client.post(f"/wiki/bot/actions/{aid}/redraft", headers=U)
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["action_id"] != aid and new["kind"] == KIND and new["source"] == "user"
    assert env.action(aid)["status"] == "rejected"
    fresh = env.action(new["action_id"])                  # inline run: written already
    assert fresh["revise"]["state"] == "ready" and fresh["revise"]["stale"] is False
    assert [s["source_id"] for s in fresh["revise"]["sources"]] == ["src1"]
    r = env.client.post(f"/wiki/bot/actions/{new['action_id']}/approve", headers=U)
    assert r.json()["status"] == "executed"
    r = env.client.post(f"/wiki/bot/actions/{aid}/redraft", headers=U)   # decided otherwise
    assert r.status_code == 409


def test_no_material_named_the_run_picks_one(env):
    r = env.revise()
    assert r.status_code == 202, r.text
    rv = env.action(r.json()["action_id"])["revise"]
    assert rv["state"] == "ready" and [s["source_id"] for s in rv["sources"]] == ["src1"]


def test_pasted_text_is_imported_as_a_material_of_the_page_first(env):
    r = env.revise(text="老师讲：z=-1 表示比均值低一个标准差。")
    assert r.status_code == 202, r.text
    a = env.action(r.json()["action_id"])
    [sid] = a["content"]["source_ids"]
    with env.svc.pool.connection() as conn:
        uses = source_store.uses_of(conn, "u1", sid)
    assert [(u["use"], u["page_id"]) for u in uses] == [("supplement", env.pid)]
    assert a["revise"]["state"] == "ready"


def test_bad_requests(env):
    assert env.revise(url="https://example.com", text="x").status_code == 422
    assert env.revise(source_ids=["s"] * 6).status_code == 422
    assert env.revise(source_ids=["nope"]).status_code == 404
    assert env.revise("pg_nope").status_code == 404
    note = add_page(env.repo, env.svc.new_id, title="我的笔记")       # never organized
    r = env.revise(note)
    assert r.status_code == 422 and r.json()["detail"] == USER_NOTE
    assert not [a for a in env.client.get("/wiki/bot/actions", headers=U).json()
                if a["kind"] == KIND]


def test_a_second_draft_of_the_same_page_is_refused_with_the_open_one(env):
    first = env.revise(source_ids=["src1"]).json()["action_id"]
    r = env.revise(source_ids=["src1"])
    assert r.status_code == 409
    assert r.json() == {"detail": BUSY, "action_id": first}
    env.client.post(f"/wiki/bot/actions/{first}/reject", headers=U)
    assert env.revise(source_ids=["src1"]).status_code == 202      # closed: a new one is fine


def test_the_bots_update_starts_one_draft_per_page_without_an_open_one(env):
    p2, p3 = env.second_page(), env.second_page()
    busy = env.revise(p2, source_ids=["src1"]).json()["action_id"]
    r = env.client.post("/wiki/sources/src1/revise", json={"page_ids": [env.pid, p2, p3]},
                        headers=U)
    assert r.status_code == 200, r.text
    started = r.json()["started"]
    assert [s["page_id"] for s in started] == [env.pid, p3]
    for s in started:
        a = env.action(s["action_id"])
        assert a["source"] == "bot" and a["revise"]["state"] == "ready"
        assert [x["source_id"] for x in a["revise"]["sources"]] == ["src1"]
    assert env.action(busy)["source"] == "user"
    assert env.client.post("/wiki/sources/nope/revise", json={"page_ids": [env.pid]},
                           headers=U).status_code == 404
    assert env.client.post("/wiki/sources/src1/revise", json={"page_ids": []},
                           headers=U).status_code == 422


def test_a_deleted_or_trashed_page_is_skipped_and_the_others_start(env):
    p2, p3 = env.second_page(), env.second_page()
    env.repo.commit(env.repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=env.svc.new_id("wev"), ts=T0, body=PagesTrashed(page_ids=(p3,)))]))
    r = env.client.post("/wiki/sources/src1/revise",
                        json={"page_ids": [env.pid, "pg_gone", p2]}, headers=U)
    assert r.status_code == 200, r.text                     # a deleted page: the other two start
    assert [s["page_id"] for s in r.json()["started"]] == [env.pid, p2]
    for s in r.json()["started"]:
        env.client.post(f"/wiki/bot/actions/{s['action_id']}/reject", headers=U)
    r = env.client.post("/wiki/sources/src1/revise", json={"page_ids": [p3, p2]}, headers=U)
    assert [s["page_id"] for s in r.json()["started"]] == [p2]     # the trashed one is skipped
    r = env.revise(p3, source_ids=["src1"])                 # a page in the trash is not revised
    assert r.status_code == 422 and r.json()["detail"] == TRASHED


def test_a_writing_draft_whose_run_cannot_fill_it_is_failed_not_blocking(env):
    ledger = env.svc.actions

    def writing(key: str, **extra):
        return ledger.propose(
            "u1", kind=KIND, scope="page_body", source="user", summary="按资料改", why="",
            content={"state": "writing", "confirm": True, "page_id": env.pid, "source_ids": [],
                     "detail": "", "draft": None, **extra},
            key=key, page_id=env.pid, page_version="v", level="ask")

    def state(aid):
        c = ledger.get("u1", aid).content
        return c["state"], c["detail"]
    young = writing("k1")                   # no run id yet, just made: its start is under way
    assert env.revise(source_ids=["src1"]).status_code == 409
    later = T0 + NO_RUN_GRACE + timedelta(seconds=1)
    assert open_draft(ledger, env.svc.runs, "u1", env.pid, later) is None   # its start was lost
    assert state(young.action_id) == ("failed", STUCK)
    gone = writing("k2", run_id="page:u1:nope:1")           # a run that is not there
    r = env.revise(source_ids=["src1"])
    assert r.status_code == 202, r.text
    assert state(gone.action_id) == ("failed", STUCK)
    done = r.json()["action_id"]                             # its run is done: ready
    env.client.post(f"/wiki/bot/actions/{done}/reject", headers=U)
    ended = writing("k3", run_id=r.json()["run_id"])         # a run that ended without filling it
    assert env.revise(source_ids=["src1"]).status_code == 202
    assert state(ended.action_id) == ("failed", STUCK)


def test_user_started_revisions_are_listed_as_changes():
    assert "user" in LISTED_SOURCES


def test_without_a_ledger_the_routes_say_unavailable(services):
    c = TestClient(create_app(services), base_url="http://127.0.0.1")
    signup(c)
    assert c.post("/wiki/pages/p/revise", json={}, headers=U).status_code == 503


def test_the_assistants_add_source_and_revise_page_go_through_the_services(env):
    """`assistant_import` / `assistant_revise` (what build_services hands the assistant)."""
    from evowiki.assistant.tools.actions import ImportFailed, ImportRequest
    from evowiki.service.deps import assistant_import, assistant_revise

    req = ImportRequest(url=None, text="老师讲：z=-1 表示低一个标准差。", title="课堂", use="supplement",
                        page_id=env.pid, session_id="s1")
    got = assistant_import(env.svc, "u1", req)
    assert got.title == "课堂"
    with env.svc.pool.connection() as conn:
        uses = source_store.uses_of(conn, "u1", got.source_id)
    assert [(u["use"], u["page_id"], u["session_id"]) for u in uses] == [
        ("supplement", env.pid, "s1")]
    bad = ImportRequest(url="http://127.0.0.1:1/x", text=None, title=None, use="supplement",
                        page_id=None, session_id=None)
    with pytest.raises(ImportFailed) as e:          # the guarded fetcher refuses a local address
        assistant_import(env.svc, "u1", bad)
    assert e.value.fetch
    row, run_id = assistant_revise(env.svc, "u1", env.pid, [got.source_id], "revise:u1:k1")
    assert row.source == "assistant" and row.kind == KIND and run_id
    assert env.action(row.action_id)["revise"]["state"] == "ready"


def test_an_executed_revision_is_listed_in_the_demos_words(env):
    from evowiki.bot.change_source import LedgerChangeSource
    from evowiki.service.deps import ledger_names
    aid = env.revise(source_ids=["src1"]).json()["action_id"]
    title = env.rec().title
    assert env.client.post(f"/wiki/bot/actions/{aid}/approve", headers=U).status_code == 200
    n = len(env.action(aid)["revise"]["sections"])
    src = env.action(aid)["revise"]["sources"][0]["title"]
    rows = LedgerChangeSource(env.svc.actions, names=ledger_names(env.svc.pool)).list("u1")
    row = next(r for r in rows if r.patch_id == aid)
    assert row.summary == f"「{title}」按《{src}》更新了 {n} 节"
    assert row.why == f"按《{src}》改这一页"                  # asked from the page (source user)
