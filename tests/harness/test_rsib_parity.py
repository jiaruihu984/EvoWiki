"""What the harness loop took over from the retired RSI-B (2026-10-29): `set_param` in H7 (the
RepParams values through the same apply / commit / undo path), the ban on re-proposing a change the
user rejected or undid or that was rolled back, the permission check at proposal time (and again at
commit and approval), and the marking of the pages a rule change left out of date. Fake improver,
replay and judge on a real `pool`; no model call."""

from dataclasses import replace

import pytest

from evowiki.harness.change_source import spec_ops_of, summary, undo_change
from evowiki.harness.diff import (
    KeyOp,
    PackDiff,
    agents_touched,
    complexity,
    diff_from_json,
    diff_to_json,
    op_key,
    refused_keys,
)
from evowiki.harness.improver import BANNED, ImproverRun
from evowiki.harness.pack import EMPTY as EMPTY_PACK
from evowiki.harness.round import RULE_LOCKED, STALE_SPEC, HarnessRounds
from evowiki.rules.guard import PASS, GuardResult
from evowiki.service.harness_defaults import defaults
from evowiki.store import harness_rsi
from evowiki.teach.approval import PendingStale
from evowiki.teach.contracts import ParamPatchOp, RepParams, SpecPatchOp, TeachingSpec
from evowiki.teach.spec_ops import body_key
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.spec_store import RepoSpecRegistry
from evowiki.wikiapp.teach import add_item_flow, edit_item_flow, remove_item_flow
from evowiki.wikiapp.wiki_schemas import SpecItemIn, SpecItemPatchIn
from tests.harness.test_improver import CASES, Improver
from tests.harness.test_improver import deps as improver_deps
from tests.harness.test_round import NOW, Env, FakeImprover, U
from tests.runtime.helpers import make_user
from tests.teach.fake_approvals import Effect, FakeApprovals
from tests.wikiapp.test_goal import Ids

PARAM = ParamPatchOp(track="B", path="analogy_level", value=2)
RULE = SpecPatchOp(op="add_item", item_id="h-1", text="先给一个能看到结果的小例子，再讲定义。")


# ---- A. set_param ------------------------------------------------------------------------------

def test_a_param_op_round_trips_and_counts_as_a_deep_change():
    diff = PackDiff(ops=(KeyOp("H4.deep.max_tool_calls", 30),), spec_ops=(PARAM, RULE))
    back, _, version = diff_from_json(diff_to_json(diff, {"H4.deep.max_tool_calls": None}, 3))
    assert back == diff and version == 3 and isinstance(back.spec_ops[0], ParamPatchOp)
    assert agents_touched(PackDiff(spec_ops=(PARAM,))) == {"deep"}
    assert complexity(EMPTY_PACK, PackDiff(spec_ops=(PARAM,)), defaults()) == 1
    line = summary(diff_to_json(PackDiff(spec_ops=(PARAM, RULE)), {}, 1), None, None)
    assert "改了 1 条讲解规则" in line and "以后的页面：可以多用类比，但要说明类比在哪里不成立" in line


def test_a_set_param_change_applies_through_the_registry_and_undoes(pool):
    env = Env(pool, improver=FakeImprover(PackDiff(spec_ops=(PARAM,)), decision="b"))
    r = env.run()
    assert r.status == "applied"
    cur = env.registry.current(U)
    assert cur.spec.params.analogy_level == 2 and cur.patch_id.startswith("harness:")
    with pool.connection() as conn:
        undo_change(conn, U, r.change_id, registry=env.registry, now=NOW)
    assert env.registry.current(U).spec.params == RepParams()


# ---- the improver's proposal checks (A permission, B ban, D) -----------------------------------

def _run(pool, **kw) -> ImproverRun:
    make_user(pool, U)
    return ImproverRun(U, CASES, replace(improver_deps(pool, Improver()), **kw))


def _propose(run: ImproverRun, spec_ops, ops=()):
    return run.guard("propose", lambda: run.propose({
        "kind": "general", "ops": list(ops), "spec_ops": spec_ops, "rationale": "理由",
        "citations": ["signal:sig1"]}))


def test_the_improver_proposes_a_param_and_reads_the_current_params(pool):
    run = _run(pool)
    out = _propose(run, [{"op": "set_param", "path": "analogy_level", "value": 2}])
    assert out.error is None, out.content
    assert run.candidates["general"].diff.spec_ops == (PARAM,)
    assert _propose(run, [{"op": "set_param", "path": "analogy_level"}]).error
    assert _propose(run, [{"op": "set_param", "path": "nope", "value": 1}]).error
    assert "set_param" in next(t.spec.description for t in run.tools if t.spec.name == "propose")
    pack = run.read_pack({"dim": "H7"})
    assert "analogy_level「类比允许程度」" in pack and "现在：1" in pack


