"""Scoped commit conflicts on the real flows: the Bot committing a page it wrote ahead (page X:
PageSaved organized, its SectionLogsSaved and SeriesUpdated - what `organize` commits) lands
between the learner's load and commit of 学完 on another page Y (`page.mark_done`) or 下一页 on
the same path (`paths.next_path_page`: page record + PathSaved). They write
different records, so neither gets a 409 any more."""

import pytest

from evowiki.goals.contracts import SectionRole
from evowiki.teach.contracts import SectionLog
from evowiki.wikiapp.events import PageSaved, SectionLogsSaved, SeriesUpdated, WikiEvent
from evowiki.wikiapp.page import mark_done
from evowiki.wikiapp.path_order import startable
from evowiki.wikiapp.paths import next_path_page, plan_page
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.series import SeriesUpdate
from tests.wikiapp.goal_script import auto_probe
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_progress import enter, goal6, write_body
from tests.wikiapp.test_state import page as a_page


@pytest.fixture
def env():
    s = Script()
    auto_probe(s)
    return MemoryRepo(["u1"]), s, Ids()


class Racing:
    """The repo the learner's flow uses: `race` runs (another writer commits) right before the
    flow's first commit, i.e. after the flow loaded its snapshot."""

    def __init__(self, inner, race):
        self.inner, self.race = inner, race

    def commit(self, snap, c, *, once=None):
        race, self.race = self.race, None
        if race is not None:
            race()
        return self.inner.commit(snap, c, once=once)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def bot_writes_ahead(env, path_id, page_id):
    """What the organize stream commits for a page the idle Bot wrote: the written record, its
    section logs and its series line."""
    repo, _, ids = env
    sn = repo.load("u1")
    rec = sn.state.pages[page_id].model_copy(update={"page": a_page(page_id)})
    log = SectionLog(page_id=page_id, section_id="s1", at=NOW, producer="writer",
                     role=SectionRole.EXPLAIN.value, spec_version=1, infer_version=1)
    repo.commit(sn, Commit(wiki=[
        WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(record=rec, action="organized")),
        WikiEvent(event_id=ids("wev"), ts=NOW, body=SectionLogsSaved(logs=(log,))),
        WikiEvent(event_id=ids("wev"), ts=NOW, body=SeriesUpdated(
            path_id=path_id, page_id=page_id, title=rec.title,
            update=SeriesUpdate(summary="写好了")))]))


def plan_ahead(env, path_id, index):
    """The Bot plans the path's page `index` (1-based in reading order) to write it ahead."""
    repo, _, ids = env
    sn = repo.load("u1")
    key = list(startable(sn.state.paths[path_id]))[index - 1].key
    planned = plan_page(sn, path_id, key, now=NOW, new_id=ids)
    repo.commit(sn, Commit(wiki=planned.events))
    return planned.page_id


def test_x_written_ahead_while_y_is_marked_done(env):
    repo, _, ids = env
    out = enter(env, goal6(env), "c1", ["c7"], ["c8"])
    y = next_path_page(repo, "u1", out.path_id, now=NOW, new_id=ids).page_id
    write_body(env, y)
    x = plan_ahead(env, out.path_id, 2)
    racing = Racing(repo, lambda: bot_writes_ahead(env, out.path_id, x))
    mark_done(racing, "u1", y, now=NOW, new_id=ids)          # no WikiConflict
    st = repo.load("u1").state
    assert st.pages[y].done and st.pages[x].page is not None and racing.race is None
    assert x in st.series[out.path_id].model_dump_json()


def test_x_written_ahead_while_next_page_starts_on_the_same_path(env):
    repo, _, ids = env
    out = enter(env, goal6(env), "c1", ["c7"], ["c8"])
    x = plan_ahead(env, out.path_id, 2)                  # the Bot writes page 2 ahead
    racing = Racing(repo, lambda: bot_writes_ahead(env, out.path_id, x))
    got = next_path_page(racing, "u1", out.path_id, now=NOW, new_id=ids)   # no WikiConflict
    st = repo.load("u1").state
    assert got.index == 1 and got.page_id in st.pages and racing.race is None
    assert st.paths[out.path_id].page_ids[0] == got.page_id
    assert st.pages[x].page is not None
