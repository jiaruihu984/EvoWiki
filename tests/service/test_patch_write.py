"""Over HTTP and Postgres: an answer or a declaration starts no write and leaves nothing waiting (user
rulings 2026-11-04). What page feedback handed over is written in one scoped refine (trigger "patch",
run kind patch_rewrite) on an explicit open, when nothing else is due (`rsi_runs.stale_rewrite`). A failed write is recorded on the waiting set and retried once a day,
at most twice. Fake deep service, scripted LLM (no network)."""

from evowiki.belief.aspects import Declare
from evowiki.deep.contracts import RefineScope
from evowiki.service.rsi_runs import patch_write, stale_rewrite
from evowiki.wikiapp.kchange import k_changed
from evowiki.wikiapp.kwrite import apply_in_memory
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.review import sources
from evowiki.wikiapp.stale import patch_pending
from tests.service.conftest import H
from tests.service.test_stale_rewrite import _Deep, _FailingDeep, _open, wiki  # noqa: F401
from tests.service.test_wiki_api import organized_page
from tests.wikiapp.test_page import LINE


def _state(services):
    return PgRepo(services.pool).load("u1").state


def _runs(services):
    with services.pool.connection() as conn:
        return [(r["kind"], r["status"]) for r in conn.execute(
            "select kind, status from page_run where user_id = 'u1' order by started_at").fetchall()]


def _handed(wiki, services, deep):  # noqa: F811
    """A LINE page opened, then page feedback named c2.term as missing: s3 goes to the writer."""
    client, _ = wiki
    pid = organized_page(wiki, LINE)
    services.deep = deep
    _open(client, pid)
    repo = PgRepo(services.pool)
    snap = repo.load("u1")
    now = services.clock()
    aspect = [Declare(event_id=services.new_id("aev"), ts=now, cell="c2.term", value="dont")]
    kc = k_changed(snap, apply_in_memory(snap, aspect), pid, None, skip=None, now=now,
                   new_id=services.new_id, action="declare", declared_too=True)
    assert kc.change.rewritten == ["s3"]
    repo.commit(snap, Commit(aspect=aspect, wiki=kc.wiki))
    return pid


def test_a_declaration_starts_no_write_and_leaves_nothing_waiting(wiki, services):  # noqa: F811
    client, _ = wiki
    pid = organized_page(wiki, LINE)
    services.deep = deep = _Deep()
    _open(client, pid)
    r = client.post("/wiki/k/cells/c2.term/declare", headers=H("u1"), json={"value": "dont"})
    assert r.status_code == 200, r.text
    assert r.json()["change"]["rewritten"] == []
    assert patch_pending(_state(services), pid) == () and deep.started == []
    assert _open(client, pid)["rewriting"] is False and deep.started == []


def test_an_open_starts_the_due_write_and_says_rewriting(wiki, services):  # noqa: F811
    client, _ = wiki
    deep = _Deep()
    pid = _handed(wiki, services, deep)
    assert patch_pending(_state(services), pid) == ("s3",)
    assert _open(client, pid)["rewriting"] is True
    assert deep.started == [("u1", pid, "patch")]
    assert deep.ran == [("u1", pid, RefineScope(page_id=pid, sections=("s3",)))]
    assert ("patch_rewrite", "done") in _runs(services)


def test_a_failed_write_is_recorded_on_the_waiting_set_and_not_retried_at_once(
        wiki, services, caplog):  # noqa: F811
    client, _ = wiki
    pid = _handed(wiki, services, _FailingDeep())
    assert _open(client, pid)["rewriting"] is True
    f = _state(services).patch_failures[pid]
    assert (f.sections, f.failures) == (("s3",), 1)
    assert _state(services).stale_marks == {} and _state(services).reorder_marks == {}
    assert _open(client, pid)["rewriting"] is False            # failed less than a day ago
    services.clock.advance(hours=25)
    assert _open(client, pid)["rewriting"] is True             # a day later: one more try
    services.clock.advance(hours=25)
    assert _open(client, pid)["rewriting"] is False            # two failures: stopped
    assert patch_pending(_state(services), pid) == ("s3",)     # still waiting


def test_without_a_deep_service_nothing_starts_and_nothing_fails(wiki, services):  # noqa: F811
    pid = _handed(wiki, services, None)
    assert patch_write(services, "u1", pid) is False and stale_rewrite(services, "u1", pid) is False
    assert patch_pending(_state(services), pid) == ("s3",)
    assert all(kind != "patch_rewrite" for kind, _ in _runs(services))


def test_nothing_waiting_starts_nothing(wiki, services):  # noqa: F811
    client, _ = wiki
    services.deep = deep = _Deep()
    pid = organized_page(wiki, LINE)
    assert _open(client, pid)["rewriting"] is False
    assert patch_write(services, "u1", None) is False          # the current page: up to date
    assert deep.started == []


def test_a_finished_page_starts_its_cards_schedule_without_evidence(wiki, services):  # noqa: F811
    client, _ = wiki
    pid = organized_page(wiki, LINE)
    repo = PgRepo(services.pool)
    cards = {c for s in sources(repo.load("u1").state) if s.page_id == pid for c in s.cells}
    assert cards
    now = services.clock()
    before = {c: repo.load("u1").k.belief(c, now) for c in cards}
    r = client.post(f"/wiki/pages/{pid}/done", headers=H("u1"))
    assert r.status_code == 200, r.text
    k = repo.load("u1").k
    for c in cards:
        b = k.belief(c, now)
        assert (b.band, b.m, b.direct) == (before[c].band, before[c].m, before[c].direct)
        assert k.memory(c).last_tested is not None