def test_a_param_the_user_set_or_a_user_rule_is_refused_at_proposal(pool):
    from evowiki.teach.contracts import SpecItem
    mine = SpecItem(item_id="si1", text="公式后面跟一句人话", origin="user_ui", authority="user")
    spec = TeachingSpec(version=2, items=(mine,), user_params=("analogy_level",))
    run = _run(pool, base_spec=spec)
    out = _propose(run, [{"op": "set_param", "path": "analogy_level", "value": 0}])
    assert out.error and "你自己定的讲解设置" in out.content
    assert "学习者自己定的，不能改" in run.read_pack({"dim": "H7"})
    out = _propose(run, [{"op": "edit_item", "item_id": "si1", "text": "改掉"}])
    assert out.error and "讲解规则改动" in out.content


def test_a_rule_the_user_deleted_is_refused_at_proposal(pool):
    from evowiki.rules.decisions import Decisions
    repo, ids = MemoryRepo([U]), Ids()
    add_item_flow(repo, U, SpecItemIn(text="每页开头列出全部术语"), now=NOW, new_id=ids)
    sid = next(i.item_id for i in tspec.effective_spec(repo.load(U).state).items
               if i.origin == "user_ui")
    remove_item_flow(repo, U, sid, now=NOW, new_id=ids)
    state = repo.load(U).state
    run = _run(pool, base_spec=tspec.effective_spec(state), decisions=Decisions.of(state))
    out = _propose(run, [{"op": "add_item", "text": "每页开头列出全部术语"}])
    assert out.error and "删除或拒绝过" in out.content


def test_a_refused_change_is_never_proposed_again(pool):
    banned = refused_keys([diff_to_json(PackDiff(ops=(KeyOp("H4.deep.max_tool_calls", 100),),
                                                 spec_ops=(PARAM, RULE)), {}, 1)])
    assert banned == {op_key(KeyOp("H4.deep.max_tool_calls", 100)), body_key(PARAM),
                      body_key(RULE)}
    run = _run(pool, banned=banned)
    for spec_ops, ops in (([{"op": "set_param", "path": "analogy_level", "value": 2}], ()),
                          ([{"op": "add_item", "text": "先给一个能看到结果的小例子，再讲定义"}], ()),
                          ([], [{"name": "H4.deep.max_tool_calls", "value": 100}])):
        out = _propose(run, spec_ops, ops)
        assert out.error and BANNED.split("{what}")[1] in out.content, out.content
    # another value of the same key, or another param value, is a new change
    assert _propose(run, [], [{"name": "H4.deep.max_tool_calls", "value": 90}]).error is None
    assert _propose(run, [{"op": "set_param", "path": "analogy_level", "value": 0}]).error is None


def test_the_round_hands_the_improver_the_refused_changes_and_the_decisions(pool):
    env = Env(pool, improver=FakeImprover(None, decision="none"))
    with pool.connection() as conn:
        rid = harness_rsi.start_round(conn, U, finding=None, now=NOW)
        # "stale": approved, but closed rejected because the spec moved: nobody said no to it
        for status, value in (("rejected", 2), ("stale", 3), ("undone", 0), ("rolled_back", 1),
                              ("applied", 2)):
            diff = PackDiff(spec_ops=(ParamPatchOp(track="B", path="fading_level", value=value),))
            cid = harness_rsi.record_change(conn, U, rid, diff_to_json(diff, {}, 1), "asked",
                                            now=NOW)
            harness_rsi.settle_change(conn, cid, "rejected" if status in ("rejected", "stale")
                                      else "applied", now=NOW)
            if status in ("undone", "rolled_back"):
                harness_rsi.settle_change(conn, cid, status, now=NOW)  # type: ignore[arg-type]
            if status in ("rejected", "stale"):
                key = f"harness:{U}:{cid}"
                conn.execute(
                    "insert into agent_action (action_id, user_id, kind, scope, level, status, "
                    "source, summary, content, content_hash, idempotency_key, created_at) values "
                    "(%s, %s, 'rsi_method', 'rsi', 'ask', %s, 'rsi', 's', '{}', 'h', %s, %s)",
                    (f"act-{cid}", U, "rejected" if status == "rejected" else "approved", key,
                     NOW))
    env.run()
    (seen,) = env.improver.seen
    assert seen.banned == {"set|B|fading_level|2", "set|B|fading_level|0", "set|B|fading_level|1"}
    assert seen.decisions.records == ()


