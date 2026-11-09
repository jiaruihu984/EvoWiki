"""D03b: validated chat complaints use the existing durable correction handoff."""

from types import SimpleNamespace

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.assistant.tools.actions import ActionDeps, ReportProblemTool
from evowiki.service import rsi_feedback
from evowiki.wikiapp.repo import PgRepo
from tests.memory.forget_world import T0, build

COMPLAINT = "你总在我说不会以后重复问同一个问题"


@pytest.fixture
def world(pool, tmp_path):
    return build(pool, tmp_path)


def tool_for(world):
    ids = iter(range(10000))
    return ReportProblemTool(
        ActionDeps(
            repo=PgRepo(world.pool),
            ledger=None,
            clock=lambda: T0,
            new_id=lambda p: f"{p}_complaint{next(ids)}",
        )
    )


def report(world):
    return tool_for(world).run(
        {"quote": COMPLAINT, "problem": "重复问已经说不会的内容"},
        ToolContext(
            user_id="u1",
            session_id="A",
            message_id="a1",
            user_words=(COMPLAINT,),
            current_words=(COMPLAINT,),
        ),
    )


def test_validated_report_problem_atomically_records_delivery_and_turn_provenance(world):
    out = report(world)
    assert out.error is None, out.content
    ids = rsi_feedback.pending_ids(SimpleNamespace(pool=world.pool), "u1")
    assert len(ids) == 1
    with world.pool.connection() as conn:
        sources = {
            r["source"]
            for r in conn.execute(
                "select source from memory_dependency where user_id=%s and target=%s",
                ("u1", "signal:" + ids[0]),
            ).fetchall()
        }
    assert {"session:A", "turn:A/a1"} <= sources


def test_replayed_report_is_one_piece_of_evidence(world):
    report(world)
    report(world)
    assert len(rsi_feedback.pending_ids(SimpleNamespace(pool=world.pool), "u1")) == 1


def test_suppression_hides_complaint_from_eligibility_and_signal_api(world):
    from evowiki.memory.forget import suppress_sources
    from evowiki.wikiapp.signals import all_signals, list_signals

    report(world)
    repo = PgRepo(world.pool)
    with world.pool.connection() as conn:
        suppress_sources(conn, "u1", "hide-source", ["turn:A/a1"])
    assert not rsi_feedback.signals(SimpleNamespace(pool=world.pool), "u1")
    assert not all_signals(repo, "u1", kinds=frozenset({"user_complaint"}))
    assert not list_signals(repo, "u1", kinds="user_complaint").items


def test_real_session_forget_purges_complaint_delivery_and_rejects_late_report(world):
    from evowiki.wikiapp.signals import all_signals

    report(world)
    out = world.service.forget_session("u1", "A", requested_by="test")
    assert out.state == "committed"
    assert not rsi_feedback.pending_ids(SimpleNamespace(pool=world.pool), "u1")
    assert not all_signals(PgRepo(world.pool), "u1", kinds=frozenset({"user_complaint"}))
    late = report(world)
    assert late.error is not None and "ForgetBusy" in late.content
    with world.pool.connection() as conn:
        rows = conn.execute(
            "select payload from wiki_event where user_id=%s and type='signal_recorded'", ("u1",)
        ).fetchall()
    assert COMPLAINT not in str(rows)


def test_invalid_quote_records_neither_complaint_nor_delivery(world):
    out = tool_for(world).run(
        {"quote": "不是他说过的话", "problem": "重复问"},
        ToolContext(
            user_id="u1",
            session_id="A",
            message_id="a1",
            user_words=(COMPLAINT,),
            current_words=(COMPLAINT,),
        ),
    )
    assert out.error is not None
    assert not rsi_feedback.pending_ids(SimpleNamespace(pool=world.pool), "u1")


