from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from evowiki.harness import round as rounds
from evowiki.harness.triage import TriageOut
from evowiki.service import routes_wiki, rsi_feedback, rsi_runs
from evowiki.store import harness_rsi
from evowiki.store.rsi import PgRsiStore
from evowiki.teach.contracts import PageFeedbackIn, RsiSettings, Signal
from evowiki.wikiapp.feedback import page_feedback_flow
from evowiki.wikiapp.repo import PgRepo
from tests.service.test_wiki_api import new_page
from tests.wikiapp.script import Script

NOW = datetime(2026, 11, 8, tzinfo=UTC)


class Gateway:
    def __init__(self, largest=3):
        self.prompts, self.largest = [], largest

    def call(self, role, req, schema, *, account):
        self.prompts.append(req.prompt)
        return TriageOut(largest=self.largest), None


def finished_round(svc, status, now):
    with svc.pool.connection() as conn:
        rid = harness_rsi.start_round(conn, "u1", finding=None, now=now)
        harness_rsi.finish_round(conn, rid, status, reason="scripted", now=now)
    return SimpleNamespace(b="User-facing text is not a protocol")


def test_page_corrections_reach_light_review_with_provenance(monkeypatch):
    monkeypatch.setattr(rounds, "assistant_feedback", lambda *a: ())
    signals = [
        Signal(
            signal_id=f"s{i}",
            kind="page_feedback",
            at=NOW,
            page_id=f"p{i}",
            section_id="intro",
            chips=("no_example",),
            text="请先给具体例子",
        )
        for i in range(3)
    ]
    gateway = Gateway()
    assert rounds.worth_review(None, None, gateway, "u", None, page_signals=signals)
    assert len(gateway.prompts) == 1
    for signal in signals:
        assert signal.signal_id in gateway.prompts[0]
        assert signal.page_id in gateway.prompts[0]
        assert signal.text in gateway.prompts[0]


def test_page_feedback_wakes_tick_after_recording(monkeypatch):
    monkeypatch.setattr(routes_wiki, "PgRepo", lambda pool: None)
    monkeypatch.setattr(routes_wiki, "_optional_gateway", lambda *a: None)
    monkeypatch.setattr(routes_wiki, "page_feedback_loop", lambda *a, **kw: "recorded")
    svc = SimpleNamespace(deep=None, pool=None, clock=lambda: NOW, new_id=None, bot_notify=None)
    tasks = BackgroundTasks()
    out = routes_wiki.post_page_feedback("p", PageFeedbackIn(text="请先给例子"), svc, "u", tasks)
    assert out == "recorded"
    assert [task.func for task in tasks.tasks] == [routes_wiki.rsi_tick]


@pytest.fixture
def corrections(client, services, monkeypatch):
    script = Script()
    services.gateway = script.gateway()
    pid = new_page((client, script))
    gateway = Gateway(largest=2)
    services.gateway = gateway
    PgRsiStore(services.pool).put_settings(
        "u1", RsiSettings(cycle="every_5_feedback", harness=True)
    )
    monkeypatch.setattr(rsi_runs, "_due_now", lambda *a: False)

    def record(text="请先给具体例子", apply=True):
        return page_feedback_flow(
            PgRepo(services.pool),
            "u1",
            pid,
            PageFeedbackIn(text=text, chips=("no_example",), apply=apply),
            now=services.clock(),
            new_id=services.new_id,
        ).signal_id

    return services, gateway, record


def test_pending_feedback_keeps_opt_in_and_consumes_only_after_review(corrections):
    svc, gw, record = corrections
    opted_out = record(apply=False)
    blank = record(text="")
    ids = [record() for _ in range(3)]
    assert rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    assert opted_out not in [s.signal_id for s in rsi_feedback.signals(svc, "u1")]
    assert blank not in [s.signal_id for s in rsi_feedback.signals(svc, "u1")]
    rsi_runs.rsi_tick(svc, "u1")
    assert len(gw.prompts) == 1
    assert not rsi_feedback.pending_ids(svc, "u1")
    rsi_runs.rsi_tick(svc, "u1")
    assert len(gw.prompts) == 1
    record()
    rsi_runs.rsi_tick(svc, "u1")
    assert len(gw.prompts) == 2 and "4." in gw.prompts[-1]


