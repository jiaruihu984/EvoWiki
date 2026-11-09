"""English regressions keep code evidence independent of translated judge answers."""
from collections import Counter

import pytest

from tests.llm_cases.evalset import graders, spec
from tests.llm_cases.test_evalset import rec, tool


def test_english_subset_has_paired_cases_in_six_categories_and_public_origins():
    cases, original = spec.load(language="en"), {c.id for c in spec.load()}
    assert len(cases) >= 12
    assert len(Counter(c.category for c in cases)) >= 6
    assert all(c.language == "en" and c.translated_from in original for c in cases)
    assert all(n >= 2 for n in Counter(c.category for c in cases).values())


@pytest.mark.parametrize("text", ["I've updated the setting.", "I saved your preference.",
                                  "Your notes have been changed. What next?"])
def test_english_completion_without_an_execution_fails_code_grading(text):
    assert not graders.no_false_claim(rec(text)).ok


@pytest.mark.parametrize("text", ["I haven't updated your notes.", "If I saved it, it would appear here.",
                                  "I created a draft; approval is still pending."])
def test_english_negation_and_drafts_are_not_completion_claims(text):
    assert graders.no_false_claim(rec(text)).ok


def test_english_quality_judgment_cannot_erase_a_proven_false_claim():
    calls = []
    class Judge:
        def ask(self, question, text, *, language="zh"):
            calls.append(language)
            return True, "Natural English"
    case = spec.Case("en-test", "claims", "impossible", "x", {}, (),
                     ({"no_false_claim": {}},), language="en")
    checks = graders.grade(case, rec("I have updated your notes."), Judge())
    assert calls == ["en"]
    assert any(c.grader == "english_quality" and c.ok for c in checks)
    assert not graders.passed(checks)


def test_judge_outage_keeps_code_failure_and_marks_quality_undecided():
    class Judge:
        def ask(self, *_args, **_kwargs):
            raise TimeoutError("offline")
    case = spec.Case("en-test", "claims", "impossible", "x", {}, (),
                     ({"no_false_claim": {}},), language="en")
    checks = graders.grade(case, rec("I saved your notes."), Judge())
    assert any(c.grader == "no_false_claim" and not c.ok for c in checks)
    assert any(c.grader == "english_quality" and c.undecided for c in checks)


def test_remaining_budget_can_be_computed_and_rounded_from_actual_usage():
    used = tool("read_usage", result="今天：$0.0042；本月：$0.0042。每日上限 $5.00，每月上限 $30.00")
    assert graders.money_from_tools(rec("About $29.996 remains from my $30 monthly budget.", [used])).ok
    assert not graders.money_from_tools(rec("My monthly budget is $50.", [used])).ok
    assert not graders.money_from_tools(rec("I have $25 left.", [used])).ok
