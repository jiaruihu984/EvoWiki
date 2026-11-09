"""SeriesUpdated folds into WikiState.series by merging (B2 Task 2, final fix item 1): the note does
not depend on the order in which pages commit."""

import pytest

from evowiki.wikiapp.events import SeriesUpdated, WikiEvent
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.series import SeriesUpdate, merge
from evowiki.wikiapp.state import apply_event, replay
from tests.runtime.helpers import T0
from tests.wikiapp.test_paths import big_goal


@pytest.fixture
def env():
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_goal import Ids
    return MemoryRepo(["u1"]), Script(), Ids()


A = SeriesUpdate(example="抽奖箱", terms=("总体",), summary="用抽奖箱引入总体。")
B = SeriesUpdate(example="另一个例子", terms=("样本",), summary="抽样。")


def _ev(i: int, path_id: str, page_id: str, title: str, update: SeriesUpdate) -> WikiEvent:
    return WikiEvent(event_id=f"s{i}", ts=T0, body=SeriesUpdated(
        path_id=path_id, page_id=page_id, title=title, update=update))


def test_series_updates_merge_onto_the_path_note(env):
    repo, _, _ = env
    res, _ = big_goal(env)
    p = res.path_id
    st = repo.load("u1").state
    assert st.series == {}
    s = apply_event(st, _ev(1, p, "a", "A", A))
    assert s.series[p] == merge(None, A, page_id="a", title="A")
    s = apply_event(s, _ev(2, p, "b", "B", B))
    assert s.series == {p: merge(merge(None, A, page_id="a", title="A"), B, page_id="b",
                                 title="B")}
    assert s.series[p].example == "抽奖箱"             # the first example is kept
    assert [pg.page_id for pg in s.series[p].pages] == ["a", "b"]
    assert {t.term for t in s.series[p].terms} == {"总体", "样本"}
    assert s.paths == st.paths


def test_a_later_page_committed_first_does_not_erase_the_earlier_one(env):
    """Page b's update committed before page a's: both lines and both terms stay."""
    repo, _, _ = env
    res, _ = big_goal(env)
    p = res.path_id
    st = repo.load("u1").state
    s = apply_event(apply_event(st, _ev(1, p, "b", "B", B)), _ev(2, p, "a", "A", A))
    note = s.series[p]
    assert {pg.page_id for pg in note.pages} == {"a", "b"}
    assert {t.term for t in note.terms} == {"总体", "样本"}


def test_no_series_events_means_empty_series():
    assert replay([]).series == {}
