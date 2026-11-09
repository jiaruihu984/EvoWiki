"""B5c: a strategy_negative trigger runs one RSI cycle of its own (spec 2026-10-15 §2.2 / §2.3)."""

from datetime import timedelta

import pytest

from evowiki.bot import pipeline
from evowiki.bot.pipeline import dispatch, rsi_summary, run_rsi, undispatched
from evowiki.store import bot as store
from tests.bot.test_pipeline import NOW, get, new_id, put, repo, trig  # noqa: F401 (fixture)

pytestmark = pytest.mark.usefixtures("repo")


def strategy(tid="t1", **payload):
    return trig(tid, kind="strategy_negative", route="rsi", dedup="strategy_negative",
                payload={"signals": ["s1", "s2", "s3"], "written_only": False, **payload})


class Runner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, user_id, trigger_id):
        self.calls.append((user_id, trigger_id))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


RAN = {"status": "ran", "a": "no_cluster", "b": "personal_accepted pb_1", "patch_ids": ["pb_1"]}


def test_dispatch_waits_for_the_rsi_run(pool):
    put(pool, strategy())
    d = dispatch(pool, "t1", now=NOW, new_id=new_id)
    assert (d.outcome, d.status, d.suggestion_id, d.rsi_attempt) == (
        "pass", "awaiting_rsi", None, 0)


def test_written_feedback_only_is_handed_off_at_once(pool):
    """Written page feedback already ran its own RSI-B round when it was sent."""
    put(pool, strategy(written_only=True))
    d = dispatch(pool, "t1", now=NOW, new_id=new_id)
    assert (d.outcome, d.status) == ("pass", "handed_off")
    assert get(pool, "t1").gate_detail == pipeline.WRITTEN_DETAIL


def test_a_run_hands_off_with_its_one_line_result(pool):
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    runner = Runner(RAN)
    out = run_rsi(pool, "t1", runner, now=NOW, run_id="bot:rsi:t1:0")
    assert runner.calls == [("u1", "t1")]
    t = get(pool, "t1")
    assert (out.status, t.status, t.run_id) == ("handed_off", "handed_off", "bot:rsi:t1:0")
    assert t.gate_detail == "改了一条讲解规则"
    # a second run of the same trigger does nothing (the cycle ran once)
    again = run_rsi(pool, "t1", runner, now=NOW, run_id="bot:rsi:t1:1")
    assert again.status == "handed_off" and not again.ran and len(runner.calls) == 1


def test_manual_cycle_setting_is_respected(pool):
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    run_rsi(pool, "t1", Runner({"status": "manual"}), now=NOW, run_id="r")
    t = get(pool, "t1")
    assert t.status == "handed_off" and t.gate_detail == pipeline.MANUAL_DETAIL


def test_the_weekly_cap_closes_the_trigger_without_a_retry(pool):
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    out = run_rsi(pool, "t1", Runner({"status": "not_due"}), now=NOW, run_id="r")
    t = get(pool, "t1")
    assert out.status == t.status == "handed_off" and t.gate_detail == pipeline.WEEKLY_DETAIL
    assert ("u1", "t1") not in undispatched(pool, now=NOW + timedelta(minutes=10))


def test_busy_defers_and_the_sweep_retries(pool):
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    out = run_rsi(pool, "t1", Runner({"status": "busy"}), now=NOW, run_id="bot:rsi:t1:0")
    t = get(pool, "t1")
    assert (out.status, t.status, t.gate_outcome, t.gate_reason) == ("new", "new", "defer", "busy")
    later = NOW + timedelta(minutes=10)
    assert ("u1", "t1") in undispatched(pool, now=later)
    with pool.connection() as conn:
        store.insert_run(conn, run_id="bot:rsi:t1:0", user_id="u1", kind="rsi",
                         trigger_id="t1", now=NOW)
    d = dispatch(pool, "t1", now=later, new_id=new_id)
    assert (d.status, d.rsi_attempt) == ("awaiting_rsi", 1)
    run_rsi(pool, "t1", Runner(RAN), now=later, run_id="bot:rsi:t1:1")
    assert get(pool, "t1").status == "handed_off"


def test_no_runner_fails_closed(pool):
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    out = run_rsi(pool, "t1", None, now=NOW, run_id="r")
    assert out.status == "failed" and get(pool, "t1").status == "failed"


def test_a_raising_runner_fails_the_trigger_and_raises(pool):
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    with pytest.raises(RuntimeError):
        run_rsi(pool, "t1", Runner(RuntimeError("boom")), now=NOW, run_id="r")
    assert get(pool, "t1").status == "failed"


def test_a_gate_error_fails_the_trigger_instead_of_handing_it_off(pool):
    """Live 2026-10-20: the Gate judge's request was an HTTP 400. That is not a rejection: the
    trigger is failed with the error line (the run is then recorded failed, failure streak)."""
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    res = {"status": "ran", "a": "no_cluster", "patch_ids": [],
           "b": "error: gate_error: judge: BadRequestError: output_config.format.schema"}
    out = run_rsi(pool, "t1", Runner(res), now=NOW, run_id="bot:rsi:t1:0")
    t = get(pool, "t1")
    assert (out.status, out.error, out.ran, t.status) == ("failed", True, False, "failed")
    assert t.gate_detail == pipeline.RSI_GATE_ERROR_DETAIL == "复核时模型调用失败，未修改，之后会重试"