@pytest.mark.parametrize(
    "settings",
    [
        RsiSettings(cycle="manual", harness=True),
        RsiSettings(cycle="every_5_feedback", harness=False),
    ],
)
def test_disabled_feedback_is_deferred_without_paid_triage(corrections, settings):
    svc, gw, record = corrections
    ids = [record() for _ in range(3)]
    PgRsiStore(svc.pool).put_settings("u1", settings)
    rsi_runs.rsi_tick(svc, "u1")
    assert not gw.prompts and rsi_feedback.pending_ids(svc, "u1") == sorted(ids)


def test_busy_and_provider_failure_keep_delivery_pending(corrections):
    svc, gw, record = corrections
    ids = [record() for _ in range(3)]
    with rsi_runs.cycle_lock(svc, "u1") as got:
        assert got
        rsi_runs.rsi_tick(svc, "u1")
    assert not gw.prompts and rsi_feedback.pending_ids(svc, "u1") == sorted(ids)

    def fail(*a, **kw):
        raise RuntimeError("scripted provider failure")

    original = gw.call
    gw.call = fail
    rsi_runs.rsi_tick(svc, "u1")
    assert rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    gw.call = original
    rsi_runs.rsi_tick(svc, "u1")
    assert len(gw.prompts) == 1 and not rsi_feedback.pending_ids(svc, "u1")


def test_below_threshold_costs_nothing_and_positive_triage_runs_once(corrections, monkeypatch):
    svc, gw, record = corrections
    record()
    record()
    rsi_runs.rsi_tick(svc, "u1")
    assert not gw.prompts
    record()
    gw.largest = 3
    runs = []

    class Cycle:
        def run(self, *a, **kw):
            runs.append(kw["trigger"])
            return finished_round(svc, "no_change", kw["now"])

    monkeypatch.setattr(rsi_runs, "_cycle", lambda *a: Cycle())
    monkeypatch.setattr(rsi_runs, "_data", lambda *a: None)
    rsi_runs.rsi_tick(svc, "u1")
    rsi_runs.rsi_tick(svc, "u1")
    assert runs == ["correction"] and len(gw.prompts) == 1


def test_actual_dbos_restart_delivers_feedback_once(corrections, db_url, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    svc, _gw, record = corrections
    ids = [record() for _ in range(3)]
    log = tmp_path / "rsi-feedback.log"
    root = Path(__file__).resolve().parents[2]

    def child(mode):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.service.rsi_feedback_crash_child",
                mode,
                db_url,
                str(log),
            ],
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root / "src")},
            timeout=120,
            check=False,
        ).returncode

    assert child("crash") == 1
    assert rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    assert child("resume") == 0
    assert log.read_text(encoding="utf-8").splitlines() == [
        "crash before triage",
        "triage",
        "delivered",
    ]
    assert not rsi_feedback.pending_ids(svc, "u1")


def test_weekly_cap_defers_until_next_week(corrections):
    from datetime import timedelta

    svc, gw, record = corrections
    store = PgRsiStore(svc.pool)
    store.put_settings("u1", RsiSettings(cycle="weekly", harness=True))
    last = svc.clock() - timedelta(days=1)
    with svc.pool.connection() as conn:
        rid = harness_rsi.start_round(conn, "u1", finding=None, now=last)
        harness_rsi.finish_round(conn, rid, "no_change", reason="scripted", now=last)
    ids = [record() for _ in range(3)]
    rsi_runs.rsi_tick(svc, "u1")
    assert not gw.prompts and rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    svc.clock = lambda: last + timedelta(days=8)
    rsi_runs.rsi_tick(svc, "u1")
    assert len(gw.prompts) == 1 and not rsi_feedback.pending_ids(svc, "u1")


def test_suppressed_signal_is_not_selected(corrections):
    from evowiki.memory.forget import suppress_sources

    svc, gw, record = corrections
    ids = [record() for _ in range(3)]
    with svc.pool.connection() as conn:
        suppress_sources(conn, "u1", "scripted-forget", ["signal:" + ids[0]])
    rsi_runs.rsi_tick(svc, "u1")
    assert not gw.prompts
    assert ids[0] not in [s.signal_id for s in rsi_feedback.signals(svc, "u1")]


def test_duplicate_signal_ids_are_one_case(monkeypatch):
    monkeypatch.setattr(rounds, "assistant_feedback", lambda *a: ())
    signal = Signal(signal_id="same", kind="page_feedback", at=NOW, page_id="p", text="请给例子")
    gw = Gateway()
    assert not rounds.worth_review(None, None, gw, "u", None, page_signals=[signal] * 3)
    assert not gw.prompts