def triage_gateway(pool):
    import json

    from evowiki.llm.gateway import Gateway
    from evowiki.llm.models import GatewayConfig
    from evowiki.llm.scripted import ScriptedProvider
    from evowiki.llm.usage_control import UsageControl
    from evowiki.store.llm_logs import PgAttemptLog, PgCallLog
    from evowiki.store.trace import PgRequestLog
    from tests.harness.test_round import PRICES, REF, _Meter

    served = []

    def answer(model, request):
        assert request.purpose == "rsi.h.triage"
        served.append(request)
        return json.dumps({"largest": 3, "what": "重复问已经说不会的内容"})

    return Gateway(
        GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True),
        PgCallLog(pool),
        mode="record",
        attempts=PgAttemptLog(pool),
        requests=PgRequestLog(pool),
        providers={"scripted": ScriptedProvider("scripted", answer)},
        usage=UsageControl(meter=_Meter(), prices=PRICES),
    ), served


def test_forget_removes_raw_triage_quote_and_its_indexed_calls(world):
    from evowiki.assistant.pg_store import PgSessionStore
    from evowiki.harness.round import worth_review

    for i in range(3):
        mid = f"complaint{i}"
        world.sessions.append(
            "D", f"complaint-entry{i}", "user", {"message_id": mid, "text": COMPLAINT}, T0
        )
        out = tool_for(world).run(
            {"quote": COMPLAINT, "problem": "重复问"},
            ToolContext(
                user_id="u1",
                session_id="D",
                message_id=mid,
                user_words=(COMPLAINT,),
                current_words=(COMPLAINT,),
            ),
        )
        assert out.error is None
    svc = SimpleNamespace(pool=world.pool)
    gateway, served = triage_gateway(world.pool)
    signals = rsi_feedback.signals(svc, "u1")
    assert worth_review(PgSessionStore(world.pool), None, gateway, "u1", None, page_signals=signals)
    assert len(served) == 1
    with world.pool.connection() as conn:
        refs = conn.execute(
            "select target from memory_dependency where user_id=%s and source=%s and target like 'call:%%'",
            ("u1", "signal:" + signals[0].signal_id),
        ).fetchall()
        assert len(refs) == 1
        before = conn.execute("select row_to_json(r) as data from llm_text r").fetchall()
        assert COMPLAINT in str(before)
    assert world.service.forget_session("u1", "D", requested_by="test").state == "committed"
    assert not rsi_feedback.signals(svc, "u1") and not rsi_feedback.pending_ids(svc, "u1")
    with world.pool.connection() as conn:
        for table in ("llm_call", "llm_request", "llm_text", "llm_attempt", "wiki_event"):
            rows = conn.execute(f"select row_to_json(r) as data from {table} r").fetchall()
            assert COMPLAINT not in str(rows), table


