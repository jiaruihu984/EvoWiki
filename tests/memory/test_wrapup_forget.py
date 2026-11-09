"""Deleting a session races its background wrap-up (assistant/wrapup.py, memory/forget.py). Postgres
for the locks and the requests; the model is scripted, and each race is forced by opening the
delete request while the model call is in flight. DBOS tests at the end run the real queue."""

import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from evowiki.agent import dbos_app
from evowiki.assistant import wrapup
from evowiki.assistant.compact import DEFAULT_POLICY
from evowiki.assistant.session import session_version
from evowiki.assistant.turn_runs import DbosTurnRuns
from evowiki.assistant.wrapup import Job, run_job
from evowiki.memory import forget as forget_mod
from evowiki.memory.anchors import AnchorMemory
from evowiki.memory.lander import Landed, NullRuleGuard
from evowiki.memory.ports import MemoryPorts
from tests.assistant.test_carryover import _gw
from tests.memory.forget_world import T0, build


@pytest.fixture
def w(pool, tmp_path, app_db_url):
    try:
        dbos_app.launch(app_db_url, app_version="wrapup-forget-test")
    finally:
        dbos_app.shutdown()             # the schema stays
    return build(pool, tmp_path)


class Spying:
    """A preference lander that records what lands."""

    def __init__(self):
        self.landed = []

    def land(self, user_id, candidate, *, session_id, **_):
        self.landed.append(session_id)
        return Landed(spec_item_id="si9")


def _deps(w, gw, lander=None):
    ids = iter(range(10_000))
    new_id = lambda p: f"{p}_wf{next(ids)}"
    mem = MemoryPorts(store=w.mem, embedder=None, sessions=w.sessions,
                      lander=lander or Spying(), guard=NullRuleGuard(), new_id=new_id,
                      clock=lambda: T0 + timedelta(days=2),
                      anchors=AnchorMemory(w.pool, w.sessions, w.mem))
    return SimpleNamespace(store=w.sessions, gateway=gw, memory=mem, compaction=DEFAULT_POLICY,
                           data=SimpleNamespace(language=lambda uid: "zh"), new_id=new_id,
                           clock=lambda: T0 + timedelta(days=2))


def _job(w, sid="D"):
    return Job(user_id="u1", session_id=sid, version=session_version(w.sessions.entries(sid)),
               attempt=0)


def _during_the_call(w, sid, text):
    """A model whose reply comes only after the delete was requested."""

    def respond(model, request):
        w.service.request_session("u1", sid, requested_by="test")
        return text
    return respond


def _items(w, sid):
    return w.count("select 1 from memory_item where session_id = %s", sid)


def _compactions(w, sid):
    return w.count("select 1 from assistant_entry where session_id = %s and type = 'compaction'", sid)


def test_a_summary_finished_after_the_delete_was_requested_is_not_stored(w, tmp_path):
    w.sessions.append("D", "D-long", "assistant", {"message_id": "d1-more", "text": "字" * 200,
                                                   "sources": []}, T0 + timedelta(days=1))
    w.sessions.append("D", "D-consolidated", "consolidation", {"through_message_id": "d1"}, T0)
    _, gw = _gw(tmp_path, text=_during_the_call(
        w, "D", '{"statements": [{"text": "他学概率", "sources": ["turn:D/d1"]}]}'))
    assert run_job(_deps(w, gw), _job(w, "D")) in ("postponed", "gone", "done")
    assert _compactions(w, "D") == 0
    assert w.service.run(w.service.open_request("u1", "D")).state == "committed"
    assert w.count("select 1 from assistant_entry where session_id = 'D'") == 0


def test_a_consolidation_finished_after_the_delete_was_requested_writes_nothing(w, tmp_path):
    _, gw = _gw(tmp_path, text=_during_the_call(w, "D", '{"items": [{"kind": "preference", '
                '"content": "先讲例子", "scope_type": "global", "evidence": '
                '[{"message_id": "d1", "role": "user", "quote": "今天学概率"}]}]}'))
    lander = Spying()
    deps = _deps(w, gw, lander)
    state = wrapup.fold(w.sessions.info("D"), w.sessions.entries("D"))
    deps.memory.consolidate(state, gateway=gw, account=None, first=0, through=len(state.turns))
    assert lander.landed == []
    assert _items(w, "D") == 0
    assert w.count("select 1 from assistant_entry where session_id = 'D' "
                   "and type = 'consolidation'") == 0


def test_the_commit_rechecks_what_a_request_hid_after_the_model_call(w):
    """The check under the lock itself: nothing between it and the request."""
    w.service.request_session("u1", "D", requested_by="test")
    assert not w.mem.commit_consolidation(
        w.sessions, session_id="D", entry_id="ent_x", payload={"through_message_id": "d1"},
        items=(), status_changes=(), at=T0)
    assert not w.sessions.append_if_current("D", "ent_y", "compaction", {}, T0, version=99)
    with w.mem.unless_forgotten("u1", "D") as allowed:
        assert not allowed