def test_three_committed_page_corrections_reach_service_triage(corrections):
    svc, gw, record = corrections
    for _ in range(3):
        record()
    gw.largest = 3
    assert rsi_runs._corrected(svc, "u1")
    assert len(gw.prompts) == 1


def test_feedback_http_entry_commits_then_wakes(client, services, monkeypatch):
    from tests.service.conftest import H

    script = Script()
    services.gateway = script.gateway()
    pid = new_page((client, script))
    services.gateway = None
    observed = []

    def tick(svc, uid):
        observed.extend(rsi_feedback.pending_ids(svc, uid))

    monkeypatch.setattr(routes_wiki, "rsi_tick", tick)
    out = client.post(
        f"/wiki/pages/{pid}/feedback",
        headers=H("u1"),
        json={"text": "请先给具体例子", "apply": True},
    )
    assert out.status_code == 201, out.text
    assert observed == [out.json()["signal_id"]]


def test_positive_triage_is_reused_after_round_failure(corrections, monkeypatch):
    from datetime import timedelta

    svc, gw, record = corrections
    ids = [record() for _ in range(3)]
    gw.largest = 3
    outcomes = iter(["error", "error", "no_change"])
    at = svc.clock() + timedelta(seconds=1)
    svc.clock = lambda: at

    class Cycle:
        def run(self, *a, **kw):
            return finished_round(svc, next(outcomes), kw["now"])

    monkeypatch.setattr(rsi_runs, "_cycle", lambda *a: Cycle())
    monkeypatch.setattr(rsi_runs, "_data", lambda *a: None)
    rsi_runs.rsi_tick(svc, "u1")
    assert rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    rsi_runs.rsi_tick(svc, "u1")
    assert rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    rsi_runs.rsi_tick(svc, "u1")
    assert len(gw.prompts) == 1 and not rsi_feedback.pending_ids(svc, "u1")


def test_ordinary_harness_round_does_not_reintroduce_opted_out_feedback(corrections):
    svc, _gw, record = corrections
    opted_out = record(apply=False)
    eligible = record()
    rows = rsi_runs._harness(svc).deps.signals("u1", None)
    ids = {s.signal_id for s in rows}
    assert eligible in ids and opted_out not in ids


def test_changed_rating_words_invalidate_positive_triage_cache(corrections, monkeypatch):
    from evowiki.wikiapp import signals as signal_source

    svc, gw, record = corrections
    record()
    record()
    rating = [
        Signal(
            signal_id="stable-rating", kind="assistant_rated", at=svc.clock(), text="补充：例子太少"
        )
    ]
    feedback = lambda *a: [(rating[0], "assistant:s1:m1")]
    monkeypatch.setattr(signal_source, "assistant_feedback", feedback)
    monkeypatch.setattr(rounds, "assistant_feedback", feedback)
    monkeypatch.setattr(
        rsi_runs, "PgSessionStore", lambda pool: SimpleNamespace(entries=lambda sid: [])
    )
    gw.largest = 3
    assert rsi_runs._corrected(svc, "u1")
    assert rsi_runs._corrected(svc, "u1") and len(gw.prompts) == 1
    rating[0] = rating[0].model_copy(update={"text": "补充：术语没讲清楚"})
    assert rsi_runs._corrected(svc, "u1") and len(gw.prompts) == 2


def test_drained_process_defers_http_tick_and_sweep(corrections, monkeypatch):
    from evowiki.agent import lifecycle

    svc, gw, record = corrections
    ids = [record() for _ in range(3)]
    enqueued = []
    monkeypatch.setattr(rsi_feedback, "_svc", svc)
    monkeypatch.setattr(
        rsi_feedback, "_queue", SimpleNamespace(enqueue=lambda *a: enqueued.append(a))
    )
    lifecycle.DRAINED.set()
    try:
        rsi_runs.rsi_tick(svc, "u1")
        rsi_feedback.wake_pending()
        assert not gw.prompts and not enqueued
        assert rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    finally:
        lifecycle.DRAINED.clear()
    rsi_runs.rsi_tick(svc, "u1")
    assert len(gw.prompts) == 1 and not rsi_feedback.pending_ids(svc, "u1")