def test_a_genuine_gate_rejection_is_still_handed_off(pool):
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    res = {"status": "ran", "a": "no_cluster", "b": "gate_failed pb_2", "patch_ids": ["pb_2"]}
    out = run_rsi(pool, "t1", Runner(res), now=NOW, run_id="bot:rsi:t1:0")
    assert (out.status, out.error) == ("handed_off", False)
    assert get(pool, "t1").gate_detail == "这条修改没有通过复核，没有采用"


@pytest.mark.parametrize(("result", "line"), [
    ({"b": "personal_accepted pb_1", "a": "no_cluster"}, "改了一条讲解规则"),
    ({"b": "awaiting_user pb_1", "a": "no_cluster"}, "有一条讲解规则的修改，待你在个性化里确认"),
    ({"b": "judge_unavailable: only one provider", "a": "no_cluster"},
     "没有第二个模型来复核，讲解规则没有改"),
    ({"b": "no_finding", "a": "no_cluster"}, "没找到要改的"),
    ({"b": "gate_failed pb_2", "a": "no_cluster"}, "这条修改没有通过复核，没有采用"),
    ({"b": "none", "a": "no_cluster"}, "没有可用的模型，没有改"),
    ({"b": "no_budget", "a": "no_cluster"}, "用量已达上限，没有改"),
    ({"b": "error: x", "a": "no_cluster"}, "模型调用失败，未修改，之后会重试"),
    ({"b": "error: gate_error: judge: BadRequestError: 400", "a": "no_cluster"},
     "复核时模型调用失败，未修改，之后会重试"),
    ({"b": "unusable_output: bad json", "a": "no_cluster"}, "模型返回的内容无法使用，未修改"),
    ({"b": "something_new", "a": "no_cluster"}, "状态未知（something_new）"),
    ({"b": "off", "a": "no_cluster"}, "讲解的自动改进没有打开，这次没有改"),
    ({"b": "off; observe error: RuntimeError", "a": "no_cluster"},
     "讲解的自动改进没有打开，这次没有改"),
    ({"b": "harness applied r1: 检查通过", "a": "no_cluster"}, "改了一条讲解规则或写页面的做法"),
    ({"b": "harness asked r1: 检查通过，等你批准", "a": "no_cluster"},
     "有一条讲解的修改，待你在个性化里确认"),
    ({"b": "harness thin r1: 问题样例不足", "a": "no_cluster"}, "反馈还不够多，这次没有改"),
    ({"b": "harness skipped: 还没到时间", "a": "no_cluster"}, "这次不需要改进，没有改"),
    ({"b": "harness error r1: x", "a": "no_cluster"}, "模型调用失败，未修改，之后会重试"),
    ({"b": "harness weird r1: x", "a": "no_cluster"}, "状态未知（harness weird r1: x）"),
    ({"b": "no_finding", "a": "accepted pa_1"}, "没找到要改的；调整了学习状态评估"),
    ({"b": "personal_accepted pb_1", "a": "awaiting_user pa_1"},
     "改了一条讲解规则；学习状态评估的调整，待你在个性化里确认"),
])
def test_rsi_summary(result, line):
    assert rsi_summary(result) == line


def test_a_harness_error_fails_the_trigger(pool):
    """The harness round is the one improvement round now: its provider error fails the trigger
    (failure streak) like a track-B error did."""
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    res = {"status": "ran", "a": "no_cluster", "patch_ids": [],
           "b": "harness error r1: BadRequestError"}
    out = run_rsi(pool, "t1", Runner(res), now=NOW, run_id="bot:rsi:t1:0")
    assert (out.status, out.error) == ("failed", True)


def test_a_re_dispatch_of_a_waiting_trigger_names_the_next_attempt(pool):
    """Review fix 4: never attempt 0 again once a run exists."""
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    with pool.connection() as conn:
        store.insert_run(conn, run_id="bot:rsi:t1:0", user_id="u1", kind="rsi",
                         trigger_id="t1", now=NOW)
    d = dispatch(pool, "t1", now=NOW, new_id=new_id)
    assert (d.outcome, d.status, d.rsi_attempt) == (None, "awaiting_rsi", 1)


def test_a_stuck_running_row_never_hides_a_waiting_trigger(pool):
    """Liveness is DBOS's (durable._rsi_live), so a `running` bot_run row left by a cancelled or
    crashed run cannot block the trigger forever."""
    put(pool, strategy())
    dispatch(pool, "t1", now=NOW, new_id=new_id)
    later = NOW + timedelta(hours=3)
    with pool.connection() as conn:
        store.insert_run(conn, run_id="bot:rsi:t1:0", user_id="u1", kind="rsi",
                         trigger_id="t1", now=NOW)
        assert [t.trigger_id for t in store.awaiting_rsi_triggers(conn, later)] == ["t1"]
        assert store.awaiting_rsi_triggers(conn, NOW) == []          # not old enough yet
