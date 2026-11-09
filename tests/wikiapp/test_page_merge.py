"""A merged page is written from the originals the merge job carries (D23), and pages in the trash
are not planning context (D25). Scripted organizer and writer: no model."""

import pytest

from evowiki.wikiapp.events import MergedFrom, PagesTrashed, WikiEvent
from evowiki.wikiapp.goal import page_events_for
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.page_merge import merged_text
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.repo import Commit, MemoryRepo
from tests.wikiapp.fake_writer import PLAN_PAGE as ORGANIZE
from tests.wikiapp.fake_writer import writing
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_organize import BODY2, add_page, run, script_page
from tests.wikiapp.test_page import organized


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def _merged(env, *originals: str) -> str:
    """A merged page record over `originals`, as `PageMergeExecutor` creates it."""
    repo, _, ids = env
    snap = repo.load("u1")
    recs = [snap.state.pages[p] for p in originals]
    froms = tuple(MergedFrom(page_id=r.page_id, title=r.title, version=page_fingerprint(r),
                             text=merged_text(r)) for r in recs)
    rec, evs = page_events_for(snap.state, snap.k, title="合并页", goal="把这几页合成一页",
                               concepts=recs[0].concepts, targets=recs[0].targets,
                               now=recs[0].created_at, new_id=ids, user_id="u1",
                               merged_from=froms)
    repo.commit(snap, Commit(wiki=evs))
    return rec.page_id


def test_the_merged_pages_first_write_reads_the_originals_text_not_only_titles(env):
    repo, s, ids = env
    a = organized(env)                                     # written: s2's body is BODY2
    b = add_page(repo, ids, title="z 分数（复习）")
    new = _merged(env, a, b)
    stored = repo.load("u1").state.pages[new].merged_from
    assert [m.page_id for m in stored] == [a, b] and BODY2 in stored[0].text
    script_page(s)
    kit = writing(env)
    run(env, new, writing=kit)
    prompt = s.requests(ORGANIZE)[-1].prompt               # the writer's outline part
    assert "z 分数怎么读" in prompt                          # the outline sees a's sections
    assert f"#p:{a}" not in prompt                          # ...and not a as a page to link to
    assert BODY2 in kit.writer.inputs[-1].instruction       # the writer gets a's whole text


def test_a_page_in_the_trash_is_no_planning_context(env):
    repo, _, ids = env
    a = organized(env)
    b = add_page(repo, ids)
    assert [p.page_id for p in page_spec(repo.load("u1").state, repo.load("u1").k, b).other_pages] == [a]
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(
        event_id=ids("wev"), ts=repo.load("u1").state.pages[a].created_at,
        body=PagesTrashed(page_ids=(a,)))]))
    snap = repo.load("u1")
    assert page_spec(snap.state, snap.k, b).other_pages == ()
