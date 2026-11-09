"""A reading-assistant turn as a DBOS workflow (assistant/turn_runs.py): the codec, the inline
runner, durable_turn_deps' carried findings, and - on Postgres - the same events and session log
as the inline turn, the busy rules, a follower that leaves, and replay from recorded steps."""

import threading
import time

import pytest

from evowiki.assistant import turn_runs
from evowiki.assistant.contracts import Done, ErrorEv, MessageIn, TextDelta
from evowiki.assistant.durable import durable_turn_deps
from evowiki.assistant.loop import TurnDeps
from evowiki.assistant.service import AssistantService, SessionBusy
from evowiki.assistant.session import MemorySessionStore
from evowiki.assistant.turn_runs import (
    DbosTurnRuns,
    InlineTurnRuns,
    TurnJob,
    decode_event,
    encode_event,
)
from evowiki.llm.chat import ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.scripted import ScriptedChatProvider
from tests.assistant.fakes import INFO, T0, Data, Ids, tools
from tests.assistant.test_loop import CFG, call

SCRIPT = [ChatTurn(tool_calls=(call("read_k", 1), call("propose_page_edit", 2))),
          ChatTurn(text="我建议加一段。" + "答" * 100)]
MSG = MessageIn(text="问")


class Applier:
    def apply(self, user_id, draft):
        return ("s9",)


# ---- codec ----

def test_event_codec_round_trips_every_event_type():
    for ev in (TextDelta(text="x"), Done(message_id="m", tool_calls=1, llm_calls=2),
               ErrorEv(code="internal", retryable=True, detail="d")):
        assert decode_event(encode_event(ev)) == ev


@pytest.mark.parametrize("bad", [{"type": "nope"}, {"type": "done"}, "x", None, {}])
def test_decode_event_refuses_anything_invalid_with_value_error(bad):
    with pytest.raises(ValueError):
        decode_event(bad)


def test_job_workflow_id_is_session_and_message():
    job = TurnJob(session_id="s1", user_id="u1", message_id="msg_001", msg=MSG)
    assert job.workflow_id == "turn:s1:msg_001"
    assert TurnJob.model_validate(job.model_dump(mode="json")) == job


def test_installed_fails_closed_without_install(monkeypatch):
    monkeypatch.setattr(turn_runs, "_service", None)
    with pytest.raises(RuntimeError, match="install"):
        turn_runs.installed()


# ---- durable_turn_deps findings ----

def build(tmp_path, script, *, blocker=None, counter=None, ts=None):
    outs = iter(script)

    def answer(m, r):
        if counter is not None:
            counter.append(1)
        if blocker is not None:
            assert blocker.wait(30), "the fake model was never released"
        out = next(outs)
        if isinstance(out, BaseException):
            raise out
        return out

    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                 providers={"scripted": ScriptedChatProvider("scripted", answer)})
    store = MemorySessionStore()
    store.create(INFO)
    return TurnDeps(gateway=gw, tools=ts if ts is not None else tools(), store=store,
                    data=Data(), new_id=Ids(),
                    clock=lambda: T0)


def test_a_tools_factory_without_durable_is_refused(tmp_path):
    deps = build(tmp_path, SCRIPT)
    deps.tools = lambda info, now: []
    with pytest.raises(TypeError, match="durable"):
        durable_turn_deps(deps)


def test_a_fixed_tools_list_is_kept(tmp_path):
    deps = build(tmp_path, SCRIPT)
    assert durable_turn_deps(deps).tools is deps.tools


def test_a_tools_factory_with_durable_is_swapped_for_its_copy(tmp_path):
    class Factory:
        def __call__(self, info, now):
            return []

        def durable(self):
            return Copy()

    class Copy(Factory):
        pass

    deps = build(tmp_path, SCRIPT)
    deps.tools = Factory()
    assert isinstance(durable_turn_deps(deps).tools, Copy)


def test_durable_turn_deps_is_idempotent(tmp_path):
    class Factory:
        n = 0

        def __call__(self, info, now):
            return []

        def durable(self):
            Factory.n += 1
            return Factory()

    deps = build(tmp_path, SCRIPT)
    deps.tools = Factory()
    once = durable_turn_deps(deps)
    twice = durable_turn_deps(once)
    assert Factory.n == 1
    for field in ("store", "data", "tools", "new_id", "clock"):
        assert getattr(twice, field) is getattr(once, field), field


# ---- inline runner ----

def test_inline_runner_is_todays_generator_and_never_busy(tmp_path):
    deps = build(tmp_path, SCRIPT)
    svc = AssistantService(deps, Applier())
    assert isinstance(svc._runs, InlineTurnRuns) and not svc._runs.busy("s1")
    types = [e.type for e in svc.send("u1", "s1", MSG)]
    assert types[-1:] == ["done"]


