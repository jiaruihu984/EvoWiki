"""Deleting a session with S3's real preference steps (memory/forget.py `S3SpecForget`): a ticked
rule is revoked through the guidelines path in the delete's own commit, an unticked one stays in
force with its evidence redacted, a ticked fact leaves the profile. Postgres, the real wiki repo
and ledger; RSI's step is a fake; no model call."""

from itertools import count

import pytest

from evowiki.memory.forget import ForgetService, S3SpecForget
from evowiki.memory.tombstone import Ledger
from evowiki.profile.note import WHO, line_key
from evowiki.wikiapp.preferences import REDACTED
from tests.assistant.test_stated_preferences import LIKES, RULE, ctx, own_items, prefs_env, remember
from tests.memory.forget_world import FakeHarness
from tests.wikiapp.test_review_flow import LATER

pytestmark = pytest.mark.usefixtures("every_skill")


@pytest.fixture
def env(pool, tmp_path):
    e = prefs_env(pool)
    remember(e, ctx(LIKES), quote="我喜欢组件多一点的页面", text=RULE)
    e.tools["remember_about_me"].run({"applies": "lasting", "section": WHO, "quote": "我是护士",
                                      "text": "是护士"}, ctx("我是护士", mid="m2"))
    with pool.connection() as conn:
        conn.execute("insert into assistant_session (session_id, user_id, created_at) "
                     "values ('s1', 'u1', %s) on conflict do nothing", (LATER,))
        conn.execute("insert into assistant_entry (session_id, seq, entry_id, type, payload, "
                     "occurred_at) values ('s1', 1, 'e1', 'user', %s, %s)",
                     ('{"message_id": "m1", "text": "我喜欢组件多一点的页面"}', LATER))
    ids = count(1)
    e.forget = ForgetService(
        pool, Ledger(tmp_path / "l.jsonl"), harness=FakeHarness(),
        spec=S3SpecForget(e.repo, e.profile, pool, lambda p: f"{p}_f{next(ids)}"),
        clock=lambda: LATER, new_id=lambda p: f"{p}_g{next(ids)}")
    return e


def test_preview_lists_the_sessions_rule_and_fact(env):
    p = env.forget.preview("u1", "s1")
    assert [r.text for r in p.rules] == [RULE]
    assert [(f.section, f.text) for f in p.facts] == [(WHO, "是护士")]


def test_unticked_rule_stays_in_force_with_its_words_redacted(env):
    assert env.forget.forget_session("u1", "s1", requested_by="test").state == "committed"
    [item] = own_items(env)
    assert item.text == RULE and item.origin_ref == "deleted:s1/m1"
    assert env.repo.load("u1").state.item_evidence[item.item_id] == REDACTED
    assert env.profile.get("u1").fact_meta[line_key(WHO, "是护士")].evidence == REDACTED


def test_ticked_rule_and_fact_are_revoked_in_the_same_commit(env):
    rule = own_items(env)[0].item_id
    out = env.forget.forget_session("u1", "s1", revoke_rule_ids=[rule],
                                    revoke_fact_keys=[line_key(WHO, "是护士")],
                                    requested_by="test")
    assert out.state == "committed"
    assert own_items(env) == []
    assert "是护士" not in env.profile.get("u1").owned.get(WHO, ())


def test_a_failed_delete_revokes_nothing(env):
    env.forget._harness.fail = RuntimeError("rsi down")
    rule = own_items(env)[0].item_id
    out = env.forget.forget_session("u1", "s1", revoke_rule_ids=[rule], requested_by="test")
    assert out.state == "failed"
    assert [i.item_id for i in own_items(env)] == [rule]          # the rule commit rolled back
    assert "是护士" in env.profile.get("u1").owned.get(WHO, ())
    env.forget._harness.fail = None
    assert env.forget.run(out.request_id).state == "committed"     # the retry revokes it
    assert own_items(env) == []


def test_two_ticked_rules_are_revoked_in_one_version_and_the_delete_commits(env):
    """Two rules ticked at once: one spec version takes both out (commit_spec takes exactly
    previous + 1; one RemoveRule each used to step it twice and the delete never committed)."""
    assert remember(env, ctx("每页最后加一个小结", mid="m3"), quote="每页最后加一个小结",
                    text="每页最后加一个小结").error is None
    rules = [i.item_id for i in own_items(env)]
    assert len(rules) == 2
    out = env.forget.forget_session("u1", "s1", revoke_rule_ids=rules, requested_by="test")
    assert out.state == "committed", out
    assert own_items(env) == []
    decided = {d.rule_id: d.action for d in env.repo.load("u1").state.rule_decisions}
    assert all(decided[r] == "revoked" for r in rules)
