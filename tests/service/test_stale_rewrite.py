"""The lazy rewrite of a marked page over HTTP and Postgres (`service.rsi_runs.stale_rewrite`): a page
marked out of date is rewritten only when it is next opened (`?rewrite=1`), with the trigger of the
mark (a path-order mark: "reorder"); a failed rewrite counts on its own mark. Also the track-A
version read the cycle keys on. Kept from the retired RSI-B API tests (2026-10-29); `_Deep`, `_open`
and `wiki` are shared with `test_patch_write`. A fake deep service (no network)."""

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from evowiki.store.rsi import PgRsiStore, current_infer_version
from evowiki.teach.contracts import InferParams, PolicyVersion
from tests.service.conftest import H
from tests.service.test_wiki_api import organized_page
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW


@pytest.fixture
def wiki(services, client):
    script = Script()
    services.gateway = script.gateway()
    services.deep = _Deep()              # records the rewrites (tests may replace it)
    return client, script


def test_current_infer_version_follows_track_a(services, client):
    from tests.service.conftest import signup
    signup(client, "u1")
    store = PgRsiStore(services.pool)
    with services.pool.connection() as conn:
        assert current_infer_version(conn, "u1") == 1
    for v in (1, 2):
        store.add_version("u1", PolicyVersion(track="A", version=v, at=NOW, infer=InferParams()))
    with services.pool.connection() as conn:
        assert current_infer_version(conn, "u1") == 2


@dataclass
class _Deep:
    """Records the lazy rewrites (start on POST /wiki/pages/{id}/open → the page workflow runs it); no asset
    store / checker (the page-feedback route reads them). `hold`: a started job stays running (its
    run never finishes), so another start of the same page raises JobRunning like DeepService."""

    assets = None
    checker = None
    hold: bool = False
    started: list = field(default_factory=list)
    ran: list = field(default_factory=list)
    running: set = field(default_factory=set)

    def durable_copy(self):
        return self

    def start(self, uid, page_id, trigger):
        from evowiki.deep.contracts import RefineJob
        from evowiki.wikiapp.refine_service import JobRunning
        if page_id in self.running:
            raise JobRunning("页面正在精修")
        self.started.append((uid, page_id, trigger))
        self.running.add(page_id)
        return RefineJob(job_id=f"rf{len(self.started)}", page_id=page_id, trigger=trigger,
                         stage="queued", started_at=NOW)

    def run(self, uid, job, scope=None):
        return self.rewrite(uid, job, scope)

    def rewrite(self, uid, job, scope):
        self.ran.append((uid, job.page_id, scope))
        if not self.hold:
            self.running.discard(job.page_id)
        return SimpleNamespace(job=job.model_copy(update={"stage": "done"}))


def _open(client, pid):
    """A real open (the page view sends `rewrite=1`)."""
    r = client.post(f"/wiki/pages/{pid}/open", params={"rewrite": 1}, headers=H("u1"))
    assert r.status_code == 200, r.text
    return r.json()


class _FailingDeep(_Deep):
    def rewrite(self, uid, job, scope):
        super().rewrite(uid, job, scope)
        raise RuntimeError("refine broke")


class _PaidFailDeep(_Deep):
    """The run spends money, then ends failed (e.g. review_failed)."""

    def rewrite(self, uid, job, scope):
        out = super().rewrite(uid, job, scope)
        return type(out)(job=out.job.model_copy(update={
            "stage": "failed", "failure": "review_failed", "cost_usd": 0.4}))


def _mark_reordered(services, pid, job_id="run-x"):
    from evowiki.wikiapp.events import PageMarkedStale, WikiEvent
    from evowiki.wikiapp.repo import Commit, PgRepo
    repo = PgRepo(services.pool)
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=f"wev-{job_id}", ts=services.clock(),
        body=PageMarkedStale(page_id=pid, job_id=job_id))]))


def _state(services):
    from evowiki.wikiapp.repo import PgRepo
    return PgRepo(services.pool).load("u1").state


def test_a_reorder_marked_page_is_rewritten_with_its_own_trigger(wiki, services):
    client, _ = wiki
    services.deep = deep = _Deep()
    pid = organized_page(wiki)
    _mark_reordered(services, pid)
    _open(client, pid)
    assert [t for _, _, t in deep.started] == ["reorder"]
    _open(client, pid)


def test_a_failed_reorder_rewrite_counts_on_the_reorder_mark_only(wiki, services):
    client, _ = wiki
    services.deep = deep = _PaidFailDeep()
    pid = organized_page(wiki)
    _mark_reordered(services, pid)
    _open(client, pid)
    assert [t for _, _, t in deep.started] == ["reorder"]
    st = _state(services)
    assert st.reorder_marks[pid].failures == 1 and st.stale_marks == {}
    _open(client, pid)                                     # failed once, less than a day ago
    assert len(deep.started) == 1
