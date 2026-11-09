from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from evowiki.store import harness_rsi as store


def test_finish_round_freezes_expectation():
    calls = []
    conn = SimpleNamespace(execute=lambda sql, args: (
        calls.append(args) or SimpleNamespace(rowcount=1)))
    store.finish_round(conn, "r", "applied", reason="通过", signal_ids=["s"],
                       cases={"problem": [{"case_id": "c"}]},
                       decision={"citations": ["s"], "rationale": "增加例子应能讲清楚"},
                       diff={"ops": []}, now=datetime.now(UTC))
    expected = calls[0][7].obj["expectation"]
    assert expected == {"feedback": ["s"], "cases": {"problem": [{"case_id": "c"}]},
                        "citations": ["s"], "rationale": "增加例子应能讲清楚"}


@pytest.mark.parametrize("verdict", ["pending", "no_effect", "helpful", "harmful"])
def test_change_log_shows_pending_and_observed_without_writing(monkeypatch, verdict):
    now = datetime.now(UTC)
    change = store.ChangeRow("c", "u", "r", 1, {}, "rolled_back", None, None,
                             "增加例子", now, now)
    rnd = SimpleNamespace(decision={"expectation": {"rationale": "减少看不懂"}})
    point = SimpleNamespace(verdict=verdict, deltas=[{"metric": "unclear_rate"}],
                            note="auto_rollback", reason=None, at=now, dropped={})
    monkeypatch.setattr(store, "fetch_all", lambda *a: [vars(change)])
    monkeypatch.setattr(store, "get_round", lambda *a: rnd)
    monkeypatch.setattr(store, "observations", lambda *a: [point])
    log = store.change_log(object(), "u", limit=10)
    assert log[0]["expectation"] == rnd.decision["expectation"]
    assert log[0]["results"][0]["verdict"] == verdict
    assert log[0]["rolled_back"] is True
    monkeypatch.setattr(store, "observations", lambda *a: [])
    assert store.change_log(object(), "u", limit=10)[0]["results"] == []


def test_read_experience_shows_intention_results_and_rollback(monkeypatch):
    import json
    from contextlib import nullcontext

    from evowiki.harness import experience
    from evowiki.harness.improver import ImproverRun
    from evowiki.store import dreaming
    from tests.harness.test_improver import CASES, Improver, deps

    expectation = {"rationale": "减少看不懂", "citations": ["s1"], "cases": ["c1"]}
    dossiers = experience.build_dossiers(
        [{"round_id": "r", "finding": "讲解不清楚", "decision": {"expectation": expectation}}],
        [{"change_id": "c", "round_id": "r", "status": "rolled_back", "diff": {}}], [],
        [{"obs_id": "o", "change_id": "c", "verdict": "no_effect", "reason": "信号没有明显变化"}], [])
    monkeypatch.setattr(experience, "load_dossiers", lambda *a, **k: dossiers)
    consumed = []
    monkeypatch.setattr(dreaming, "register_consumption", lambda conn, uid, node, sources:
                        consumed.append((uid, node, set(sources))))
    pool = SimpleNamespace(connection=lambda: nullcontext(object()))
    run = ImproverRun("u1", CASES, deps(pool, Improver()))
    out = run.read_experience({"mode": "change", "change_id": "c"})
    assert all(text in out for text in ("减少看不懂", "s1", "c1", "no_effect", "信号没有明显变化", "rolled_back"))
    assert json.loads(out)["items"][0]["change"]["status"] == "rolled_back"
    assert {"change:c", "observation:o"} <= run.known()
    assert consumed == [("u1", "round:r1", set(dossiers[0]["sources"]))]


def test_observation_records_explanation_without_new_decision(monkeypatch):
    from evowiki.harness import observe
    from evowiki.teach.contracts import AnchorDelta, OutcomeOut

    calls = []
    monkeypatch.setattr(store, "add_observation", lambda *a, **k: calls.append(k) or "o")
    outcome = OutcomeOut(change_id="c", at=datetime.now(UTC), verdict="no_effect", deltas=(
        AnchorDelta(metric="check_correct", before=.5, after=.5, delta=0,
                    n_before=30, n_after=30),))
    subject = observe._Subject("change", "c", "r", {})
    subject.add(object(), "u", outcome.verdict, observe._dump(outcome), now=outcome.at)
    assert "未观察到效果" in calls[0]["reason"]
    assert "check_correct" in calls[0]["reason"]
