"""The round's evidence gate on the real replay-pairing and judge code (scripted models): what the
improver never sees, what stops the spending, and that a passing change is only proposed."""

from dataclasses import replace
from datetime import timedelta

import pytest

from evowiki.harness.anchors import EVAL_SAMPLES
from evowiki.harness.diff import KeyOp, PackDiff
from evowiki.harness.evalset_redlines import (
    NO_BASELINE,
    NOT_FINISHED,
    Regression,
    compare_runs,
    confirmed,
    replicated,
    verdict,
)
from evowiki.harness.evidence import (
    EvidenceResult,
    _underpowered,
    aa_health,
    evaluate,
    held_out_split,
)
from evowiki.harness.leak import CaseWords
from evowiki.harness.offline.stats import DEV_LOW, CaseResult, decide
from tests.harness.test_round import H4, NOW, Env, FakeImprover, FakeReplay, caseset, prefer
from tests.teach.fake_approvals import FakeApprovals


def _evidence(pool, **kw):
    return Env(pool, evidence=evaluate, propose_only=True, approvals=FakeApprovals(ask=False),
               cases=caseset(n=5, anchors=1), **kw)


def _gate(env):
    return env.rounds()[0].decision["gate"]


def test_the_improver_never_sees_the_held_out_cases_and_they_are_judged(pool):
    env = _evidence(pool)
    r = env.run(manual=True)
    dev, held = held_out_split(caseset(n=5).problem)
    assert len(held) == 1
    assert r.status == "asked"
    held_signals = set(held[0].signal_ids)
    assert not held_signals & {s.signal_id for s in env.improver.seen[0].signals}
    assert env.improver.cases_seen[0].problem == dev
    ev = _gate(env)["evidence"]
    assert ev["held_verdict"]["passed"] and ev["dev_verdict"]["passed"]
    assert [c["id"] for c in ev["held_cases"]] == [c.case_id for c in held]
    replays = [c for c, _ in env.replay.calls]
    # k samples of each pack, plus the smoke replays
    assert replays.count(held[0].case_id) == 2 * EVAL_SAMPLES


def test_a_passing_change_is_only_proposed_whatever_the_level_says(pool):
    env = _evidence(pool)
    assert env.run(manual=True).status == "asked"
    assert not env.marks


def test_a_change_the_judge_prefers_the_current_pack_on_fails_with_a_lesson(pool):
    env = _evidence(pool, judge=prefer("现行"))
    r = env.run(manual=True)
    assert r.status == "failed" and "胜率下界" in r.reason
    ev = _gate(env)["evidence"]
    assert "held_verdict" not in ev                      # the held-out set was not spent
    assert ev["beside"]["losses"] > 0
    assert env.lessons()[0].verdict == "ineffective"


def _run(**cases):
    return {"trace": {"complete": True}, "case_hash": "h", "grader_hash": "g",
            "cases": {k: {"trials": v} for k, v in cases.items()}}


FFF, TTT, TTF = [False] * 3, [True] * 3, [True, True, False]


def test_restored_cases_pass_only_past_the_sign_test_and_one_flake_is_no_movement():
    three = compare_runs(_run(a=FFF, b=FFF, c=[False, False], d=TTT), _run(a=TTT, b=TTT, c=[True, True], d=TTT))
    assert three.restores and three.fixed == ("a", "b", "c") and three.gained == 8
    assert not compare_runs(_run(a=FFF, d=TTT), _run(a=TTT, d=TTT)).restores       # one case: too few to say
    flake = compare_runs(_run(a=TTF, b=TTF), _run(a=TTT, b=TTT))
    assert flake.fixed == () and not flake.restores


def test_a_case_that_really_moved_clears_the_bar_on_more_trials_and_a_flake_falls_back():
    asked = []

    def again(script):
        def run(arm, ids):
            asked.append((arm, tuple(ids)))
            return _run(a=script[arm])
        return run
    fixed = replicated(_run(a=[True, False, False], b=TTT), _run(a=TTT, b=TTT), again({"before": FFF, "after": TTT}))
    assert fixed.restores and fixed.fixed == ("a",) and asked[0] == ("before", ("a",))
    flake = replicated(_run(a=[True, False, False]), _run(a=TTT), again({"before": TTT, "after": [True, False, True]}))
    assert not flake.restores
    asked.clear()
    assert not replicated(_run(a=TTT), _run(a=TTT), again({})).restores and not asked       # nothing moved: nothing more spent


