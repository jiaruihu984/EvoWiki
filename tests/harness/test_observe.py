"""Watching applied harness changes (harness RSI phase 2, Task 11): the anchor metrics before vs
after a change (signals by time; deep write runs' cost and latency by pack version), the points
kept in `harness_observation`, the automatic rollback after two harmful points in a row and the
lessons written from the outcome. Real Postgres; fake signals, `agent_run` / `llm_call` rows."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.harness.diff import KeyOp, PackDiff, diff_to_json
from evowiki.harness.observe import ObserveDeps, observe_changes
from evowiki.store import harness as pack_store
from evowiki.store import harness_rsi as hr
from evowiki.teach.contracts import Signal
from evowiki.teach.spec_registry import MemoryRegistry
from tests.runtime.helpers import make_user

U = "u1"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
APPLIED = T0 + timedelta(days=5)
H4 = "H4.deep.max_tool_calls"


class Signals:
    def __init__(self):
        self.items: list[Signal] = []

    def add(self, kind, n, at, *, outcome=None, chips=()):
        start = len(self.items)
        self.items += [Signal(signal_id=f"s{start + i}", kind=kind, at=at + timedelta(minutes=i),
                              page_id="p1", section_id="s2", outcome=outcome, chips=chips)
                       for i in range(n)]

    def __call__(self, user_id, since):
        assert since is None
        return list(self.items)


@pytest.fixture
def env(pool):
    make_user(pool, U, now=T0)
    with pool.connection() as conn:
        pack_store.set_value(conn, U, H4, 80, source="user", why="起点", now=T0)
        rid = hr.start_round(conn, U, finding="讲得太浅：页一", now=APPLIED)
        hr.finish_round(conn, rid, "applied", reason="关卡通过",
                        cases={"problem": [{"case_id": "c1", "signal_ids": ["trig"]}],
                               "anchors": [], "unusable": [], "finding": "讲得太浅：页一"},
                        decision={"improver": "a"}, now=APPLIED)
        v = pack_store.set_value(conn, U, H4, 100, source="improver", why="r", now=APPLIED)
        stored = diff_to_json(PackDiff((KeyOp(H4, 100),)), {H4: 80}, None)
        cid = hr.record_change(conn, U, rid, stored, "applied", version=v, now=APPLIED)
    sig = Signals()
    sig.items.append(Signal(signal_id="trig", kind="page_feedback", at=APPLIED - timedelta(hours=1),
                            page_id="p1", chips=("too_shallow",)))
    return pool, cid, v, sig


def deps(pool, sig):
    return ObserveDeps(pool=pool, registry=MemoryRegistry(), signals=sig)


def _lessons(pool):
    with pool.connection() as conn:
        return hr.experience(conn, U, query=None, limit=50, now=APPLIED + timedelta(days=30))


def _change(pool, cid):
    with pool.connection() as conn:
        return hr.get_change(conn, U, cid)


def _points(pool, cid):
    with pool.connection() as conn:
        return hr.observations(conn, U, cid)


def test_not_enough_events_gives_no_verdict_and_writes_nothing(env):
    pool, cid, _, sig = env
    sig.add("check_unclear", 5, T0)
    sig.add("check_answer", 5, APPLIED + timedelta(hours=1), outcome=1.0)
    (res,) = observe_changes(U, deps(pool, sig), now=APPLIED + timedelta(days=2))
    assert res.change_id == cid and res.verdict == "not_judged" and res.deltas == {}
    assert _points(pool, cid) == [] and _lessons(pool) == []
    assert _change(pool, cid).status == "applied"


def test_helpful_writes_one_effective_lesson_the_first_time(env):
    pool, cid, _, sig = env
    sig.add("check_unclear", 20, T0)                                        # 看不懂 before
    sig.add("check_answer", 20, APPLIED + timedelta(hours=1), outcome=1.0)  # understood after
    now = APPLIED + timedelta(days=2)
    (res,) = observe_changes(U, deps(pool, sig), now=now)
    assert res.verdict == "helpful" and res.deltas["unclear_rate"] == pytest.approx(20 / 21)   # the trigger stays before
    (lesson,) = _lessons(pool)
    assert lesson.verdict == "effective" and lesson.scope == "general"
    assert lesson.situation == {"agent": "deep", "signal_kinds": ["page_feedback"],
                                "finding": "讲得太浅：页一"}
    assert lesson.change["ops"][0]["name"] == H4
    assert lesson.evidence["change_id"] == cid
    assert {d["metric"] for d in lesson.evidence["deltas"]} >= {"unclear_rate", "check_correct"}
    # more evidence, still helpful: a new point, never a second lesson
    sig.add("check_answer", 10, APPLIED + timedelta(days=1), outcome=1.0)
    (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(days=1))
    assert res.verdict == "helpful" and res.point
    assert len(_points(pool, cid)) == 2 and len(_lessons(pool)) == 1
    (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(days=2))   # nothing new
    assert not res.point and res.verdict == "helpful" and len(_points(pool, cid)) == 2
    assert _change(pool, cid).status == "applied"


def test_harmful_once_waits_and_harmful_twice_rolls_back_with_a_harmful_lesson(env):
    pool, cid, _, sig = env
    sig.add("check_answer", 20, T0, outcome=1.0)                       # no 看不懂 before
    sig.add("check_unclear", 10, APPLIED + timedelta(hours=1))         # all 看不懂 after
    now = APPLIED + timedelta(days=2)
    (res,) = observe_changes(U, deps(pool, sig), now=now)
    assert res.verdict == "harmful" and not res.rolled_back
    assert res.deltas["unclear_rate"] == pytest.approx(-1.0)
    assert _change(pool, cid).status == "applied" and _lessons(pool) == []
    (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(hours=1))  # no new evidence
    assert not res.rolled_back and len(_points(pool, cid)) == 1
    sig.add("check_unclear", 10, APPLIED + timedelta(days=1))
    (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(days=1))
    assert res.verdict == "harmful" and res.rolled_back
    row = _change(pool, cid)
    assert row.status == "rolled_back"
    with pool.connection() as conn:
        assert pack_store.load_pack(conn, U).values[H4] == 80
    (lesson,) = _lessons(pool)
    assert lesson.verdict == "harmful" and lesson.evidence["change_id"] == cid
    unclear = next(d for d in lesson.evidence["deltas"] if d["metric"] == "unclear_rate")
    assert unclear["delta"] == pytest.approx(-1.0) and unclear["n_after"] == 20
    assert len(lesson.evidence["points"]) == 2
    # the rolled-back change is no longer watched
    assert observe_changes(U, deps(pool, sig), now=now + timedelta(days=3)) == []
    assert len(_lessons(pool)) == 1


def _run(conn, run_id, pv, at, cost, latency, *, agent="deep.write", trigger=None,
         finished=True):
    """One deep run of `latency` seconds wall time with two model calls costing `cost` in all
    (None: unpriced)."""
    inp = {"page_id": "p1"} if trigger is None else {"page_id": "p1", "job": {"trigger": trigger}}
    conn.execute("insert into agent_run (run_id, user_id, agent, subject, pack_version, input, "
                 "started_at, finished_at, stop) values (%s, %s, %s, 'p1', %s, %s, %s, %s, 'finish')",
                 (run_id, U, agent, pv, json.dumps(inp), at,
                  at + timedelta(seconds=latency) if finished else None))
    for seq in range(2):                          # two model calls per run: cost summed
        cid = f"llm_{run_id}_{seq}"
        conn.execute("insert into llm_call (id, input_hash, purpose, prompt_version, subject, "
                     "provider, model, params, raw_output, cost, latency, ts) values "
                     "(%s, %s, 'deep.write', 'v', 'p1', 'scripted', 'm', '{}', '', %s, 1.0, %s)",
                     (cid, cid, None if cost is None else cost / 2, at))
        conn.execute("insert into agent_step (run_id, seq, kind, payload, call_id, ts) "
                     "values (%s, %s, 'model', '{}', %s, %s)", (run_id, seq, cid, at))


def test_page_cost_and_latency_split_by_pack_version(env):
    pool, cid, v, sig = env
    with pool.connection() as conn:
        for i in range(12):
            _run(conn, f"old{i}", v - 1, T0 + timedelta(hours=i), 0.10, 20.0)
            _run(conn, f"new{i}", v, APPLIED + timedelta(hours=i + 1), 0.30, 40.0)
        # left out: an old-pack run started after the change, a replay, another agent
        _run(conn, "late-old", v - 1, APPLIED + timedelta(minutes=5), 9.0, 900.0)
        _run(conn, "harness-replay:x", v, APPLIED + timedelta(hours=2), 9.0, 900.0)
        _run(conn, "job-replay", v, APPLIED + timedelta(hours=2), 9.0, 900.0,
             trigger="harness_replay")
        _run(conn, "plan", v, APPLIED + timedelta(hours=2), 9.0, 900.0, agent="assistant")
        # counted as dropped, not silently: an unpriced job and an unfinished one after the change
        _run(conn, "unpriced", v, APPLIED + timedelta(hours=3), None, 40.0)
        _run(conn, "running", v, APPLIED + timedelta(hours=3), 0.30, 40.0, finished=False)
    (res,) = observe_changes(U, deps(pool, sig), now=APPLIED + timedelta(days=2))
    assert res.dropped == {"after": {"unpriced": 1, "unfinished": 1}}
    assert res.deltas["page_cost"] == pytest.approx(-0.20)
    assert res.deltas["page_latency"] == pytest.approx(-20.0)
    assert res.verdict == "harmful" and not res.rolled_back
    (pt,) = _points(pool, cid)
    cost = next(d for d in pt.deltas if d["metric"] == "page_cost")
    # the unfinished job's partial cost is left out like its latency (final review M4)
    assert (cost["n_before"], cost["n_after"], cost["before"]) == (12, 12, pytest.approx(0.10))
    lat = next(d for d in pt.deltas if d["metric"] == "page_latency")
    assert (lat["n_before"], lat["n_after"]) == (12, 13)          # the unpriced job has a latency
    assert pt.dropped == {"after": {"unpriced": 1, "unfinished": 1}}


def test_page_cost_and_latency_are_per_page_job(env):
    pool, cid, v, sig = env
    with pool.connection() as conn:
        for i in range(12):
            _run(conn, f"refine:p1:old{i}:write:1:w0", v - 1, T0 + timedelta(hours=i), 0.10, 20.0)
            # after: two rounds per page job, 100 s apart: 0.20 per page, 140 s wall time
            at = APPLIED + timedelta(hours=i + 1)
            _run(conn, f"refine:p1:new{i}:write:1:w0", v, at, 0.10, 20.0)
            _run(conn, f"refine:p1:new{i}:write:2:w0", v, at + timedelta(seconds=100), 0.10, 40.0)
    (res,) = observe_changes(U, deps(pool, sig), now=APPLIED + timedelta(days=2))
    assert res.deltas["page_cost"] == pytest.approx(-0.10)
    assert res.deltas["page_latency"] == pytest.approx(-120.0)
    (pt,) = _points(pool, cid)
    cost = next(d for d in pt.deltas if d["metric"] == "page_cost")
    assert (cost["n_before"], cost["n_after"]) == (12, 12)


def test_the_before_side_starts_at_the_previous_change(env):
    pool, _, _, sig = env
    prev_at = APPLIED - timedelta(days=2)
    with pool.connection() as conn:
        rid = hr.start_round(conn, U, finding="更早的一次", now=prev_at)
        hr.finish_round(conn, rid, "applied", reason="ok", now=prev_at)
        prev = hr.record_change(conn, U, rid, {"ops": [], "spec_ops": [],
                                               "spec_before_version": None},
                                "applied", version=None, now=prev_at)
        hr.settle_change(conn, prev, "undone", now=prev_at + timedelta(hours=1))
    sig.add("check_unclear", 30, T0)                                 # old, much worse: excluded
    sig.add("check_answer", 20, prev_at + timedelta(hours=2), outcome=1.0)
    sig.add("check_answer", 20, APPLIED + timedelta(hours=1), outcome=1.0)
    (res,) = observe_changes(U, deps(pool, sig), now=APPLIED + timedelta(days=2))
    assert res.verdict == "not_judged" and res.deltas["unclear_rate"] == pytest.approx(0.0)
    assert _lessons(pool) == []


def test_a_newer_applied_change_ends_the_after_side(env):
    pool, cid, v, sig = env
    later = APPLIED + timedelta(days=1)
    with pool.connection() as conn:
        rid = hr.start_round(conn, U, finding="另一处", now=later)
        hr.finish_round(conn, rid, "applied", reason="ok", now=later)
        v2 = pack_store.set_value(conn, U, "H4.deep.max_rewrites", 3, source="improver", why="r",
                                  now=later)
        newer = hr.record_change(conn, U, rid, diff_to_json(
            PackDiff((KeyOp("H4.deep.max_rewrites", 3),)), {"H4.deep.max_rewrites": None}, None),
            "applied", version=v2, now=later)
        for i in range(12):
            _run(conn, f"old{i}", v - 1, T0 + timedelta(hours=i), 0.10, 20.0)
            _run(conn, f"mine{i}", v, APPLIED + timedelta(minutes=10 + i), 0.10, 20.0)
            _run(conn, f"next{i}", v2, later + timedelta(hours=i + 1), 5.0, 500.0)
    res = {r.change_id: r for r in observe_changes(U, deps(pool, sig),
                                                   now=later + timedelta(days=2))}
    assert res[cid].deltas["page_cost"] == pytest.approx(0.0)       # v2's runs are not mine
    assert res[newer].deltas["page_cost"] < 0


def test_a_spec_only_change_splits_by_time(env):
    pool, cid, v, sig = env
    later = APPLIED + timedelta(days=1)
    with pool.connection() as conn:
        hr.settle_change(conn, cid, "undone", now=later)            # only the spec change in force
        pack_store.set_value(conn, U, H4, 80, source="rollback", why="x", now=later)
        rid = hr.start_round(conn, U, finding="讲解规则", now=later)
        hr.finish_round(conn, rid, "applied", reason="ok", now=later)
        spec_cid = hr.record_change(conn, U, rid, {"ops": [], "spec_ops": [],
                                                   "spec_before_version": 1},
                                    "applied", version=None, now=later)
        for i in range(12):
            _run(conn, f"b{i}", v, later - timedelta(hours=i + 1), 0.10, 20.0)
            _run(conn, f"a{i}", v + 1, later + timedelta(hours=i + 1), 0.10, 20.0)
    (res,) = observe_changes(U, deps(pool, sig), now=later + timedelta(days=2))
    assert res.change_id == spec_cid
    assert res.deltas["page_cost"] == pytest.approx(0.0)


def test_a_refused_rollback_writes_the_lesson_once_and_is_recorded_not_retried(env):
    pool, cid, _, sig = env
    sig.add("check_answer", 20, T0, outcome=1.0)
    sig.add("check_unclear", 10, APPLIED + timedelta(hours=1))
    now = APPLIED + timedelta(days=2)
    observe_changes(U, deps(pool, sig), now=now)
    with pool.connection() as conn:                       # the user changed the key since
        pack_store.set_value(conn, U, H4, 120, source="user", why="我自己改", now=now)
    sig.add("check_unclear", 10, APPLIED + timedelta(days=1))
    (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(days=1))
    assert res.verdict == "harmful" and not res.rolled_back
    assert res.note.startswith("连续两次观察都变差了，但没能撤回") and res.lesson == "harmful"
    pts = _points(pool, cid)
    assert len(pts) == 3 and pts[-1].note == "rollback_refused" and pts[-1].reason
    (lesson,) = _lessons(pool)
    assert lesson.verdict == "harmful" and pts[-1].lesson_id == lesson.exp_id
    assert _change(pool, cid).status == "applied"
    (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(days=2))   # not retried
    assert res.note == pts[-1].reason and not res.lesson
    assert len(_points(pool, cid)) == 3 and len(_lessons(pool)) == 1


def test_a_harmful_lesson_after_an_effective_one_names_it(env):
    pool, _, v, sig = env
    with pool.connection() as conn:
        for i in range(12):
            _run(conn, f"old{i}", v - 1, T0 + timedelta(hours=i), 0.30, 20.0)
            _run(conn, f"new{i}", v, APPLIED + timedelta(minutes=i + 1), 0.10, 20.0)
    now = APPLIED + timedelta(days=2)
    (res,) = observe_changes(U, deps(pool, sig), now=now)
    assert res.verdict == "helpful" and res.lesson == "effective"
    for k, n in ((1, 30), (2, 10)):                  # then much dearer pages: harmful twice
        with pool.connection() as conn:
            for i in range(n):
                _run(conn, f"dear{k}-{i}", v, APPLIED + timedelta(hours=k, minutes=i), 1.0, 20.0)
        (res,) = observe_changes(U, deps(pool, sig), now=now + timedelta(days=k))
    assert res.rolled_back and res.lesson == "harmful"
    effective, harmful = sorted(_lessons(pool), key=lambda e: e.created_at)
    assert effective.verdict == "effective" and harmful.evidence["supersedes"] == effective.exp_id


def test_a_change_undone_since_it_was_listed_is_skipped(env, monkeypatch):
    pool, cid, _, sig = env
    with pool.connection() as conn:
        stale = hr.applied_changes(conn, U)
        hr.settle_change(conn, cid, "undone", now=APPLIED + timedelta(hours=1))
    from evowiki.harness import observe as obs
    monkeypatch.setattr(obs.harness_rsi, "applied_changes", lambda conn, uid: stale)
    sig.add("check_unclear", 20, T0)
    sig.add("check_answer", 20, APPLIED + timedelta(hours=1), outcome=1.0)
    assert observe_changes(U, deps(pool, sig), now=APPLIED + timedelta(days=2)) == []
    assert _points(pool, cid) == [] and _lessons(pool) == []


def test_the_cycle_row_names_the_change_with_its_prefix():
    from evowiki.harness.observe import ObserveResult
    from evowiki.harness.round import observation_row
    row = observation_row(ObserveResult("c1", "harmful", {"page_cost": -0.2}, point=True,
                                        rolled_back=True, lesson="harmful", note="x",
                                        dropped={"after": {"unpriced": 1}}))
    assert row == {"change_id": "h_c1", "verdict": "harmful", "deltas": {"page_cost": -0.2},
                   "point": True, "rolled_back": True, "lesson": "harmful", "note": "x",
                   "dropped": {"after": {"unpriced": 1}}}


# ---- assistant changes (harness 3b follow-ups, #12 / MF7) ---------------------------------------

def _round_change(pool, ops, before, *, agent, at):
    with pool.connection() as conn:
        rid = hr.start_round(conn, U, finding="助手的回答被拒绝", now=at)
        hr.finish_round(conn, rid, "applied", reason="关卡通过",
                        cases={"problem": [{"case_id": "c2", "signal_ids": ["trig"]}],
                               "anchors": [], "unusable": [], "finding": "助手的回答被拒绝",
                               "agent": agent},
                        decision={"improver": "a"}, now=at)
        v = None
        for name, value in ops:
            v = pack_store.set_value(conn, U, name, value, source="improver", why="r", now=at)
        stored = diff_to_json(PackDiff(tuple(KeyOp(n, val) for n, val in ops)), before, None)
        return hr.record_change(conn, U, rid, stored, "applied", version=v, now=at)


class Rated:
    """`ObserveDeps.rated`: (answer time, 👎?) of the rated answers."""

    def __init__(self):
        self.items: list[tuple[datetime, bool]] = []

    def add(self, n, at, down):
        self.items += [(at + timedelta(minutes=i), down) for i in range(n)]

    def __call__(self, user_id):
        return list(self.items)


def test_an_assistant_only_change_is_judged_by_the_down_rate_alone(pool):
    """Page drift cannot come from an assistant-only change: harmful page metrics give it no
    point. Its 👎 rate does: pending under MIN_EVENTS, then harmful twice in a row → rolled back."""
    make_user(pool, U, now=T0)
    cid = _round_change(pool, [("H2.assistant.read_k", "先读再讲")], {}, agent="assistant",
                        at=APPLIED)
    sig = Signals()
    sig.add("check_answer", 20, T0, outcome=1.0)
    sig.add("check_unclear", 20, APPLIED + timedelta(hours=1))         # pages look harmful
    rated = Rated()
    rated.add(20, T0, False)                                           # before: all 👍
    rated.add(5, APPLIED + timedelta(hours=1), True)                   # after: 5 👎, too few
    d = ObserveDeps(pool=pool, registry=MemoryRegistry(), signals=sig, rated=rated)
    now = APPLIED + timedelta(days=2)
    (res,) = observe_changes(U, d, now=now)
    assert (res.change_id, res.verdict, res.deltas, res.point) == (cid, "not_judged", {}, False)
    assert _points(pool, cid) == [] and _change(pool, cid).status == "applied"
    # no rated answers wired at all: the same pending, never the page metrics
    (res,) = observe_changes(U, deps(pool, sig), now=now)
    assert (res.verdict, res.point) == ("not_judged", False)

    rated.add(5, APPLIED + timedelta(hours=3), True)                   # 10 after: a point
    (res,) = observe_changes(U, d, now=now)
    assert res.verdict == "harmful" and res.point and not res.rolled_back
    assert set(res.deltas) == {"assistant_down_rate"}
    assert res.deltas["assistant_down_rate"] == pytest.approx(-1.0)
    (pt,) = _points(pool, cid)
    assert [x["metric"] for x in pt.deltas] == ["assistant_down_rate"]
    assert _change(pool, cid).status == "applied" and _lessons(pool) == []

    rated.add(10, APPLIED + timedelta(days=1), True)                   # harmful again
    (res,) = observe_changes(U, d, now=now + timedelta(days=1))
    assert res.verdict == "harmful" and res.rolled_back and res.lesson == "harmful"
    assert _change(pool, cid).status == "rolled_back"
    (lesson,) = _lessons(pool)
    assert lesson.verdict == "harmful" and lesson.situation["agent"] == "assistant"
    assert [x["metric"] for x in lesson.evidence["deltas"]] == ["assistant_down_rate"]


def test_a_deep_only_change_ignores_ratings_even_when_wired(env):
    pool, cid, _, sig = env
    sig.add("check_unclear", 20, T0)
    sig.add("check_answer", 20, APPLIED + timedelta(hours=1), outcome=1.0)   # pages: helpful
    rated = Rated()
    rated.add(20, T0, False)
    rated.add(20, APPLIED + timedelta(hours=1), True)                       # answers: harmful
    d = ObserveDeps(pool=pool, registry=MemoryRegistry(), signals=sig, rated=rated)
    (res,) = observe_changes(U, d, now=APPLIED + timedelta(days=2))
    assert res.change_id == cid and res.verdict == "helpful"
    assert "assistant_down_rate" not in res.deltas
    (pt,) = _points(pool, cid)
    assert "assistant_down_rate" not in {x["metric"] for x in pt.deltas}


def test_ratings_are_placed_by_the_answers_time_not_the_ratings():
    from evowiki.harness.observe import _rated_values, _Window
    w = _Window(lower=None, start=APPLIED, end=APPLIED + timedelta(days=3), version=None,
                end_version=None)
    rated = [(APPLIED - timedelta(hours=1), True), (APPLIED + timedelta(hours=1), False),
             (APPLIED + timedelta(days=4), True), (APPLIED + timedelta(days=9), True)]
    assert _rated_values(rated, w, APPLIED + timedelta(days=5)) == ([1.0], [0.0])


def test_a_mixed_change_keeps_the_page_metrics_and_its_lesson_says_mixed(pool):
    make_user(pool, U, now=T0)
    with pool.connection() as conn:
        pack_store.set_value(conn, U, H4, 80, source="user", why="起点", now=T0)
    cid = _round_change(pool, [(H4, 100), ("H2.assistant.read_k", "先读再讲")], {H4: 80},
                        agent="mixed", at=APPLIED)
    sig = Signals()
    sig.items.append(Signal(signal_id="trig", kind="assistant_rejected",
                            at=APPLIED - timedelta(hours=1), page_id="p1"))
    sig.add("check_unclear", 20, T0)
    sig.add("check_answer", 20, APPLIED + timedelta(hours=1), outcome=1.0)
    rated = Rated()
    rated.add(12, T0, False)
    rated.add(12, APPLIED + timedelta(hours=1), False)
    d = ObserveDeps(pool=pool, registry=MemoryRegistry(), signals=sig, rated=rated)
    (res,) = observe_changes(U, d, now=APPLIED + timedelta(days=2))
    assert res.change_id == cid and res.verdict == "helpful" and res.point
    assert {"unclear_rate", "assistant_down_rate"} <= set(res.deltas)  # both kinds of metric
    (lesson,) = _lessons(pool)
    assert lesson.situation["agent"] == "mixed"