def test_drain_during_triage_defers_full_round_without_repaying_triage(corrections, monkeypatch):
    from evowiki.agent import lifecycle

    svc, gw, record = corrections
    ids = [record() for _ in range(3)]
    original = gw.call
    gw.largest = 3

    def triage(*a, **kw):
        lifecycle.DRAINED.set()
        return original(*a, **kw)

    gw.call = triage
    runs = []

    class Cycle:
        def run(self, *a, **kw):
            runs.append(kw["trigger"])
            return finished_round(svc, "no_change", kw["now"])

    monkeypatch.setattr(rsi_runs, "_cycle", lambda *a: Cycle())
    monkeypatch.setattr(rsi_runs, "_data", lambda *a: None)
    try:
        rsi_runs.rsi_tick(svc, "u1")
        assert not runs and rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    finally:
        lifecycle.DRAINED.clear()
    gw.call = original
    rsi_runs.rsi_tick(svc, "u1")
    assert runs == ["correction"] and len(gw.prompts) == 1


def test_later_assistant_correction_joins_previously_reviewed_page_signals(
    corrections, monkeypatch
):
    from evowiki.wikiapp import signals as signal_source

    svc, gw, record = corrections
    ids = [record(), record()]
    rsi_runs.rsi_tick(svc, "u1")
    assert not gw.prompts and not rsi_feedback.pending_ids(svc, "u1")
    rating = Signal(
        signal_id="new-rating", kind="assistant_rated", at=svc.clock(), text="补充：请先给具体例子"
    )
    feedback = lambda *a: [(rating, "assistant:s1:m1")]
    monkeypatch.setattr(signal_source, "assistant_feedback", feedback)
    monkeypatch.setattr(rounds, "assistant_feedback", feedback)
    monkeypatch.setattr(
        rsi_runs, "PgSessionStore", lambda pool: SimpleNamespace(entries=lambda sid: [])
    )
    gw.largest = 3
    outcomes = iter(["error", "no_change"])
    runs = []

    class Cycle:
        def run(self, *a, **kw):
            status = next(outcomes)
            runs.append(status)
            return finished_round(svc, status, kw["now"])

    monkeypatch.setattr(rsi_runs, "_cycle", lambda *a: Cycle())
    monkeypatch.setattr(rsi_runs, "_data", lambda *a: None)
    rsi_runs.rsi_tick(svc, "u1")
    assert rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    rsi_runs.rsi_tick(svc, "u1")
    rsi_runs.rsi_tick(svc, "u1")
    assert runs == ["error", "no_change"]
    assert not rsi_feedback.pending_ids(svc, "u1")
    assert len(gw.prompts) == 1
    assert all(signal_id in gw.prompts[0] for signal_id in ids)


def test_bot_on_to_off_retains_pending_bot_work_and_keeps_rsi_scheduler(db_url):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]

    def child(mode):
        return subprocess.run(
            [sys.executable, "-m", "tests.service.rsi_bot_switch_child", mode, db_url],
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root / "src")},
            timeout=120,
            check=False,
        ).returncode

    assert child("on") == 1
    assert child("off") == 0


@pytest.mark.parametrize("status", [None, "skipped"])
def test_missing_or_skipped_native_round_never_acknowledges_feedback(
    corrections, monkeypatch, status
):
    svc, gw, record = corrections
    ids = [record() for _ in range(3)]
    gw.largest = 3

    class Cycle:
        def run(self, *a, **kw):
            if status:
                return finished_round(svc, status, kw["now"])
            return SimpleNamespace(b="harness applied misleading-summary")

    monkeypatch.setattr(rsi_runs, "_cycle", lambda *a: Cycle())
    monkeypatch.setattr(rsi_runs, "_data", lambda *a: None)
    rsi_runs.rsi_tick(svc, "u1")
    assert rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    assert len(gw.prompts) == 1


def test_correction_arriving_during_triage_keeps_its_own_delivery(corrections):
    svc, gw, record = corrections
    first = [record() for _ in range(3)]
    original = gw.call
    late = []

    def during_review(*a, **kw):
        late.append(record(text="术语也请先解释"))
        return original(*a, **kw)

    gw.call = during_review
    rsi_runs.rsi_tick(svc, "u1")
    assert rsi_feedback.pending_ids(svc, "u1") == late
    assert all(signal_id not in rsi_feedback.pending_ids(svc, "u1") for signal_id in first)
    gw.call = original
    rsi_runs.rsi_tick(svc, "u1")
    assert not rsi_feedback.pending_ids(svc, "u1") and len(gw.prompts) == 2