def test_a_broken_case_or_runs_that_cannot_be_compared_never_restore():
    assert not compare_runs(_run(a=FFF, b=FFF, c=FFF, d=TTT), _run(a=TTT, b=TTT, c=TTT, d=FFF)).restores
    other = {**_run(a=TTT), "grader_hash": "other"}
    assert compare_runs(_run(a=FFF), other).error == NO_BASELINE
    assert compare_runs({**_run(a=FFF), "trace": {"complete": False}}, _run(a=TTT)).error == NOT_FINISHED


@pytest.mark.parametrize(("judged", "regression", "status", "reason"), [
    ("fail", Regression(fixed=("a", "b"), gained=6, p=0.016), "asked", "修好了 2 条"),
    ("pass", Regression(broken=("c",)), "failed", "红线用例变差了：c"),
    ("fail", Regression(), "failed", "裁判的结论"),            # nothing moved: the judge's verdict
    ("pass", Regression(), "asked", "裁判的结论"),             # content only, nothing broken
])
def test_code_grading_outranks_the_judge_on_an_assistant_change_either_way(pool, judged, regression, status, reason):
    cs = caseset(n=5, anchors=0)
    env = Env(pool, evidence=lambda *a, **k: EvidenceResult(judged, ("裁判的结论",), 0, 0, 0, 0, {}),
              propose_only=True, approvals=FakeApprovals(ask=False),
              cases=replace(cs, problem=tuple(replace(c, kind="assistant") for c in cs.problem)),
              improver=FakeImprover(PackDiff((KeyOp("H2.assistant.open_review", "他要考就用它"),))),
              regression=lambda *a: regression)
    r = env.run(manual=True)
    assert r.status == status and reason in r.reason


def _results(*pairs):
    return [CaseResult(f"c{i}", False, p) for i, p in enumerate(pairs)]


def test_a_mean_above_even_with_too_few_cases_is_no_verdict_not_a_loss():
    good = decide(_results(("a", "a"), ("a", "a"), ("tie", "b"), ("tie", "a")))
    assert not good.passed and _underpowered(good, DEV_LOW)
    even = decide(_results(("b", "a"), ("tie", "tie"), ("a", "b"), ("tie", "tie")))
    assert not even.passed and not _underpowered(even, DEV_LOW)


def test_ties_are_not_evidence(pool):
    env = _evidence(pool, judge=prefer("没有这个词"))
    r = env.run(manual=True)
    assert r.status == "thin" and "胜率下界 0.50" in r.reason
    assert not env.lessons()


def _chat_quiz_env(pool, *, judge, regression, cand_actions=("open_review",), base_actions=()):
    """The assistant change of the fault injection: the current pack writes a quiz in the chat, the
    candidate calls the tool (or the other way round)."""
    cs = caseset(n=5, anchors=0)
    inner = FakeReplay()

    def acting(case, candidate, **kw):
        out = inner(case, candidate, **kw)
        return replace(out, actions=cand_actions if candidate.ops else base_actions)
    return Env(pool, evidence=evaluate, propose_only=True, approvals=FakeApprovals(ask=False), judge=judge,
               cases=replace(cs, problem=tuple(replace(c, kind="assistant") for c in cs.problem)),
               improver=FakeImprover(PackDiff((KeyOp("H2.assistant.open_review", "他要考就用它"),))),
               replay=acting, regression=regression)


RESTORED = Regression(fixed=("act-quiz", "ask-due-cards", "quiz-me"), gained=9, p=0.002)


def test_the_chat_quiz_cannot_beat_the_candidate_that_calls_the_tool(pool):
    """The judge prefers the current text; real turns say the candidate restored the task."""
    env = _chat_quiz_env(pool, judge=prefer("现行"), regression=lambda *a: RESTORED)
    r = env.run(manual=True)
    assert r.status == "asked" and "修好了 3 条" in r.reason
    gate = _gate(env)
    assert gate["layers"]["task"]["outcome"] == "restored"
    assert gate["layers"]["content"]["from"] == "replay"
    assert any(x.startswith("第一层") for x in gate["reasons"]) and any(x.startswith("第二层") for x in gate["reasons"])


