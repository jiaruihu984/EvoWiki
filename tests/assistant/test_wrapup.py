"""A session's background wrap-up (assistant/wrapup.py), with the scripted gateway: what a new
session asks for, what a run stores, how a failure is kept and tried again, and that the new
session's first message pays for none of it."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from evowiki.assistant.compact import DEFAULT_POLICY
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import run_turn
from evowiki.assistant.session import MemorySessionStore
from evowiki.assistant.wrapup import MAX_FAILED, InlineWrapups, Job, job_of, run_job
from evowiki.llm.chat import ChatTurn
from evowiki.llm.usage import Account, BudgetExceededError
from tests.assistant.fakes import INFO, T0, Ids
from tests.assistant.test_carryover import _gw, _session
from tests.assistant.test_loop import setup


def _deps(store, gw, memory=None):
    return SimpleNamespace(store=store, gateway=gw, memory=memory, compaction=DEFAULT_POLICY,
                           data=SimpleNamespace(language=lambda uid: "zh"), new_id=Ids(),
                           clock=lambda: T0 + timedelta(hours=1))


def _types(store, sid):
    return [e.type for e in store.entries(sid)]


def _opened(tmp_path, store, new, **kw):
    prov, gw = _gw(tmp_path, **kw)
    InlineWrapups(_deps(store, gw)).opened(new)
    return prov


def test_wrapup_no_longer_writes_closing_compaction(tmp_path):
    store = MemorySessionStore()
    info = _session(store, "prev", "p1", 1, turns=2)
    prov, gw = _gw(tmp_path)
    assert run_job(_deps(store, gw), job_of(info, store.entries("prev"))) == "done"
    assert prov.calls == []
    assert "compaction" not in _types(store, "prev")


def test_pending_watermark_finds_old_sessions(tmp_path):
    from evowiki.assistant.wrapup import _opened
    store = MemorySessionStore()
    old = _session(store, "old", "p1", 1)
    for i in range(8):
        _session(store, f"new{i}", "p2", i+2)
    _, gw = _gw(tmp_path)
    mem = SimpleNamespace(background_allowed=lambda uid: True,
                          anchors=SimpleNamespace(pending=lambda uid: [
        {'session_id': old.session_id, 'revision': 99}]))
    jobs = _opened(_deps(store, gw, mem), store.info('new7'))
    assert [(j.session_id, j.version) for j in jobs] == [('old', 99)]


@pytest.mark.parametrize('budget', [False, True])
def test_failures_are_recorded_and_retryable(tmp_path, monkeypatch, budget):
    from evowiki.assistant import wrapup
    store = MemorySessionStore()
    info = _session(store, "prev", "p1", 1)
    _, gw = _gw(tmp_path)
    def broken(*args):
        if budget:
            raise BudgetExceededError(Account(user_id='u1', group='assistant'), 'day', 1)
        raise ValueError('bad anchor response')
    monkeypatch.setattr(wrapup, '_summarise', broken)
    deps = _deps(store, gw)
    retries = []
    for _ in range(MAX_FAILED):
        job = job_of(info, store.entries('prev'))
        assert run_job(deps, job, lambda j, delay: retries.append(j)) == ('budget' if budget else 'failed')
    assert bool(job_of(info, store.entries('prev'))) == budget
    assert retries and retries[0].attempt == 1
    assert _types(store, 'prev')[-1] == 'wrapup_failed'


def test_a_deleted_session_stops_the_run_without_a_write(tmp_path):
    store = MemorySessionStore()
    prov, gw = _gw(tmp_path)
    job = Job(user_id="u1", session_id="gone", version=3, attempt=0)
    assert run_job(_deps(store, gw), job) == "gone"
    assert prov.calls == []


def test_the_first_message_only_asks(tmp_path):
    """Whatever waits to be wrapped up, the first turn's only model call is its answer, and the
    last session's raw words are in its prompt."""
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="好的"), ChatTurn(text="继续")])
    asked = []
    deps.wrapups = SimpleNamespace(opened=lambda i: asked.append(i.session_id),
                                   compacted=lambda i: None)
    prev = INFO.model_copy(update={"session_id": "prev", "page_id": "p1",
                                   "created_at": T0 - timedelta(hours=1)})
    deps.store.create(prev)
    deps.store.append("prev", "e1", "user", {"message_id": "m0", "text": "刚才讲到 z 分数",
                                             "selection": None}, T0)
    deps.store.append("prev", "e2", "assistant", {"message_id": "a0", "text": "z 分数是标准化后的值",
                                                  "sources": [], "proposals": [], "tool_calls": 0,
                                                  "llm_calls": 1}, T0)
    new = INFO.model_copy(update={"page_id": "p1"})
    list(run_turn(deps, new, MessageIn(text="刚才那段再说一遍")))
    list(run_turn(deps, new, MessageIn(text="还有呢")))
    assert asked == ["s1", "s1", "s1"]                      # the first turn only
    assert len(prov.chat_calls) == 2
    assert "z 分数是标准化后的值" not in prov.chat_calls[0].system
    assert any("z 分数是标准化后的值" in m.text and m.origin == "memory"
               for m in prov.chat_calls[0].messages)
