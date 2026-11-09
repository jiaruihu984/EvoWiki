"""remember_preference asks the rule judge (verdict "same", `teach.rule_judge`) when his words
now are no near-duplicate of a rule or card he has: one preference stays one card. Consolidation
backfilled 「讲公式先举个具体例子」 as a pending card; a later turn he says 「讲数学时先给我看个
例子再写公式」 → that card is his yes (confirmed as a card click, actor user), no second card; a
rule already in force → 「这条已经在用了」; a failed judge → the rule lands as new (logged).
Real Postgres and ledger, fake judges: no model call."""

import logging
from dataclasses import replace

from evowiki.assistant.tools.actions import ALREADY_IN_FORCE, RememberPreferenceTool
from evowiki.memory.contracts import EvidenceRef, MemoryScope
from evowiki.teach.rule_judge import Verdict
from evowiki.wikiapp.preferences import land_preference
from tests.assistant.test_stated_preferences import ctx, env, own_items  # noqa: F401
from tests.wikiapp.test_review_flow import LATER

BACKFILL = "讲公式先举个具体例子"
SAID_NOW = "讲数学时先给我看个例子再写公式"


class SameAs:
    """Says the candidate restates the rule with `text` (none there: no_conflict)."""

    def __init__(self, text):
        self.text, self.seen = text, []

    def conflict(self, candidate, scope, domain, against):
        self.seen.append((candidate, tuple(a.ref for a in against)))
        hit = tuple(a.ref for a in against if a.text == self.text)
        return Verdict("same" if hit else "no_conflict", hit, "同一个要求", "",
                       tuple(a.ref for a in against))


class Broken:
    def conflict(self, *a):
        raise RuntimeError("judge down")


def _tool(env, judge):  # noqa: F811
    return RememberPreferenceTool(replace(env.deps, rule_judge=lambda uid: judge))


def _say(env, judge, words=SAID_NOW):  # noqa: F811
    return _tool(env, judge).run({"applies": "lasting", "quote": words, "text": words},
                                 ctx(words, mid="m7"))


def _backfill(env):  # noqa: F811
    ev = (EvidenceRef(message_id="m1", session_id="s0", role="user", quote="以后讲公式先举个具体例子"),)
    got = land_preference(env.repo, env.repo.load("u1"), kind="teaching", text=BACKFILL,
                          provenance="explicit_history", evidence=ev,
                          scope=MemoryScope(scope_type="global"), session_id=None,
                          idempotency_key="cand1", now=LATER, new_id=env.ids)
    assert got.status == "pending"
    return got


def test_his_words_restating_a_pending_card_confirm_it_and_make_no_second(env):  # noqa: F811
    card = _backfill(env)
    judge = SameAs(BACKFILL)
    res = _say(env, judge)
    assert res.error is None, res.content
    assert "用的是已有的那条" in res.content and card.item_id in res.content
    assert [c for c, _ in judge.seen] == [SAID_NOW]
    [item] = own_items(env)
    assert (item.item_id, item.authority) == (card.item_id, "user")       # his now, one rule
    assert [r.action_id for r in env.rows()] == [card.pending_action_id]   # no second card
    assert env.ledger.get("u1", card.pending_action_id).status == "executed"


def test_his_words_restating_a_rule_in_force_write_nothing(env):  # noqa: F811
    first = _say(env, SameAs("nothing"), words="以后讲公式先举个具体例子")
    assert first.error is None, first.content
    res = _say(env, SameAs("以后讲公式先举个具体例子"))
    assert res.error is None and ALREADY_IN_FORCE in res.content
    assert len(own_items(env)) == 1 and len(env.rows()) == 1


def test_a_different_rule_lands_as_new(env):  # noqa: F811
    _backfill(env)
    res = _say(env, SameAs("别的规则"))
    assert res.error is None and "用的是已有的那条" not in res.content
    assert len(own_items(env)) == 2


def test_a_failed_judge_lands_the_rule_as_new_and_logs(env, caplog):  # noqa: F811
    _backfill(env)
    with caplog.at_level(logging.WARNING, logger="evowiki.wikiapp.preferences"):
        res = _say(env, Broken())
    assert res.error is None, res.content
    assert len(own_items(env)) == 2 and "rule judge failed" in caplog.text