def test_a_replay_that_acts_differently_proves_nothing_without_real_turns(pool):
    env = _chat_quiz_env(pool, judge=prefer("候选"), regression=None)
    r = env.run(manual=True)
    assert r.status == "thin" and "证据不足" in r.reason
    assert not env.lessons()


def test_fine_words_with_the_task_failing_do_not_win(pool):
    """The judge likes the candidate's text; real turns say it broke a case."""
    env = _chat_quiz_env(pool, judge=prefer("候选"), cand_actions=(), base_actions=("open_review",),
                         regression=lambda *a: Regression(broken=("act-quiz",)))
    r = env.run(manual=True)
    assert r.status == "failed" and "act-quiz" in r.reason


def test_fine_words_with_the_task_unmoved_and_other_actions_are_no_verdict(pool):
    env = _chat_quiz_env(pool, judge=prefer("候选"), cand_actions=(), base_actions=("open_review",),
                         regression=lambda *a: Regression())
    assert env.run(manual=True).status == "thin"


def test_both_did_the_task_and_the_worse_content_loses(pool):
    env = _chat_quiz_env(pool, judge=prefer("现行"), cand_actions=("open_review",), base_actions=("open_review",),
                         regression=lambda *a: Regression())
    assert env.run(manual=True).status == "failed"


def test_both_did_the_task_and_the_same_content_is_no_win(pool):
    same = _chat_quiz_env(pool, judge=prefer("没有这个词"), cand_actions=("open_review",),
                          base_actions=("open_review",), regression=lambda *a: Regression())
    assert same.run(manual=True).status == "thin"


def test_a_restored_task_with_clearly_worse_content_does_not_pass(pool):
    env = _chat_quiz_env(pool, judge=prefer("现行"), cand_actions=("open_review",), base_actions=("open_review",),
                         regression=lambda *a: RESTORED)
    assert env.run(manual=True).status == "failed"


def test_a_regression_that_did_not_finish_is_too_little_evidence_not_a_loss(pool):
    env = _chat_quiz_env(pool, judge=prefer("候选"), regression=lambda *a: Regression(error=NOT_FINISHED))
    r = env.run(manual=True)
    assert r.status == "thin" and NOT_FINISHED in r.reason and not env.lessons()


def test_a_case_only_the_candidate_cannot_write_fails_before_the_judge_compares(pool):
    dev, _ = held_out_split(caseset(n=5).problem)
    env = _evidence(pool, replay=FakeReplay(broken={dev[-1].case_id}))
    r = env.run(manual=True)
    assert r.status == "failed"


def test_a_diff_that_names_a_case_is_refused_by_code_before_any_evidence(pool):
    diff = PackDiff((KeyOp("H1.write", "讲「度与度分布」时先给数字。"),))
    env = Env(pool, evidence=evaluate, improver=FakeImprover(diff), cases=caseset(n=5))
    env.deps = replace(env.deps, words_of=lambda *_: CaseWords(titles=("度与度分布",)))
    r = env.run(manual=True)
    assert r.status == "failed" and "度与度分布" in r.reason
    assert "evidence" not in _gate(env)


def test_a_red_line_regression_vetoes_whatever_the_judge_would_say(pool):
    env = _evidence(pool, redlines=lambda *_: ["说做了没做-03"])
    r = env.run(manual=True)
    assert r.status == "failed" and "说做了没做-03" in r.reason
    assert _gate(env)["evidence"]["dev_verdict"]["passed"]       # the judge liked it: a veto all the same
    assert env.lessons()[0].verdict == "ineffective"


def test_a_change_the_judge_refuses_does_not_pay_for_the_red_lines(pool):
    asked = []
    env = _evidence(pool, judge=prefer("现行"), redlines=lambda *a: asked.append(a) or [])
    assert env.run(manual=True).status == "failed" and not asked