@pytest.mark.parametrize("with_deep,expected", [(False, "skipped"), (True, "thin")])
def test_native_chat_tool_immediately_delivers_to_real_dbos_cycle(
    world, services, tmp_path, monkeypatch, with_deep, expected
):
    from dbos import DBOS
    from fastapi.testclient import TestClient

    from evowiki.agent import dbos_app
    from evowiki.assistant.loop import TurnDeps
    from evowiki.assistant.pg_store import PgSessionStore
    from evowiki.assistant.service import AssistantService
    from evowiki.llm.chat import ChatTurn, ToolCall
    from evowiki.llm.gateway import Gateway
    from evowiki.llm.log import CallLog
    from evowiki.llm.scripted import ScriptedChatProvider
    from evowiki.service.app import create_app
    from evowiki.store import harness_rsi
    from evowiki.store.rsi import PgRsiStore
    from evowiki.teach.contracts import RsiSettings
    from tests.assistant.fakes import Data, Ids
    from tests.assistant.test_action_tools import CFG
    from tests.service.test_assistant_api import Applier

    turns = iter(
        [
            turn
            for i in range(3)
            for turn in (
                ChatTurn(
                    tool_calls=(
                        ToolCall(
                            id=f"call{i}",
                            name="report_problem",
                            args={"quote": COMPLAINT, "problem": "重复问"},
                        ),
                    )
                ),
                ChatTurn(text="已记下这个问题。"),
            )
        ]
    )
    chat = Gateway(
        CFG,
        CallLog(tmp_path / "chat.jsonl"),
        mode="record",
        providers={"scripted": ScriptedChatProvider("scripted", lambda *a: next(turns))},
    )
    tool = ReportProblemTool(
        ActionDeps(
            repo=PgRepo(world.pool),
            ledger=None,
            clock=services.clock,
            new_id=services.new_id,
            feedback_notify=rsi_feedback.notify,
        )
    )
    services.gateway, served = triage_gateway(world.pool)
    if with_deep:
        from evowiki.harness import judge
        from evowiki.wikiapp.refine_service import DeepService
        from tests.deep.fakes import DeepScript
        from tests.harness.test_judge import DS

        deep_script = DeepScript()
        monkeypatch.setattr(
            judge, "judge_configs", lambda: judge.JudgeConfigs(deepseek=DS, anthropic=None)
        )
        monkeypatch.setattr(judge, "judge_providers", deep_script.providers)
        services.deep = DeepService(
            repo=PgRepo(world.pool),
            jobs=None,
            assets=None,
            specs=None,
            checker=None,
            llm=deep_script.llm(),
            clock=services.clock,
            new_id=services.new_id,
        )
    services.assistant = AssistantService(
        TurnDeps(
            gateway=chat,
            tools=(tool,),
            store=PgSessionStore(world.pool),
            data=Data(),
            clock=services.clock,
            new_id=Ids(),
        ),
        Applier(),
    )
    PgRsiStore(world.pool).put_settings("u1", RsiSettings(cycle="every_5_feedback", harness=True))
    rsi_feedback.install(services)
    dbos_app.launch(
        services.pool.conninfo,
        app_version="d03b-chat",
        executor_id="d03b-chat",
        queues=[rsi_feedback.QUEUE],
        schedules=False,
    )
    try:
        rsi_feedback.register()
        api = TestClient(create_app(services))
        headers = {"X-EvoWiki-User": "u1"}
        sid = api.post("/assistant/sessions", json={}, headers=headers).json()["session_id"]
        seen = set()
        for _ in range(3):
            response = api.post(
                f"/assistant/sessions/{sid}/messages", json={"text": COMPLAINT}, headers=headers
            )
            assert response.status_code == 200 and "event: done" in response.text, response.text
            workflows = DBOS.list_workflows(workflow_id_prefix="rsi-feedback:u1:")
            new = [w for w in workflows if w.workflow_id not in seen]
            assert new, "report_problem did not enqueue its immediate wake"
            for w in new:
                DBOS.retrieve_workflow(w.workflow_id).get_result()
                seen.add(w.workflow_id)
        assert len(served) == 1
        cycles = PgRsiStore(world.pool).cycles("u1")
        assert len(cycles) == 1 and cycles[0].trigger == "correction"
        with world.pool.connection() as conn:
            native = harness_rsi.last_round(conn, "u1")
            assert native.status == expected, native.reason
        assert len(rsi_feedback.pending_ids(services, "u1")) == (3 if expected == "skipped" else 0)
        if with_deep:
            assert not native.cases["problem"]
            assert len(native.cases["unusable"]) == 3
            assert all("没有指定可重放的回答" in c["reason"] for c in native.cases["unusable"])
    finally:
        rsi_feedback.uninstall()
        dbos_app.shutdown()


