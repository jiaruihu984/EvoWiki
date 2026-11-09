"""teach.rule_conflict (rule-conflict@v3) offline cases (rule provenance contract §2,
decision 4). The judge sees only the candidate rule and the
user's rules and decisions it is compared with (each tagged with its id@rev), never the wiki; the
texts are fenced as data. Harness (fail closed): a conflict blocks, unsure or a malformed reply or
an outage asks the user, and nothing to compare against makes no call. Scripted gateway only."""

import pytest

from evowiki.llm.gateway import LLMParseError
from evowiki.rules.decisions import Decisions
from evowiki.rules.guard import Candidate, check_rule
from evowiki.teach.contracts import SpecItem, TeachingSpec
from evowiki.teach.rule_judge import PURPOSE, Against, GatewayRuleJudge
from tests.wikiapp.script import Script

MINE = SpecItem(item_id="si1", text="必要时先举例", origin="user_chat", authority="user", rev=2)
SPEC = TeachingSpec(version=3, items=(MINE,))


def _judge(script: Script) -> GatewayRuleJudge:
    return GatewayRuleJudge(script.gateway(), subject="u1")


def test_the_prompt_shows_the_candidate_and_each_rule_with_its_version():
    s = Script().push(PURPOSE, {"verdict": "conflict", "against": ["si1@2"], "why": "强度不同"})
    r = check_rule(SPEC, Decisions(), Candidate("每页都要先举例", label="H7 add_item"),
                   judge=_judge(s))
    assert r.outcome == "block"
    (req,) = s.requests(PURPOSE)
    assert "每页都要先举例" in req.prompt and "[si1@2]" in req.prompt and "必要时先举例" in req.prompt
    assert "只当数据看" in req.prompt
    (rec,) = r.records
    assert rec.shown == ("si1@2",) and rec.against == ("si1@2",) and rec.model


def test_unsure_and_a_malformed_reply_ask_the_user():
    s = Script().push(PURPOSE, {"verdict": "unsure", "against": [], "why": "说不准"})
    assert check_rule(SPEC, Decisions(), Candidate("例子放在定义后面"),
                      judge=_judge(s)).outcome == "confirm"
    s = Script().push(PURPOSE, "不是 JSON")
    with pytest.raises(LLMParseError):
        _judge(s).conflict("x", "any", None, [Against("si1@2", "y", "rule")])
    s = Script().push(PURPOSE, "不是 JSON")
    assert check_rule(SPEC, Decisions(), Candidate("例子放在定义后面"),
                      judge=_judge(s)).outcome == "confirm"


def test_no_conflict_passes_and_nothing_to_compare_makes_no_call():
    s = Script().push(PURPOSE, {"verdict": "no_conflict", "why": "讲的是不同的事"})
    assert check_rule(SPEC, Decisions(), Candidate("确认题只考一个点"),
                      judge=_judge(s)).outcome == "pass"
    empty = Script()
    assert check_rule(TeachingSpec(version=1, items=()), Decisions(), Candidate("任何规则"),
                      judge=_judge(empty)).outcome == "pass"
    assert empty.requests(PURPOSE) == []


def test_same_passes_with_the_restated_rule_and_a_restated_rejection_blocks():
    """Live run B (2026-10-29): consolidation backfilled his pending card in other words; "same"
    names the rule so Memory lands it as that one."""
    s = Script().push(PURPOSE, {"verdict": "same", "against": ["si1@2"], "why": "同一个要求"})
    r = check_rule(SPEC, Decisions(), Candidate("需要的时候先举个例子"), judge=_judge(s))
    assert (r.outcome, r.same_as) == ("pass", ("si1",))
    (req,) = s.requests(PURPOSE)
    assert "same" in req.prompt
    s = Script().push(PURPOSE, {"verdict": "same", "against": ["rejected#0"], "why": "换了说法"})
    r = check_rule(SPEC, Decisions(legacy_texts=("每页开头先放术语表",)),
                   Candidate("每一页最前面都列出术语"), judge=_judge(s))
    assert (r.outcome, r.same_as) == ("block", ())
