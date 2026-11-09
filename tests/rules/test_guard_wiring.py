"""The rule guard in the RSI round (contract §2, §4.1): the harness round never deploys on its own
a change the guard blocks or is unsure about. Block → the round fails; unsure → the user is asked.
The guard's records go into the harness round's decision. Fake models only."""

from dataclasses import replace

from evowiki.harness.diff import KeyOp, PackDiff
from evowiki.rules.decisions import Decisions
from evowiki.rules.guard import GuardRecord, GuardResult, harness_candidates
from evowiki.teach.contracts import SpecPatchOp
from tests.teach.fake_approvals import FakeApprovals

BLOCK = GuardResult("block", "冲突", (GuardRecord("edit_item s2", "block", "冲突", "fake/j"),))
ASK = GuardResult("confirm", "拿不准", (GuardRecord("edit_item s2", "confirm", "拿不准", "fake/j"),))


def test_harness_candidates_are_the_h7_texts_and_the_h1_h2_h8_values():
    diff = PackDiff(ops=(KeyOp("H1.write", "每页先举例"), KeyOp("H4.deep.max_tool_calls", 90),
                         KeyOp("H8.deep.evoui_examples", [{"name": "x"}]),
                         KeyOp("H2.deep.read_k", None)),
                    spec_ops=(SpecPatchOp(op="add_item", item_id="h1", text="先给结论"),
                              SpecPatchOp(op="remove_item", item_id="d01")))
    got = [(c.label, c.text) for c in harness_candidates(diff)]
    assert got == [("H7 add_item h1", "先给结论"), ("H1.write", "每页先举例"),
                   ("H8.deep.evoui_examples", '[{"name": "x"}]')]


H1_DIFF = PackDiff(ops=(KeyOp("H1.write", "每页都要先举例"),))


def test_h_round_a_block_fails(pool):
    from tests.harness.test_round import Env, FakeImprover
    env = Env(pool, improver=FakeImprover(H1_DIFF))
    env.deps = replace(env.deps, guard=lambda u, d: BLOCK)
    r = env.run()
    assert r.status == "failed" and "冲突" in r.reason
    assert "H1.write" not in env.pack().values
    (row,) = env.rounds()
    assert row.decision["rules"][0]["verdict"] == "block"


def test_h_round_unsure_asks(pool):
    from tests.harness.test_round import Env, FakeImprover
    ap = FakeApprovals(ask=False)
    env = Env(pool, improver=FakeImprover(H1_DIFF), approvals=ap)
    env.deps = replace(env.deps, guard=lambda u, d: ASK)
    r = env.run()
    assert r.status == "asked" and "H1.write" not in env.pack().values


def test_decisions_feed_the_guard_from_the_state():
    assert Decisions().active() == ()
