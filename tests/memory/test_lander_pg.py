import pytest

"""Task 10 step 6 over S3's real `land_preference` (Postgres: the wiki, the ledger and its
executors): a backfilled preference is pending (in no page's rules) until he confirms its card,
then active; the same candidate retried never makes a second card; a backfilled fact follows its
ledger row. Scripted consolidation model, no paid call."""

from evowiki.memory.consolidate import retry_pending_landings
from evowiki.memory.contracts import EvidenceRef, MemoryScope
from evowiki.memory.lander import AutoRuleGuard, NullPreferenceLander, S3PreferenceLander
from evowiki.memory.search import authority_status
from evowiki.teach.rule_judge import Verdict
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.preferences import FACT_KNOWN, NOT_GLOBAL, land_preference
from evowiki.wikiapp.teachspec import page_teach
from tests.assistant.test_stated_preferences import ctx, own_items, prefs_env, remember
from tests.memory.test_consolidate import PREF, World, cand, ev
from tests.wikiapp.test_review_flow import LATER

RULE = "数学题先解释符号"
WORDS = (EvidenceRef(message_id="m3", session_id="s1", role="user",
                     quote="以后数学题都先解释符号"),)


def _status(env, item):
    def draft(aid):
        row = env.ledger.get("u1", aid)
        return row.status if row is not None else None
    return authority_status(item, env.state(), {}, draft)


@pytest.fixture(autouse=True)
def _any_quote_backs(monkeypatch):
    """These are about landing: the candidates quote one fixture message, whatever they say."""
    monkeypatch.setattr("evowiki.memory.consolidate.BACKED_CHARS", 0.0)


def test_backfill_is_pending_until_confirmed_then_active(pool, tmp_path):
    env = prefs_env(pool)
    lander = S3PreferenceLander(land_preference, env.repo, lambda: LATER, env.ids)
    w = World(tmp_path)
    r = w.run([PREF], lander=lander)
    [it] = r.items_written
    row = env.ledger.get("u1", it.pending_action_id)
    assert (row.kind, row.status) == ("rule", "proposed")
    [spec_item] = [i for i in tspec.effective_spec(env.state()).items if i.text == RULE]
    assert it.spec_item_id == spec_item.item_id
    assert (spec_item.origin, spec_item.authority) == ("memory_backfill", "pending")
    assert RULE not in page_teach(env.state(), env.pid).guidelines
    assert _status(env, it) == "pending" and it.status is None
    env.ledger.approve("u1", row.action_id)                         # his 确认
    assert env.ledger.get("u1", row.action_id).status == "executed"
    assert RULE in page_teach(env.state(), env.pid).guidelines
    assert _status(env, it) == "active"


def test_the_same_candidate_retried_makes_one_card(pool, tmp_path):
    env = prefs_env(pool)
    lander = S3PreferenceLander(land_preference, env.repo, lambda: LATER, env.ids)
    w = World(tmp_path)
    w.run([PREF], lander=NullPreferenceLander())                   # waited for S3
    key = w.entries()[0]["pending_landing"][0]["candidate_key"]
    first = land_preference(env.repo, env.repo.load("u1"), kind="teaching", text=RULE,
                            provenance="explicit_history",
                            evidence=WORDS, scope=MemoryScope(scope_type="global"),
                            session_id=None,
                            idempotency_key=key, now=LATER, new_id=env.ids)
    assert first.status == "pending"                                # landed, not recorded yet
    assert retry_pending_landings(w.mem, w.sessions, lander, "u1", LATER, env.ids) == 1
    assert retry_pending_landings(w.mem, w.sessions, lander, "u1", LATER, env.ids) == 0
    [it] = w.mem.items("u1", kinds=["preference"])
    assert it.pending_action_id == first.pending_action_id
    assert len(env.rows()) == 1
    assert [i.text for i in tspec.effective_spec(env.state()).items].count(RULE) == 1


def test_one_message_two_facts_two_cards_and_a_known_fact_no_card(pool, tmp_path):
    env = prefs_env(pool)
    lander = S3PreferenceLander(land_preference, env.repo, lambda: LATER, env.ids)
    words = ev("m3", quote="以后数学题都先解释符号")
    r = World(tmp_path).run([cand("about_me", "每天只有半小时", words),
                             cand("about_me", "机械工程研究生，平时主要用 MATLAB", words),
                             cand("about_me", "用户是机械工程研究生，平时主要用 MATLAB", words)], lander=lander)
    assert len({it.pending_action_id for it in r.items_written}) == 2
    assert [reason for _, reason in r.dropped] == [f"refused:{FACT_KNOWN}"]


def test_a_backfilled_fact_follows_its_card(pool, tmp_path):
    env = prefs_env(pool)
    lander = S3PreferenceLander(land_preference, env.repo, lambda: LATER, env.ids)
    fact = cand("about_me", "每天只有半小时", ev("m3", quote="以后数学题都先解释符号"))
    [it] = World(tmp_path).run([fact], lander=lander).items_written
    assert it.spec_item_id is None and it.profile_ref is None
    assert env.ledger.get("u1", it.pending_action_id).kind == "profile_fact"
    assert _status(env, it) == "pending"
    env.ledger.approve("u1", it.pending_action_id)
    assert _status(env, it) == "active"



