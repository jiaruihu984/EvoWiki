"""A round asks the user whenever the composed diff has a key that needs approval (harness phase 3b
Task 3), whatever the user's `rsi` approval level; H10 changes carry the replay costs. Uses the
phase-2 round fakes of tests/harness/test_round.py (scripted improver, fake replays and judge, the
FakeApprovals ledger); nothing reaches a real provider."""

import pytest

from evowiki.harness import round as round_mod
from evowiki.harness.diff import KeyOp, PackDiff
from evowiki.harness.round import HarnessRounds, ledger_key
from evowiki.teach.approval import METHOD_KIND, RSI
from tests.harness.test_round import H1, NOW, Env, FakeImprover, FakeReplay, U
from tests.teach.fake_approvals import Effect, FakeApprovals

H3 = PackDiff(ops=(KeyOp("H3.deep.read_page", False),))
H10 = PackDiff(ops=(KeyOp("H10.deep.write.tier", "good"),))


def test_an_approval_key_is_asked_even_when_the_level_does_not_ask(pool):
    ap = FakeApprovals(ask=False)
    env = Env(pool, improver=FakeImprover(H3), approvals=ap)
    r = env.run()
    assert r.status == "asked" and r.change_id is not None
    assert env.pack().version == 0 and env.pack().values == {} and env.marks == []
    (ch,) = env.changes()
    assert ch["status"] == "asked"
    assert ch["cost_before"] is None and ch["cost_after"] is None      # not a cost key
    (p,) = ap.proposals
    assert (p["kind"], p["scope"], p["key"]) == (METHOD_KIND, RSI, ledger_key(U, r.change_id))
    assert p["content"]["kind"] == "H" and p["content"]["change_id"] == "h_" + r.change_id
    assert "写页面时不再查看已有的页面" in p["summary"], p["summary"]
    assert any("写页面时不再查看已有的页面" in str(ln) for ln in p["content"]["diff"]), p["content"]["diff"]

    effect = Effect()
    HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert effect.n == 1
    assert env.pack().values == {"H3.deep.read_page": False}
    (ch,) = env.changes()
    assert (ch["status"], ch["version"]) == ("applied", 1)


def test_an_h10_change_is_asked_with_the_replay_costs(pool):
    env = Env(pool, improver=FakeImprover(H10), approvals=FakeApprovals(ask=False),
              replay=FakeReplay(cost=(0.010, 0.006)))
    r = env.run()
    assert r.status == "asked"
    (ch,) = env.changes()
    assert ch["cost_before"] == pytest.approx(0.010) and ch["cost_after"] == pytest.approx(0.006)
    (p,) = env.deps.approvals.proposals
    assert "改为 质量优先" in p["summary"] and "少花 $0.004" in p["summary"], p["summary"]


def test_an_h1_change_still_applies_when_the_level_does_not_ask(pool):
    env = Env(pool, improver=FakeImprover(H1), approvals=FakeApprovals(ask=False))
    r = env.run()
    assert r.status == "applied" and env.deps.approvals.proposals == []
    assert env.pack().values["H1.write"] == H1.ops[0].value


def test_an_approval_key_without_a_ledger_fails_closed(pool):
    env = Env(pool, improver=FakeImprover(H3))          # approvals None
    r = env.run()
    assert (r.status, r.reason) == ("failed", round_mod.NO_LEDGER)
    assert env.changes() == [] and env.pack().values == {}
