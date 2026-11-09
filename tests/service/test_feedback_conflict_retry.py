"""Page feedback / note edit: one retry on a fresh snapshot when a background commit got in first
(the 409 of the phase-2 live run); two conflicts in a row still surface as WikiConflict."""

import pytest

from evowiki.service import routes_wiki
from evowiki.teach.contracts import PageFeedbackIn
from evowiki.wikiapp.events import SignalRecorded
from evowiki.wikiapp.feedback import page_feedback_flow
from evowiki.wikiapp.organize import PageChanged
from evowiki.wikiapp.repo import WikiConflict
from tests.service.conftest import H, signup
from tests.service.test_wiki_api import wiki  # noqa: F401 — fixture
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized
from tests.wikiapp.test_teach_generation import SpyRepo


class ConflictRepo(SpyRepo):
    """Raises WikiConflict on the first `fail` commits."""

    def __init__(self, users, fail: int):
        super().__init__(users)
        self.fail = fail
        self.attempts = 0

    def commit(self, snap, c, *, once=None):
        self.attempts += 1
        if self.fail:
            self.fail -= 1
            raise WikiConflict()
        return super().commit(snap, c, once=once)



def signals(repo):
    return [b.signal for c in repo.commits for e in c.wiki
            if isinstance(b := e.body, SignalRecorded)]


def test_feedback_retries_once_and_records_one_signal():
    repo = ConflictRepo(["u1"], 0)
    ids = Ids()
    pid = organized((repo, Script(), ids))
    repo.fail, repo.attempts, repo.commits = 1, 0, []
    out = page_feedback_flow(repo, "u1", pid, PageFeedbackIn(chips=("unclear",)), now=NOW,
                             new_id=ids)
    assert repo.attempts == 2
    assert [s.signal_id for s in signals(repo)] == [out.signal_id]


def test_two_conflicts_in_a_row_still_raise():
    repo = ConflictRepo(["u1"], 0)
    ids = Ids()
    pid = organized((repo, Script(), ids))
    repo.fail, repo.attempts, repo.commits = 2, 0, []
    with pytest.raises(WikiConflict):
        page_feedback_flow(repo, "u1", pid, PageFeedbackIn(chips=("unclear",)), now=NOW,
                           new_id=ids)
    assert repo.attempts == 2 and signals(repo) == []


def test_page_changed_under_a_feedback_is_409_with_its_text(wiki, monkeypatch):  # noqa: F811
    """The section / page a feedback is about was rewritten meanwhile (PageChanged): 409, the
    status the section rewrite's `conflict` error event has, with the flow's plain text."""
    client, _ = wiki
    signup(client, "u1")

    def changed(*_a, **_k):
        raise PageChanged("这一节刚被别的流程改过，这次没有改，请刷新后重试")
    monkeypatch.setattr(routes_wiki, "feedback_know", changed)
    monkeypatch.setattr(routes_wiki, "page_feedback_loop", changed)
    r = client.post("/wiki/pages/p1/sections/s2/feedback", headers=H("u1"), json={"kind": "know"})
    assert r.status_code == 409 and r.json() == {
        "detail": "这部分内容已在别处修改，反馈没有应用，请刷新后重试", "code": "feedback_stale", "params": {}}
    r = client.post("/wiki/pages/p1/feedback", headers=H("u1"), json={"text": "看不懂"})
    assert r.status_code == 409 and r.json() == {
        "detail": "这部分内容已在别处修改，反馈没有应用，请刷新后重试", "code": "feedback_stale", "params": {}}