def test_execute_turns_a_crash_into_one_internal_error_event(tmp_path):
    deps = build(tmp_path, SCRIPT)
    job = TurnJob(session_id="nope", user_id="u1", message_id="m1", msg=MSG)
    out = []
    assert turn_runs.execute(job, deps, out.append) == "error"
    assert [e.type for e in out] == ["error"] and out[0].code == "internal"


# ---- DBOS (Postgres) ----

@pytest.fixture
def dbos_env(db_url, tmp_path):
    from evowiki.agent import dbos_app

    made = {}

    def make(script, **kw):
        deps = build(tmp_path, script, **kw)
        svc = AssistantService(deps, Applier(), DbosTurnRuns())
        turn_runs.install(svc)
        made["svc"] = svc
        return svc

    dbos_app.launch(db_url, app_version="t")
    yield make
    dbos_app.shutdown()
    turn_runs.uninstall()


def entries(svc, sid="s1"):
    return [(e.type, e.entry_id) for e in svc.turn_deps.store.entries(sid)]


def wait_until(pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def workflow_id(svc):
    from dbos import DBOS
    ws = DBOS.list_workflows(workflow_id_prefix="turn:s1:", load_input=False, load_output=False)
    assert len(ws) == 1
    return ws[0].workflow_id


def test_dbos_turn_streams_the_inline_events_and_logs_the_same_session(dbos_env, tmp_path):
    from dbos import DBOS

    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    inline = AssistantService(build(plain_dir, SCRIPT), Applier())
    want = [e.model_dump(mode="json") for e in inline.send("u1", "s1", MSG)]
    want_log = [(e.type, e.payload) for e in inline.turn_deps.store.entries("s1")]

    svc = dbos_env(SCRIPT)
    got = [e.model_dump(mode="json") for e in svc.send("u1", "s1", MSG)]
    assert [e["type"] for e in got] == [e["type"] for e in want] and got[-1]["type"] == "done"
    wid = workflow_id(svc)
    wait_until(lambda: DBOS.retrieve_workflow(wid).get_status().status == "SUCCESS")
    assert DBOS.retrieve_workflow(wid).get_result() == "done"
    got_log = [(e.type, e.payload) for e in svc.turn_deps.store.entries("s1")]
    assert [t for t, _ in got_log] == [t for t, _ in want_log]
    steps = [s["function_name"] for s in DBOS.list_workflow_steps(wid)]
    assert "llm.converse:assistant.turn" in steps and "assistant.append" in steps
    assert steps.count("llm.converse:assistant.turn") == 2


def test_dbos_second_send_is_busy_while_the_turn_is_pending(dbos_env):
    from dbos import DBOS

    gate = threading.Event()
    svc = dbos_env([ChatTurn(text="答" * 100)], blocker=gate)
    svc.send("u1", "s1", MSG).close()            # the follower leaves; the turn goes on
    try:
        with pytest.raises(SessionBusy):
            svc.send("u1", "s1", MSG)
    finally:
        gate.set()
    wid = workflow_id(svc)
    wait_until(lambda: DBOS.retrieve_workflow(wid).get_status().status == "SUCCESS")
    # the turn finished while nobody followed: its answer is in the log
    assert "assistant" in [t for t, _ in entries(svc)]


def test_dbos_a_pending_turn_makes_its_user_busy_not_another(dbos_env):
    """The Bot's idle page writing waits for the learner's running turn (`user_busy`)."""
    from dbos import DBOS

    gate = threading.Event()
    svc = dbos_env([ChatTurn(text="答" * 100)], blocker=gate)
    assert svc.user_busy("u1") is False
    svc.send("u1", "s1", MSG).close()
    try:
        assert svc.user_busy("u1") is True and svc.user_busy("u2") is False
    finally:
        gate.set()
    wid = workflow_id(svc)
    wait_until(lambda: DBOS.retrieve_workflow(wid).get_status().status == "SUCCESS")
    assert svc.user_busy("u1") is False


def test_inline_turns_never_make_a_user_busy(tmp_path):
    svc = AssistantService(build(tmp_path, [ChatTurn(text="好")]), Applier())
    assert svc.user_busy("u1") is False


def test_dbos_decide_is_busy_while_a_turn_is_pending(dbos_env):
    from dbos import DBOS

    gate = threading.Event()
    svc = dbos_env([ChatTurn(text="答" * 100)], blocker=gate)
    store = svc.turn_deps.store
    store.append("s1", "e_p", "proposal", {"proposal_id": "pr1", "message_id": "m0", "draft": {
        "kind": "add_section", "page_id": "p1", "heading": "补充", "text": "正文", "why": "x"}}, T0)
    svc.send("u1", "s1", MSG).close()
    try:
        with pytest.raises(SessionBusy):
            svc.decide("u1", "pr1", False)
    finally:
        gate.set()
    wid = workflow_id(svc)
    wait_until(lambda: DBOS.retrieve_workflow(wid).get_status().status == "SUCCESS")
    assert svc.decide("u1", "pr1", False).status == "rejected"


def test_dbos_a_follower_that_stops_early_does_not_cancel_the_turn(dbos_env):
    from dbos import DBOS

    svc = dbos_env(SCRIPT)
    it = svc.send("u1", "s1", MSG)
    next(it)                                     # one event, then the client goes away
    it.close()
    wid = workflow_id(svc)
    wait_until(lambda: DBOS.retrieve_workflow(wid).get_status().status == "SUCCESS")
    types = [t for t, _ in entries(svc)]
    assert types[0] == "user" and "assistant" in types and "turn_error" not in types
    # a later follower still reads the whole stream
    assert [e.type for e in DbosTurnRuns._follow(wid)][-1] == "done"


def test_dbos_start_fails_closed_when_dbos_is_not_launched(tmp_path):
    svc = AssistantService(build(tmp_path, SCRIPT), Applier(), DbosTurnRuns())
    with pytest.raises(RuntimeError, match="not launched"):
        svc.send("u1", "s1", MSG)
    # the in-process lock was released
    with pytest.raises(RuntimeError, match="not launched"):
        svc.send("u1", "s1", MSG)


def test_dbos_replay_from_recorded_steps_does_not_rerun_them_or_duplicate_the_log(
        dbos_env):
    from dbos import DBOS

    calls: list[int] = []
    ts = tools()
    svc = dbos_env(SCRIPT, counter=calls, ts=ts)
    assert [e.type for e in svc.send("u1", "s1", MSG)][-1] == "done"
    wid = workflow_id(svc)
    wait_until(lambda: DBOS.retrieve_workflow(wid).get_status().status == "SUCCESS")
    steps = DBOS.list_workflow_steps(wid)
    names = [s["function_name"] for s in steps]
    # re-execute from the turn's last log append (the question-as-evidence entry): every step
    # before it - the model calls, the tool runs, the earlier appends, the ids
    # and clock reads - is replayed from its record, only that append runs again
    redo = max(i for i, n in enumerate(names) if n == "assistant.append")
    assert names.index("llm.converse:assistant.turn") < redo
    before_calls, before_log = len(calls), entries(svc)
    before_tool_runs = [len(t.calls) for t in ts]
    forked = DBOS.fork_workflow(wid, steps[redo]["function_id"])
    assert forked.get_result() == "done"
    assert len(calls) == before_calls, "a recorded model call ran again"
    assert [len(t.calls) for t in ts] == before_tool_runs, "a recorded tool step ran again"
    assert entries(svc) == before_log, "the re-run appends duplicated session entries"


def test_dbos_start_called_directly_fails_closed_when_not_launched():
    job = TurnJob(session_id="s1", user_id="u1", message_id="m1", msg=MSG)
    with pytest.raises(turn_runs.AssistantUnavailable, match="not launched"):
        DbosTurnRuns().start(job)
    with pytest.raises(turn_runs.AssistantUnavailable, match="not launched"):
        DbosTurnRuns().busy("s1")


def test_dbos_start_refuses_a_service_that_is_not_the_installed_one(dbos_env, tmp_path):
    svc = dbos_env(SCRIPT)                              # installed and bound
    other = AssistantService(build(tmp_path, SCRIPT), Applier(), DbosTurnRuns())
    with pytest.raises(RuntimeError, match="not the one sending"):
        other.send("u1", "s1", MSG)
    # and a runner nobody bound refuses too
    job = TurnJob(session_id="s1", user_id="u1", message_id="m1", msg=MSG)
    with pytest.raises(RuntimeError, match="not the one sending"):
        DbosTurnRuns().start(job)
    assert [e.type for e in svc.send("u1", "s1", MSG)][-1] == "done"     # the installed one works


def test_dbos_back_to_back_sends_are_never_spuriously_busy(dbos_env):
    """The follower returns only after the workflow left PENDING (was 4/20 spurious 409s)."""
    svc = dbos_env([ChatTurn(text="答" * 100) for _ in range(10)])
    for _ in range(10):
        assert [e.type for e in svc.send("u1", "s1", MSG)][-1] == "done"     # no wait in between


def test_execute_reraises_dbos_errors_instead_of_masking_them(tmp_path):
    from dbos._error import DBOSUnexpectedStepError

    deps = build(tmp_path, SCRIPT)

    class Boom:
        def info(self, sid):
            raise DBOSUnexpectedStepError("wf", 1, "a", "b")
    deps.store = Boom()
    job = TurnJob(session_id="s1", user_id="u1", message_id="m1", msg=MSG)
    out = []
    with pytest.raises(DBOSUnexpectedStepError):
        turn_runs.execute(job, deps, out.append)
    assert out == []


def test_dbos_a_pending_turn_of_another_app_version_is_not_busy_and_is_swept(dbos_env, db_url):
    import psycopg
    from dbos import DBOS

    svc = dbos_env(SCRIPT)
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute(
            "insert into dbos.workflow_status (workflow_uuid, status, name, application_version, "
            "created_at, updated_at) values ('turn:s1:old', 'PENDING', 'evowiki.assistant_turn', "
            "'other-version', 0, 0)")
    assert not svc._runs.busy("s1")
    assert [e.type for e in svc.send("u1", "s1", MSG)][-1] == "done"
    assert turn_runs.sweep_foreign_turns() == 1
    assert DBOS.retrieve_workflow("turn:s1:old").get_status().status == "CANCELLED"
    assert turn_runs.sweep_foreign_turns() == 0


def test_dbos_replay_with_attachments_follows_the_recorded_path(dbos_env):
    """A turn with an attachment, replayed after the material was deleted and the routing
    changed: the attachment rows and `sees_images` come from the record, so the replay takes the
    first run's path (no new model call, the same log) instead of failing on the deletion."""
    from dbos import DBOS

    from evowiki.assistant.attachments import MemoryAttachmentReader
    from tests.assistant.attach_fakes import doc

    calls: list[int] = []
    svc = dbos_env([ChatTurn(text="讲的是向量。")], counter=calls)
    data = {"u1": {"d": (doc("d", ["向量有方向和大小。"]), None)}}
    route = {"sees": False}
    deps = svc.turn_deps
    deps.attachments = MemoryAttachmentReader(data)
    deps.sees_images = lambda acct: route["sees"]
    msg = MessageIn(text="看看", attachments=("d",))
    assert [e.type for e in svc.send("u1", "s1", msg)][-1] == "done"
    wid = workflow_id(svc)
    wait_until(lambda: DBOS.retrieve_workflow(wid).get_status().status == "SUCCESS")
    steps = DBOS.list_workflow_steps(wid)
    names = [s["function_name"] for s in steps]
    for want in ("assistant.attachments.load", "assistant.sees_images", "assistant.route"):
        assert want in names, (want, names)
    redo = max(i for i, n in enumerate(names) if n == "assistant.append")
    before_calls, before_log = len(calls), entries(svc)
    data["u1"]["d"] = (doc("d", ["向量有方向和大小。"], deleted=True), None)   # gone since
    route["sees"] = True                                                    # tier changed
    forked = DBOS.fork_workflow(wid, steps[redo]["function_id"])
    assert forked.get_result() == "done"
    assert len(calls) == before_calls, "a recorded model call ran again"
    assert entries(svc) == before_log


class _Stream:
    """DBOS.read_stream: a scripted read per call; an Exception in the script is raised."""

    def __init__(self, *reads):
        self.reads, self.offsets = list(reads), []

    def __call__(self, wid, key, *, offset=0, **_):
        self.offsets.append(offset)
        read = self.reads.pop(0)
        for x in read:
            if isinstance(x, Exception):
                raise x
            yield x


def _follow(monkeypatch, stream):
    from dbos import DBOS

    from evowiki.assistant.turn_runs import Follower, encode_event

    monkeypatch.setattr(DBOS, "read_stream", stream)
    monkeypatch.setattr("threading.Event.wait", lambda self, t=None: self.is_set())
    monkeypatch.setattr(turn_runs, "_await_settled", lambda wid: None)
    done = encode_event(Done(message_id="m", tool_calls=0, llm_calls=0))
    return Follower("turn:s:m"), done


def test_a_full_connection_pool_does_not_end_the_turn_for_its_reader(monkeypatch):
    """Live under load (evalset past-turn-verbatim): the turn answered, its follower said error."""
    busy = RuntimeError("QueuePool limit of size 20 overflow 0 reached")
    stream = _Stream([busy], [busy])
    f, done = _follow(monkeypatch, stream)
    stream.reads.append([done])
    assert [e.type for e in f] == ["done"]
    assert stream.offsets == [0, 0, 0]          # nothing lost, nothing read twice


def test_a_reader_that_keeps_failing_says_so(monkeypatch):
    from evowiki.assistant.turn_runs import READ_RETRIES

    busy = RuntimeError("down")
    f, _ = _follow(monkeypatch, _Stream(*([busy],) * (READ_RETRIES + 1)))
    got = list(f)
    assert [e.type for e in got] == ["error"] and got[0].detail == "turn ended without done"
