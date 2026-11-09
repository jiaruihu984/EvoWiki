"""The fast rollback channel of applied harness changes (`harness.fast_check`): clear failures the
new pack version shows and the previous one did not, read from `agent_run` / `agent_step` /
`refine_job` rows, roll the change back at once with a `harmful` lesson; a refused undo is
recorded and not retried without new failures. Real Postgres; no model call."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.harness import fast_check as fc
from evowiki.harness.diff import KeyOp, PackDiff, diff_to_json
from evowiki.harness.fast_check import FastDeps, fast_check, refusal_kind, unit_failures
from evowiki.store import harness as pack_store
from evowiki.store import harness_rsi as hr
from evowiki.teach.spec_registry import MemoryRegistry
from tests.runtime.helpers import make_user

U = "u1"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
APPLIED = T0 + timedelta(days=5)
H4 = "H4.deep.max_tool_calls"


def _change(pool, key, before, after, *, at):
    with pool.connection() as conn:
        rid = hr.start_round(conn, U, finding="讲得太浅：页一", now=at)
        hr.finish_round(conn, rid, "applied", reason="关卡通过",
                        cases={"problem": [], "anchors": [], "unusable": [],
                               "finding": "讲得太浅：页一"},
                        decision={"improver": "b"}, now=at)
        v = pack_store.set_value(conn, U, key, after, source="improver", why="r", now=at)
        stored = diff_to_json(PackDiff((KeyOp(key, after),)), {key: before}, None)
        return hr.record_change(conn, U, rid, stored, "applied", version=v, now=at), v


@pytest.fixture
def env(pool):
    make_user(pool, U, now=T0)
    with pool.connection() as conn:
        pack_store.set_value(conn, U, H4, 80, source="user", why="起点", now=T0)
    cid, v = _change(pool, H4, 80, 10, at=APPLIED)         # an H4 cap far too low
    return pool, cid, v


def _job(conn, key, pv, at, *, stop="finished", error=None, tools=(), failure=None,
         agent="deep.write", finished=True):
    """One page job of one write run (`tools`: (name, error, crashed) of its failed tool calls;
    `failure`: its refine job's failure) or, with agent assistant.turn, one assistant turn."""
    run_id = f"assistant:s1:{key}" if agent == fc.ASSISTANT_AGENT else f"{key}:write:1:w0"
    conn.execute("insert into agent_run (run_id, user_id, agent, subject, pack_version, input, "
                 "started_at, finished_at, stop, error) values (%s, %s, %s, 'p1', %s, %s, %s, "
                 "%s, %s, %s)",
                 (run_id, U, agent, pv, json.dumps({"page_id": "p1", "job_id": key}), at,
                  at + timedelta(seconds=30) if finished else None, stop, error))
    for seq, (name, err, crashed) in enumerate(tools):
        content = ("工具出错：" if crashed else "错误：") + err
        conn.execute("insert into agent_step (run_id, seq, kind, payload, ts) values "
                     "(%s, %s, 'tool', %s, %s)",
                     (run_id, seq, json.dumps({"name": name, "args": {}, "result": {
                         "content": content, "error": err}}), at))
    if failure is not None:
        conn.execute("insert into refine_job (job_id, user_id, page_id, job) values (%s, %s, "
                     "'p1', %s)", (key, U, json.dumps({"job_id": key, "failure": failure})))
    return run_id


def _old(conn, v, n=3, **kw):
    return [_job(conn, f"old{i}", v - 1, T0 + timedelta(hours=i + 1), **kw) for i in range(n)]


def deps(pool):
    return FastDeps(pool=pool, registry=MemoryRegistry())


def _lessons(pool):
    with pool.connection() as conn:
        return hr.experience(conn, U, query=None, limit=50, now=APPLIED + timedelta(days=30))


def _status(pool, cid):
    with pool.connection() as conn:
        return hr.get_change(conn, U, cid).status


def _checks(pool, cid):
    with pool.connection() as conn:
        return hr.fast_checks(conn, U, cid)


NOW = APPLIED + timedelta(days=1)


def test_two_new_tool_cap_failures_roll_the_change_back_with_a_harmful_lesson(env):
    pool, cid, v = env
    with pool.connection() as conn:
        _old(conn, v)
        bad = [_job(conn, f"new{i}", v, APPLIED + timedelta(hours=i + 1), stop="tool_cap")
               for i in range(2)]
    (res,) = fast_check(U, deps(pool), now=NOW)
    assert res.change_id == cid and res.rolled_back and res.lesson == "harmful"
    assert res.note == fc.ROLLED_BACK
    (f,) = res.failures
    assert (f.agent, f.signature, f.n_after, f.before_units, f.after_units) == (
        "deep", fc.TOOL_CAP, 2, 3, 2)
    assert _status(pool, cid) == "rolled_back"
    with pool.connection() as conn:
        assert pack_store.load_pack(conn, U).values[H4] == 80
    (lesson,) = _lessons(pool)
    assert lesson.verdict == "harmful" and lesson.evidence["by"] == "fast_check"
    assert lesson.evidence["change_id"] == cid and sorted(lesson.evidence["runs"]) == sorted(bad)
    assert lesson.change["ops"][0]["name"] == H4
    (row,) = _checks(pool, cid)
    assert row.outcome == "rolled_back" and row.lesson_id == lesson.exp_id
    # rolled back: no longer watched, by this channel or by observe
    assert fast_check(U, deps(pool), now=NOW + timedelta(days=1)) == []
    with pool.connection() as conn:
        assert hr.observations(conn, U, cid) == []          # the metric points stay apart


def test_one_failure_waits(env):
    pool, cid, v = env
    with pool.connection() as conn:
        _old(conn, v)
        _job(conn, "new0", v, APPLIED + timedelta(hours=1), stop="tool_cap")
        _job(conn, "new1", v, APPLIED + timedelta(hours=2))
    assert fast_check(U, deps(pool), now=NOW) == []
    assert _status(pool, cid) == "applied" and _checks(pool, cid) == [] and _lessons(pool) == []


def test_a_failure_the_old_version_had_too_is_not_new(env):
    pool, cid, v = env
    with pool.connection() as conn:
        _old(conn, v, n=2)
        _job(conn, "old-cap", v - 1, T0 + timedelta(hours=5), stop="tool_cap")
        for i in range(3):
            _job(conn, f"new{i}", v, APPLIED + timedelta(hours=i + 1), stop="tool_cap")
    assert fast_check(U, deps(pool), now=NOW) == []
    assert _status(pool, cid) == "applied"


def test_too_few_old_units_decide_nothing(env):
    pool, _, v = env
    with pool.connection() as conn:
        _old(conn, v, n=fc.MIN_BASELINE - 1)
        for i in range(3):
            _job(conn, f"new{i}", v, APPLIED + timedelta(hours=i + 1), stop="tool_cap")
    assert fast_check(U, deps(pool), now=NOW) == []


def test_unfinished_and_replayed_units_are_left_out(env):
    pool, _, v = env
    with pool.connection() as conn:
        _old(conn, v)
        _job(conn, "new0", v, APPLIED + timedelta(hours=1), stop="tool_cap", finished=False)
        _job(conn, "harness-replay:x", v, APPLIED + timedelta(hours=2), stop="tool_cap")
        _job(conn, "new1", v, APPLIED + timedelta(hours=3), stop="tool_cap")
    assert fast_check(U, deps(pool), now=NOW) == []


def test_refine_job_failures_count_but_provider_trouble_does_not(env):
    pool, _, v = env
    with pool.connection() as conn:
        _old(conn, v)
        for i in range(3):                                   # transient: never the pack's fault
            _job(conn, f"busy{i}", v, APPLIED + timedelta(hours=i + 1), failure="llm_busy",
                 error="ProviderBusyError")
    assert fast_check(U, deps(pool), now=NOW) == []
    with pool.connection() as conn:
        for i in range(2):
            _job(conn, f"render{i}", v, APPLIED + timedelta(hours=i + 5),
                 failure="render_failed")
    (res,) = fast_check(U, deps(pool), now=NOW)
    assert res.rolled_back and [f.signature for f in res.failures] == ["job:render_failed"]


def test_tool_errors_group_by_tool_and_kind(env):
    pool, _, v = env
    with pool.connection() as conn:
        _old(conn, v, tools=[("write_section", "没有段 s1：先用 write_section 写这一段", False)])
        _job(conn, "new0", v, APPLIED + timedelta(hours=1),
             tools=[("finish", "正文引用了不存在的交互、块或图片：a1, b2", False),
                    ("write_section", "没有段 s7：先用 write_section 写这一段", False)])
        _job(conn, "new1", v, APPLIED + timedelta(hours=2),
             tools=[("finish", "正文引用了不存在的交互、块或图片：c3", False)])
    (res,) = fast_check(U, deps(pool), now=NOW)
    (f,) = res.failures                    # the old pack's write_section refusal is not new
    assert f.signature == "tool:finish:正文引用了不存在的交互、块或图片" and f.n_after == 2


def test_a_refused_undo_is_recorded_once_and_retried_only_on_new_failures(env):
    pool, cid, v = env
    with pool.connection() as conn:
        _old(conn, v)
        for i in range(2):
            _job(conn, f"new{i}", v, APPLIED + timedelta(hours=i + 1), stop="tool_cap")
        # the user set the key themself since: the undo must not overwrite their edit
        pack_store.set_value(conn, U, H4, 60, source="user", why="我自己改",
                             now=APPLIED + timedelta(hours=5))
    (res,) = fast_check(U, deps(pool), now=NOW)
    assert not res.rolled_back and res.lesson == "harmful"
    assert res.note.startswith("新版本出现了旧版本没有的错误，但没能撤回")
    (row,) = _checks(pool, cid)
    assert row.outcome == "refused" and row.reason and row.evidence["n_failing"] == 2
    assert _status(pool, cid) == "applied" and len(_lessons(pool)) == 1
    with pool.connection() as conn:
        assert pack_store.load_pack(conn, U).values[H4] == 60
    assert fast_check(U, deps(pool), now=NOW + timedelta(hours=1)) == []      # not every cycle
    assert len(_checks(pool, cid)) == 1
    with pool.connection() as conn:    # failures under the user's own pack are not the change's
        _job(conn, "mine0", v + 1, APPLIED + timedelta(hours=6), stop="tool_cap")
    assert fast_check(U, deps(pool), now=NOW + timedelta(hours=2)) == []
    with pool.connection() as conn:    # one more under the change's own version: tried again
        _job(conn, "new2", v, APPLIED + timedelta(hours=3), stop="tool_cap")
    (res,) = fast_check(U, deps(pool), now=NOW + timedelta(hours=3))
    assert not res.rolled_back and res.lesson is None                # the lesson stays one
    assert [c.outcome for c in _checks(pool, cid)] == ["refused", "refused"]
    assert len(_lessons(pool)) == 1


def test_an_assistant_only_change_is_judged_by_assistant_turns_alone(pool):
    make_user(pool, U, now=T0)
    key = "H4.assistant.max_tool_calls"
    with pool.connection() as conn:
        pack_store.set_value(conn, U, key, 12, source="user", why="起点", now=T0)
    cid, v = _change(pool, key, 12, 1, at=APPLIED)
    with pool.connection() as conn:
        _old(conn, v)                                         # deep jobs only
        for i in range(3):
            _job(conn, f"new{i}", v, APPLIED + timedelta(hours=i + 1), stop="tool_cap")
    assert fast_check(U, deps(pool), now=NOW) == []           # deep failures do not judge it
    with pool.connection() as conn:
        for i in range(3):
            _job(conn, f"t-old{i}", v - 1, T0 + timedelta(hours=i + 1), agent=fc.ASSISTANT_AGENT,
                 stop="answered")
        for i in range(2):
            _job(conn, f"t-new{i}", v, APPLIED + timedelta(hours=i + 1), agent=fc.ASSISTANT_AGENT,
                 stop="answered", tools=[("web_search", "tool cap reached", False)])
    (res,) = fast_check(U, deps(pool), now=NOW)
    assert res.change_id == cid and res.rolled_back
    assert [(f.agent, f.signature) for f in res.failures] == [
        ("assistant", fc.TOOL_CAP)]


def test_refusal_kinds_and_run_signatures():
    assert refusal_kind("没有段 s7：先用 write_section 写这一段") == "没有段"
    assert refusal_kind("组件库里没有 quiz_x；可用：a, b") == "组件库里没有"
    assert refusal_kind("「牛顿定律」不在大纲里") == "不在大纲里"
    assert refusal_kind("render check failed") == "render check failed"
    run = {"agent": "deep.write", "stop": "no_tool_calls", "error": "ProviderOutputError",
           "job_failure": "no_draft", "tool_errors": [
               {"name": "place_component", "error": "boom", "crash": True},
               {"name": "nope", "error": "unknown tool nope", "crash": False}]}
    assert unit_failures(run, "deep") == [
        "job:no_draft", "error:ProviderOutputError", "stop:no_tool_calls",
        "tool:place_component:crash", "tool:nope:unknown"]
    assert unit_failures({**run, "error": "ProviderTimeoutError", "job_failure": "llm_timeout",
                          "tool_errors": []}, "deep") == ["stop:no_tool_calls"]


def test_the_assistant_agent_name_is_the_loops():
    from evowiki.assistant.loop import PURPOSE
    assert fc.ASSISTANT_AGENT == PURPOSE


def test_the_cycle_row_has_observations_keys_and_the_failures():
    from evowiki.harness.round import fast_row
    f = fc.Failure("deep", fc.TOOL_CAP, ("r1", "r2"), 2, 3, 2)
    row = fast_row(fc.FastResult("c1", True, (f,), "harmful", fc.ROLLED_BACK))
    assert row == {"change_id": "h_c1", "verdict": "harmful", "deltas": {}, "point": False,
                   "rolled_back": True, "lesson": "harmful", "note": fc.ROLLED_BACK,
                   "dropped": {}, "failures": [f.dump()]}