def test_the_veto_compares_with_the_baseline_of_the_same_test_and_fails_closed():
    base = {"case_hash": "c", "grader_hash": "g", "note": "baseline-1",
            "cases": {"a": {"trials": [True, True]}, "b": {"trials": [True, False]}}}
    run = {"case_hash": "c", "grader_hash": "g", "trace": {"complete": True},
           "cases": {"a": {"trials": [True, False]}, "b": {"trials": [False, False]}}}
    assert verdict(run, [base]) == ["a"]                         # b already failed: not new
    assert verdict({**run, "grader_hash": "other"}, [base]) == [NO_BASELINE]
    assert verdict({**run, "trace": {"complete": False}}, [base]) == [NOT_FINISHED]
    again = {"cases": {"a": {"trials": [True, True, True]}, "c": {"trials": [True, False]}}}
    assert confirmed(["a", "c", "d"], again) == ["c", "d"]        # a flake goes; no verdict stays


def test_a_scrambled_text_preferred_voids_the_round_before_the_candidate_is_written(pool):
    """A judge that always names the first text it is shown as a: the scrambled copy wins."""
    calls = []

    def always_second_order(model, req):
        calls.append(req)
        # names the altered copy in both orders when the text is reversed: it reads 乙 in (b, a)
        return '{"better": "甲", "quote": "", "reason": "x"}' if len(calls) % 2 else \
            '{"better": "乙", "quote": "", "reason": "x"}'
    env = _evidence(pool, judge=always_second_order)
    r = env.run(manual=True)
    assert r.status == "thin" and "打乱" in r.reason


def test_a_later_round_pools_the_aa_pairs_of_the_earlier_ones(pool):
    env = _evidence(pool, judge=prefer("没有这个词"))
    env.run(manual=True)
    env.run(manual=True, now=NOW + timedelta(days=8))
    aa = [r.decision["gate"]["evidence"]["aa"] for r in env.rounds()]
    assert sorted(a["pooled"] for a in aa) == [4, 8]


def test_aa_cannot_void_a_round_on_a_handful_of_pairs():
    assert aa_health(["a", "a", "a"])[0] is True
    assert aa_health(["a"] * 12)[0] is False
    assert aa_health(["a", "b"] * 6 + ["tie"])[0] is True


def test_no_held_out_case_is_thin_not_a_pass(pool):
    env = Env(pool, evidence=evaluate, improver=FakeImprover(H4), cases=caseset(n=3, anchors=0))
    env.cases = replace(env.cases, problem=env.cases.problem[:1])
    assert env.run(manual=True).status == "thin"


def test_the_cases_speak_to_the_feedback_by_his_words_and_the_tools_and_none_is_too_little_evidence():
    from pathlib import Path

    from evowiki.harness.evalset_relevant import Feedback, relevant
    root = Path(__file__).resolve().parents[2]
    quiz = relevant(root, Feedback(said=("出两道题考考我，度与度分布这页的",), tools=("open_review",)))
    assert "act-quiz" in quiz and "quiz-me-question-is-consistent" in quiz
    assert relevant(root, Feedback(said=("zzzz qqqq",))) == ()


def test_no_case_for_the_feedback_is_too_little_evidence_and_runs_nothing(pool):
    runs = []
    env = _chat_quiz_env(pool, judge=prefer("候选"), regression=lambda *a: runs.append(a) or RESTORED)
    env.deps = replace(env.deps, relevant=lambda fb: ())
    r = env.run(manual=True)
    assert r.status == "thin" and "没有对应这条反馈的用例" in r.reason and not runs


def test_one_correction_is_a_signal_and_three_of_a_kind_are_worth_a_round():
    from evowiki.harness.triage import TriageOut, worth_a_round

    class Gw:
        def __init__(self, largest):
            self.calls, self.largest = 0, largest

        def call(self, role, req, schema, *, account):
            self.calls += 1
            assert "只是数据" in req.prompt and schema is TriageOut and account.group == "rsi"
            if self.largest is None:
                raise RuntimeError("down")
            return TriageOut(largest=self.largest, what="题写在聊天里"), None
    two = Gw(3)
    assert not worth_a_round(["一", "二"], two, "u") and two.calls == 0    # nothing called below three
    assert worth_a_round(["一", "二", "三"], two, "u") and two.calls == 1
    assert not worth_a_round(["一", "二", "三"], Gw(2), "u")               # three corrections, no one kind
    assert not worth_a_round(["一", "二", "三"], Gw(None), "u")            # a failed call is no round