def test_a_request_waits_for_a_landing_in_flight(w):
    """A preference landing and a delete request serialise: the request comes after the landing,
    so the delete sees what it wrote; a landing after the request does not run."""
    entered, release = threading.Event(), threading.Event()
    order = []

    def land():
        with w.mem.unless_forgotten("u1", "D") as allowed:
            assert allowed
            entered.set()
            release.wait(5)
            order.append("landed")

    def request():
        entered.wait(5)
        w.service.request_session("u1", "D", requested_by="test")
        order.append("requested")

    a, b = threading.Thread(target=land), threading.Thread(target=request)
    a.start()
    b.start()
    entered.wait(5)
    time.sleep(0.3)
    assert order == []
    release.set()
    a.join()
    b.join()
    assert order == ["landed", "requested"]


def test_an_active_wrapup_of_the_user_holds_the_delete_until_it_ends(w, monkeypatch):
    monkeypatch.setattr(forget_mod, "WRAPUP_WAIT_S", 0.0)
    wid = "wrapup:u1:B:3:0"
    with w.pool.connection() as conn:
        conn.execute("insert into dbos.workflow_status (workflow_uuid, status, name, priority) "
                     "values (%s, 'PENDING', 'evowiki.assistant.wrapup', 0)", (wid,))
    out = w.service.forget_session("u1", "D", requested_by="test")
    assert out.state == "failed" and "ForgetBusy" in out.error
    with w.pool.connection() as conn:
        conn.execute("update dbos.workflow_status set status = 'SUCCESS' where workflow_uuid = %s",
                     (wid,))
    w.service.retry_user("u1")
    assert w.service.open_request("u1", "D") is None
    assert w.count("select 1 from dbos.workflow_status where workflow_uuid = %s", wid) == 0


def test_a_failure_record_of_another_session_stays_outside_the_closure(w, tmp_path):
    """A wrap-up that stopped for someone's delete records `postponed` in its own session."""
    w.service.request_session("u1", "A", requested_by="test")
    _, gw = _gw(tmp_path)
    assert run_job(_deps(w, gw), _job(w, "D")) == "postponed"
    [row] = [r for r in w.entries("D") if r["type"] == "wrapup_failed"]
    assert row["payload"]["reason"] == "postponed"
    assert w.service.run(w.service.open_request("u1", "A")).state == "committed"
    assert [r for r in w.entries("D") if r["type"] == "wrapup_failed"] == [row]


# ---- the real queue ----

@pytest.fixture
def queue(w, tmp_path, app_db_url):
    box = {}
    w.sessions.append("D", "D-long", "assistant", {"message_id": "d1-more", "text": "字" * 200,
                                                   "sources": []}, T0 + timedelta(days=1))
    w.sessions.append("D", "D-consolidated", "consolidation", {"through_message_id": "d1"}, T0)
    with w.pool.connection() as conn:
        conn.execute("update dream_pending set processed=revision where session_id<>'D'")
    entered, release = threading.Event(), threading.Event()
    summaries = []

    def respond(model, request):
        summaries.append(request.purpose)
        entered.set()
        release.wait(30)
        if request.purpose == "memory_anchor":
            return '{"statements": [{"text": "他学概率", "sources": ["turn:D/d1"]}]}'
        return '{"items": []}'
    _, gw = _gw(tmp_path, text=respond)
    box["deps"] = _deps(w, gw)
    wrapup.install(lambda: box["deps"], w.service.retry_user)
    dbos_app.launch(app_db_url, app_version="wrapup-forget-test", queues=[wrapup.QUEUE])
    wrapup.register()
    yield SimpleNamespace(deps=box["deps"], entered=entered, release=release, summaries=summaries,
                          wrapups=wrapup.DbosWrapups(lambda: box["deps"]))
    release.set()
    wrapup.uninstall()
    dbos_app.shutdown()


def _wait(pred, s=20.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _workflows(w, sid="D"):
    with w.pool.connection() as conn:
        return forget_mod.dbos_workflows(conn, [], [f"wrapup:u1:{sid}:"])


def test_two_new_sessions_ask_for_one_run_and_the_old_session_stays_free(w, queue):
    new = w.sessions.info("C")
    for _ in range(2):                  # the same ask twice: two sessions starting together
        queue.wrapups.opened(new)
    assert _wait(queue.entered.is_set)
    assert not DbosTurnRuns().busy("D")   # no 上一轮还没结束 for him in the old chat
    w.sessions.append("D", "D-back", "user", {"message_id": "d9", "text": "接着说",
                                              "selection": None}, T0 + timedelta(days=1, hours=1))
    queue.release.set()
    assert _wait(lambda: any(r["status"] == "SUCCESS" for r in _workflows(w)))
    assert sum(r["status"] == "SUCCESS" for r in _workflows(w)) == 1
    assert queue.summaries.count("memory_anchor") == 1
    assert _compactions(w, "D") == 0
    assert w.count("select 1 from dream_summary") == 0  # changed raw revision rejects publication


def test_deleting_the_session_during_its_run_commits_and_writes_nothing_back(w, queue):
    queue.wrapups.opened(w.sessions.info("C"))
    assert _wait(queue.entered.is_set)
    out = {}
    t = threading.Thread(target=lambda: out.update(
        o=w.service.forget_session("u1", "D", requested_by="test")))
    t.start()
    assert _wait(lambda: w.service.open_request("u1", "D") is not None)
    assert t.is_alive()                 # the delete waits for the run
    queue.release.set()
    t.join(30)
    assert out["o"].state == "committed"
    assert w.count("select 1 from assistant_entry where session_id = 'D'") == 0
    assert _items(w, "D") == 0
    assert _workflows(w) == []
