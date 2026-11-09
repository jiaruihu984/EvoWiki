"""The unified guard also compares a harness change with RSI's own and the default rules (RSI-B's
review conflict / duplicate check, retired 2026-10-29): opt-in (`rsi_rules`, the harness guard
only; Memory's consolidation keeps comparing with the user's rules and decisions alone), the items
the diff edits or removes left out. Fake judges and a scripted gateway: no model call."""

from collections.abc import Sequence

from evowiki.harness.diff import KeyOp, PackDiff
from evowiki.rules.decisions import Decisions
from evowiki.rules.guard import (
    CONFLICT,
    CONFLICT_OWN,
    DUP_OWN,
    Candidate,
    _assistant_key,
    against_of,
    check_rule,
    edited_items,
    harness_candidates,
)
from evowiki.teach.contracts import ParamPatchOp, SpecItem, SpecPatchOp, TeachingSpec
from evowiki.teach.rule_judge import PURPOSE, Against, GatewayRuleJudge, Verdict
from tests.wikiapp.script import Script

OWN = SpecItem(item_id="r1", text="每节先给一个生活里的例子", origin="rsi_h", authority="rsi")
DEFAULT = SpecItem(item_id="d04", text="新词第一次出现时先解释", origin="default", authority="rsi")
GUARD = SpecItem(item_id="d12", text="化简要说出来", origin="default", authority="rsi",
                 protected=True)
MINE = SpecItem(item_id="si1", text="必要时先举例", origin="user_chat", authority="user")
SPEC = TeachingSpec(version=4, items=(OWN, DEFAULT, GUARD, MINE))


class FakeJudge:
    def __init__(self, verdict: str, against: tuple[str, ...]):
        self.verdict, self.against = verdict, against
        self.shown: list[tuple[str, ...]] = []

    def conflict(self, candidate: str, scope: str, domain: str | None,
                 against: Sequence[Against]) -> Verdict:
        refs = tuple(a.ref for a in against)
        self.shown.append(refs)
        return Verdict(self.verdict, self.against, "因为", "fake/m", refs)  # type: ignore[arg-type]


def test_an_assistant_prompt_is_not_held_to_the_rules_for_writing_pages():
    """Round 2ec34dc3 of 2026-11-04 was blocked on d01 / d09, rules the assistant does not write under."""
    assert _assistant_key("H1.assistant") and _assistant_key("H2.assistant.resolve_memory_conflict")
    assert not _assistant_key("H1.write") and not _assistant_key("H7 add_item r9")


def test_the_rsi_pool_is_opt_in_and_leaves_out_the_edited_items():
    plain = {a.ref for a in against_of(SPEC, Decisions())}
    assert plain == {"d12@1", "si1@1"}                        # Memory's pool, as before
    pool = {a.ref: a.kind for a in against_of(SPEC, Decisions(), rsi_rules=True,
                                              exclude={"r1"})}
    assert pool == {"d12@1": "rule", "si1@1": "rule", "d04@1": "rsi_rule"}


def test_a_near_duplicate_of_an_rsi_rule_blocks_without_a_call():
    judge = FakeJudge("no_conflict", ())
    r = check_rule(SPEC, Decisions(), Candidate("每节先给一个生活里的例子。", label="H7 add_item"),
                   judge=judge, rsi_rules=True)
    assert (r.outcome, r.reason) == ("block", DUP_OWN) and judge.shown == []
    # the same text as the rule being edited is that edit, not a duplicate
    r = check_rule(SPEC, Decisions(), Candidate("每节先给一个生活里的例子。"), judge=judge,
                   rsi_rules=True, exclude={"r1"})
    assert r.outcome == "pass"
    # without the RSI pool (Memory) it is not compared at all
    assert check_rule(SPEC, Decisions(), Candidate("每节先给一个生活里的例子。"),
                      judge=judge).outcome == "pass"


def test_the_judges_conflict_and_same_on_an_rsi_rule_block():
    r = check_rule(SPEC, Decisions(), Candidate("新词先不解释，用到再说"),
                   judge=FakeJudge("conflict", ("d04@1",)), rsi_rules=True)
    assert (r.outcome, r.reason) == ("block", CONFLICT_OWN)
    r = check_rule(SPEC, Decisions(), Candidate("新术语头一回出现就先讲它是什么"),
                   judge=FakeJudge("same", ("d04@1",)), rsi_rules=True)
    assert (r.outcome, r.reason, r.same_as) == ("block", DUP_OWN, ())
    # a conflict with the user's rule keeps its reason; a restatement of his rule still passes
    r = check_rule(SPEC, Decisions(), Candidate("每页都要先举例"),
                   judge=FakeJudge("conflict", ("si1@1", "d04@1")), rsi_rules=True)
    assert r.reason == CONFLICT
    r = check_rule(SPEC, Decisions(), Candidate("需要的时候先举个例子"),
                   judge=FakeJudge("same", ("si1@1",)), rsi_rules=True)
    assert (r.outcome, r.same_as) == ("pass", ("si1",))


def test_the_judge_prompt_labels_rsi_rules():
    s = Script().push(PURPOSE, {"verdict": "no_conflict", "why": "不同的事"})
    r = check_rule(SPEC, Decisions(), Candidate("确认题只考一个点"),
                   judge=GatewayRuleJudge(s.gateway(), subject="u1"), rsi_rules=True)
    assert r.outcome == "pass"
    (req,) = s.requests(PURPOSE)
    assert req.prompt_version == "rule-conflict@v3"
    assert "[d04@1]（系统默认或自动改进的规则" in req.prompt and "[r1@1]" in req.prompt


def test_harness_candidates_skip_set_param_and_edited_items_name_the_touched_rules():
    diff = PackDiff(ops=(KeyOp("H4.deep.max_tool_calls", 30),), spec_ops=(
        ParamPatchOp(track="B", path="analogy_level", value=2),
        SpecPatchOp(op="edit_item", item_id="r1", text="每节先给一个能动手的例子"),
        SpecPatchOp(op="remove_item", item_id="d04")))
    assert [c.label for c in harness_candidates(diff)] == ["H7 edit_item r1"]
    assert edited_items(diff) == {"r1", "d04"}