# ---- D. commit and approval --------------------------------------------------------------------

def _wiki_env(pool, diff, **kw):
    repo, ids = MemoryRepo([U]), Ids()
    env = Env(pool, improver=FakeImprover(diff), registry=RepoSpecRegistry(repo, ids), repo=repo,
              **kw)
    return env, repo, ids


def test_the_commit_checks_again_with_the_decisions_in_force(pool):
    """The proposal check is the improver's; the commit runs the checkpoint on its own (here the
    improver is a fake that skipped it): re-adding a rule the user deleted is RULE_LOCKED."""
    text = "每页开头列出全部术语"
    env, repo, ids = _wiki_env(pool, PackDiff(spec_ops=(SpecPatchOp(op="add_item", item_id="h-1",
                                                                     text=text),)))
    add_item_flow(repo, U, SpecItemIn(text=text), now=NOW, new_id=ids)
    sid = next(i.item_id for i in tspec.effective_spec(repo.load(U).state).items
               if i.origin == "user_ui")
    remove_item_flow(repo, U, sid, now=NOW, new_id=ids)
    r = env.run()
    assert (r.status, r.reason) == ("failed", RULE_LOCKED)
    assert all(i.text != text for i in tspec.effective_spec(repo.load(U).state).items)


def test_a_user_edit_while_the_change_waits_makes_its_approval_stale(pool):
    diff = PackDiff(spec_ops=(SpecPatchOp(op="edit_item", item_id="d22",
                                          text="例子优先用学习者学过的页面里的内容。"),))
    env, repo, ids = _wiki_env(pool, diff, approvals=FakeApprovals(ask=True))
    r = env.run()
    assert r.status == "asked"
    edit_item_flow(repo, U, "d22", SpecItemPatchIn(text="例子用我熟悉的东西"), now=NOW, new_id=ids)
    with pytest.raises(PendingStale) as e:
        HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=Effect())
    assert str(e.value) == STALE_SPEC and env.changes()[0]["status"] == "rejected"
    d22 = next(i for i in tspec.effective_spec(repo.load(U).state).items if i.item_id == "d22")
    assert (d22.text, d22.authority) == ("例子用我熟悉的东西", "user")


def test_the_guard_runs_again_at_approval(pool):
    calls = []
    block = GuardResult("block", "和你的规则或你拒绝过的规则冲突")

    def guard(user_id, diff):
        calls.append(diff)
        return PASS if len(calls) == 1 else block
    env = Env(pool, approvals=FakeApprovals(ask=True), guard=guard,
              improver=FakeImprover(PackDiff(spec_ops=(RULE,))))
    r = env.run()
    assert r.status == "asked"
    effect = Effect()
    with pytest.raises(PendingStale) as e:
        HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=effect)
    assert "冲突" in str(e.value) and effect.n == 0 and len(calls) == 2
    assert env.changes()[0]["status"] == "rejected" and env.registry.current(U).spec.items == ()


def test_a_confirm_at_approval_applies_it(pool):
    """Unsure is asked: approving it is the user's confirmation."""
    env = Env(pool, approvals=FakeApprovals(ask=True), improver=FakeImprover(
        PackDiff(spec_ops=(RULE,))), guard=lambda u, d: GuardResult("confirm", "拿不准"))
    r = env.run()
    assert r.status == "asked"
    HarnessRounds(env.deps).approve(U, "h_" + r.change_id, now=NOW, on_accept=Effect())
    assert [i.item_id for i in env.registry.current(U).spec.items] == ["h-1"]


# ---- E. the pages a rule change left out of date -----------------------------------------------

def test_an_applied_rule_change_calls_the_rule_stale_pass(pool):
    seen = []
    env = Env(pool, improver=FakeImprover(PackDiff(spec_ops=(PARAM,)), decision="b"),
              rule_stale=lambda u, now: seen.append(u) or 0)
    r = env.run()
    assert r.status == "applied" and seen == [U]
    # the ops the pass reads back for this round's spec version
    ops_of = spec_ops_of(pool)
    rnd = env.rounds()[0]
    assert ops_of(U, f"harness:{rnd.round_id}") == (PARAM,)
    assert ops_of(U, "harness:nope") is None and ops_of(U, "rsib_x") is None


def test_a_change_without_spec_ops_skips_the_rule_stale_pass(pool):
    seen = []
    env = Env(pool, rule_stale=lambda u, now: seen.append(u) or 0)      # H4 only
    assert env.run().status == "applied" and seen == []