class SameJudge:
    """The rule judge saying the candidate restates the rule with this text (live run B,
    2026-10-29: the turn's card and consolidation's reworded backfill)."""

    def __init__(self, text):
        self.text, self.seen = text, []

    def conflict(self, candidate, scope, domain, against):
        self.seen.append(candidate)
        hit = tuple(a.ref for a in against if a.text == self.text)
        return Verdict("same", hit, "同一个要求", "", tuple(a.ref for a in against))


def test_the_turns_card_and_a_reworded_backfill_make_one_card(pool, tmp_path):
    """Live run B: the turn's remember_preference drafted a card; consolidation then backfilled
    the same preference in other words. The judge's "same" lands it as that card, not a second."""
    env = prefs_env(pool)
    said = "以后数学题都先解释符号"
    drafted = "讲数学题时，先把每个符号是什么意思讲清楚"
    turn = remember(env, ctx(said, mid="m3"), quote=said, text=drafted)
    assert turn.error is None, turn.content
    [card] = env.rows()
    assert (card.kind, card.status) == ("rule", "proposed")
    judge = SameJudge(drafted)
    backfill = cand("preference", "数学题都要先解释清楚符号，再讲怎么算", ev("m3", quote=said))
    r = World(tmp_path).run([backfill],
                            lander=S3PreferenceLander(land_preference, env.repo, lambda: LATER,
                                                      env.ids),
                            guard=AutoRuleGuard(env.repo, lambda uid: judge))
    assert judge.seen == ["数学题都要先解释清楚符号，再讲怎么算"]
    [it] = r.items_written
    [item] = own_items(env)
    assert (it.pending_action_id, it.spec_item_id) == (card.action_id, item.item_id)
    assert [row.action_id for row in env.rows()] == [card.action_id]    # one card


def test_a_backfill_of_a_rule_in_force_returns_that_rule(pool, tmp_path):
    env = prefs_env(pool)
    said = "以后数学题都先解释符号"
    assert remember(env, ctx(said, mid="m3"), quote=said, text=said).error is None
    [item] = own_items(env)
    assert item.authority == "user"
    got = land_preference(env.repo, env.repo.load("u1"), kind="teaching",
                          text="数学题都要先解释清楚符号", provenance="explicit_history",
                          evidence=WORDS, scope=MemoryScope(scope_type="global"),
                          session_id=None, idempotency_key="k9", same_as=item.item_id,
                          now=LATER, new_id=env.ids)
    assert (got.status, got.item_id, got.action_id) == ("active", item.item_id, None)
    assert len(env.rows()) == 1 and len(own_items(env)) == 1


def test_a_backfill_judged_same_as_a_rule_in_force_links_to_it(pool, tmp_path):
    """Live run D: the turn's own words put the rule in force; consolidation's backfill, judged
    "same", is that rule (spec_item_id; no card) and history reads it as active."""
    env = prefs_env(pool)
    said = "以后数学题都先解释符号"
    assert remember(env, ctx(said, mid="m3"), quote=said, text=said).error is None
    [rule] = own_items(env)
    judge = SameJudge(said)
    backfill = cand("preference", "数学题都先解释符号，再讲怎么算", ev("m3", quote=said))
    r = World(tmp_path).run([backfill],
                            lander=S3PreferenceLander(land_preference, env.repo, lambda: LATER,
                                                      env.ids),
                            guard=AutoRuleGuard(env.repo, lambda uid: judge))
    [it] = r.items_written
    assert (it.spec_item_id, it.pending_action_id) == (rule.item_id, None)
    assert _status(env, it) == "active" and len(env.rows()) == 1


def test_a_page_scoped_preference_stuck_on_land_failed_is_settled_on_retry(pool, tmp_path):
    """Live run C: the lander built MemoryScope(page, None) and failed forever. It now takes the
    session's page; S3 keeps no page rule, so the retry settles it as refused."""
    env = prefs_env(pool)
    w = World(tmp_path)
    page_pref = cand("preference", "讲方差时用具体数字的例子", ev("m3", quote="以后数学题都先解释符号"),
                     scope_type="page")
    w.run([page_pref], lander=NullPreferenceLander())                # waiting, page id kept
    [waiting] = w.entries()[0]["pending_landing"]
    assert waiting["page_id"] == "p1" and waiting["candidate"]["scope_id"] is None
    lander = S3PreferenceLander(land_preference, env.repo, lambda: LATER, env.ids)
    assert retry_pending_landings(w.mem, w.sessions, lander, "u1", LATER, env.ids) == 0
    assert w.entries()[-1]["refused"] == [[waiting["candidate_key"], NOT_GLOBAL]]
    assert retry_pending_landings(w.mem, w.sessions, lander, "u1", LATER, env.ids) == 0
    assert len(w.entries()) == 2                                      # settled: not tried again