@pytest.mark.parametrize("with_deep, expected", [(False, "skipped"), (True, "thin")])
def test_actual_durable_delivery_runs_real_cycle_and_preserves_gates(
    client, services, db_url, with_deep, expected, monkeypatch
):
    import json

    from dbos import DBOS

    from evowiki.agent import dbos_app
    from evowiki.llm.gateway import Gateway as RealGateway
    from evowiki.llm.models import GatewayConfig
    from evowiki.llm.scripted import ScriptedProvider
    from evowiki.llm.usage_control import UsageControl
    from evowiki.wikiapp.refine_service import DeepService
    from tests.deep.fakes import DeepScript
    from tests.harness.test_judge import DS
    from tests.harness.test_round import PRICES, REF, _Meter
    from tests.runtime.llm_helpers import MemoryCallLog

    script = Script()
    services.gateway = script.gateway()
    pid = new_page((client, script))
    served = []

    def answer(model, request):
        served.append(request.purpose)
        assert request.purpose == "rsi.h.triage"
        return json.dumps({"largest": 3, "what": "先给具体例子"})

    services.gateway = RealGateway(
        GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True),
        MemoryCallLog(),
        mode="record",
        providers={"scripted": ScriptedProvider("scripted", answer)},
        usage=UsageControl(meter=_Meter(), prices=PRICES),
    )
    if with_deep:
        from evowiki.harness import judge

        deep_script = DeepScript()
        monkeypatch.setattr(
            judge, "judge_configs", lambda: judge.JudgeConfigs(deepseek=DS, anthropic=None)
        )
        monkeypatch.setattr(judge, "judge_providers", deep_script.providers)
        services.deep = DeepService(
            repo=PgRepo(services.pool),
            jobs=None,
            assets=None,
            specs=None,
            checker=None,
            llm=deep_script.llm(),
            clock=services.clock,
            new_id=services.new_id,
        )
    PgRsiStore(services.pool).put_settings(
        "u1", RsiSettings(cycle="every_5_feedback", harness=True)
    )
    ids = [
        page_feedback_flow(
            PgRepo(services.pool),
            "u1",
            pid,
            PageFeedbackIn(text="请先给例子"),
            now=services.clock(),
            new_id=services.new_id,
        ).signal_id
        for _ in range(3)
    ]
    rsi_feedback.install(services)
    dbos_app.launch(
        db_url,
        app_version="d03-native-cycle",
        executor_id="d03-native-cycle",
        queues=[rsi_feedback.QUEUE],
        schedules=False,
    )
    try:
        rsi_feedback.register()
        wid = "rsi-feedback:u1:" + services.clock().strftime("%Y%m%d%H%M")
        DBOS.retrieve_workflow(wid).get_result()
        cycles = PgRsiStore(services.pool).cycles("u1")
        assert len(cycles) == 1 and cycles[0].trigger == "correction"
        with services.pool.connection() as conn:
            native = harness_rsi.last_round(conn, "u1")
        assert native.status == expected, native.reason
        assert served == ["rsi.h.triage"]
        assert rsi_feedback.pending_ids(services, "u1") == (
            sorted(ids) if expected == "skipped" else []
        )
    finally:
        rsi_feedback.uninstall()
        dbos_app.shutdown()


def test_sweep_keeps_disabled_feedback_pending_without_repeated_workflows(corrections, monkeypatch):
    svc, _gw, record = corrections
    ids = [record() for _ in range(3)]
    enqueued = []
    monkeypatch.setattr(rsi_feedback, "_svc", svc)
    monkeypatch.setattr(
        rsi_feedback, "_queue", SimpleNamespace(enqueue=lambda *a: enqueued.append(a))
    )
    store = PgRsiStore(svc.pool)
    for settings in (
        RsiSettings(cycle="manual", harness=True),
        RsiSettings(cycle="every_5_feedback", harness=False),
    ):
        store.put_settings("u1", settings)
        rsi_feedback.wake_pending()
        rsi_feedback.wake_pending()
        assert not enqueued
        assert rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
    store.put_settings("u1", RsiSettings(cycle="every_5_feedback", harness=True))
    rsi_feedback.wake_pending()
    assert len(enqueued) == 1
    assert rsi_feedback.pending_ids(svc, "u1") == sorted(ids)