def test_forget_between_source_read_and_commit_rolls_back_signal_and_wake(world, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from evowiki.wikiapp.signals import all_signals

    entered, release = threading.Event(), threading.Event()
    repo = PgRepo(world.pool)
    commit = repo.commit

    def delayed(*a, **kw):
        entered.set()
        assert release.wait(5)
        return commit(*a, **kw)

    monkeypatch.setattr(repo, "commit", delayed)
    tool = ReportProblemTool(
        ActionDeps(repo=repo, ledger=None, clock=lambda: T0, new_id=lambda p: p)
    )

    def run():
        return tool.run(
            {"quote": COMPLAINT, "problem": "重复问"},
            ToolContext(
                user_id="u1",
                session_id="A",
                message_id="a1",
                user_words=(COMPLAINT,),
                current_words=(COMPLAINT,),
            ),
        )

    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(run)
        try:
            assert entered.wait(5)
            assert world.service.forget_session("u1", "A", requested_by="test").state == "committed"
        finally:
            release.set()
        assert pending.result(timeout=5).error is not None
    assert not rsi_feedback.pending_ids(SimpleNamespace(pool=world.pool), "u1")
    assert not all_signals(repo, "u1", kinds=frozenset({"user_complaint"}))


def test_memory_publication_lock_rejects_without_waiting_under_wiki_lock(world):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as executor, world.pool.connection() as conn:
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", ("memory:user:u1",))
        pending = executor.submit(report, world)
        out = pending.result(timeout=2)
        assert out.error is not None and "ForgetBusy" in out.content
    assert not rsi_feedback.pending_ids(SimpleNamespace(pool=world.pool), "u1")


def test_multiple_quoted_fragments_of_one_turn_are_one_evidence_unit(world):
    tool = tool_for(world)
    ctx = ToolContext(
        user_id="u1",
        session_id="A",
        message_id="a1",
        user_words=(COMPLAINT,),
        current_words=(COMPLAINT,),
    )
    for quote in (COMPLAINT[:8], COMPLAINT[8:]):
        out = tool.run({"quote": quote, "problem": "重复问"}, ctx)
        assert out.error is None
    assert len(rsi_feedback.pending_ids(SimpleNamespace(pool=world.pool), "u1")) == 1


def test_forget_removes_request_committed_before_call_log_failure(world, monkeypatch):
    from evowiki.assistant.pg_store import PgSessionStore
    from evowiki.harness.round import worth_review

    for i in range(3):
        mid = f"failed-complaint{i}"
        world.sessions.append(
            "D", f"failed-complaint-entry{i}", "user", {"message_id": mid, "text": COMPLAINT}, T0
        )
        out = tool_for(world).run(
            {"quote": COMPLAINT, "problem": "重复问"},
            ToolContext(
                user_id="u1",
                session_id="D",
                message_id=mid,
                user_words=(COMPLAINT,),
                current_words=(COMPLAINT,),
            ),
        )
        assert out.error is None
    gateway, _ = triage_gateway(world.pool)

    def fail(call):
        raise RuntimeError("scripted call log failure after request commit")

    monkeypatch.setattr(gateway._log, "append", fail)
    with pytest.raises(RuntimeError, match="call log failure"):
        worth_review(
            PgSessionStore(world.pool),
            None,
            gateway,
            "u1",
            None,
            page_signals=rsi_feedback.signals(SimpleNamespace(pool=world.pool), "u1"),
            strict=True,
        )
    assert world.service.forget_session("u1", "D", requested_by="test").state == "committed"
    with world.pool.connection() as conn:
        rows = conn.execute("select row_to_json(r) as data from llm_text r").fetchall()
        assert COMPLAINT not in str(rows)


def test_request_log_cannot_publish_quote_after_its_source_was_forgotten(world):
    from evowiki.memory.forget import ForgetBusy
    from evowiki.store.trace import PgRequestLog

    assert world.service.forget_session("u1", "D", requested_by="test").state == "committed"
    with pytest.raises(ForgetBusy):
        PgRequestLog(world.pool).record(
            "late-private-request",
            "SYS",
            COMPLAINT,
            "TriageOut",
            source_user_id="u1",
            source_nodes=["session:D"],
        )
    with world.pool.connection() as conn:
        assert (
            conn.execute(
                "select 1 from llm_request where call_id='late-private-request'"
            ).fetchone()
            is None
        )
        texts = conn.execute("select row_to_json(r) as data from llm_text r").fetchall()
        assert COMPLAINT not in str(texts)
