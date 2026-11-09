"""An assistant page made without a node (executors_assistant: `targets = ()`, e.g. 「帮我写一篇关于
均值的文章」) is a legal record; a catalog once crashed on it, so every later new page of that user
failed (POST /wiki/paths/{id}/next: the chat-tails walkthrough over a copy of the user's real data,
2026-10-24). The catalog lists it (tests/wikiapp/test_unnamed.py)."""

from evowiki.wikiapp.goal import page_events_for
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.views import catalog_out
from tests.wikiapp.test_goal import NOW, Ids


def test_a_page_without_targets_is_in_the_catalog_and_new_pages_still_work():
    repo, ids = MemoryRepo(["u1"]), Ids()
    snap = repo.load("u1")
    rec, evs = page_events_for(snap.state, snap.k, title="均值", goal="均值：均值是什么、怎么算",
                               concepts=(), targets=(), now=NOW, new_id=ids)
    repo.commit(snap, Commit(wiki=evs))
    snap = repo.load("u1")
    assert snap.state.pages[rec.page_id].targets == ()
    cat = catalog_out(snap.state, snap.k, NOW, user_id="u1")
    assert [p.page_id for p in cat.learning] == [rec.page_id]
    # the next new page (with a target) is made as before
    rec2, _ = page_events_for(snap.state, snap.k, title="方差", goal="方差",
                              concepts=("c1",), targets=("c1",), now=NOW, new_id=ids)
    assert rec2.targets == ("c1",)


def test_a_page_never_written_teaches_nothing():
    """D04: a page made but not written (its write still running or failed) is not 已经讲过 for
    planning another goal (goals.path's `taught`)."""
    from evowiki.wikiapp.goal import _taught
    repo, ids = MemoryRepo(["u1"]), Ids()
    snap = repo.load("u1")
    _, evs = page_events_for(snap.state, snap.k, title="向量与矩阵", goal="向量与矩阵",
                             concepts=("c7",), targets=("c7",), now=NOW, new_id=ids)
    repo.commit(snap, Commit(wiki=evs))
    assert _taught(repo.load("u1").state) == {}
