"""Rule provenance contract (the product specification §8):
each write entry stamps origin / authority / origin_ref; the checkpoint lets the user do anything
and RSI only touch its own, unprotected rules; a user's delete stops RSI from bringing that rule
back without banning similar teaching; the user's 「设为我的规则」. The four acceptance cases are the
`test_acceptance_*` tests. In-memory repo, fake judges: no model call."""

from datetime import timedelta

import pytest

from evowiki.rules.authority import RuleAuthorityError, check
from evowiki.rules.decisions import Decisions
from evowiki.rules.drafted import drafted_authority
from evowiki.rules.guard import Candidate, check_rule
from evowiki.teach import spec_ops
from evowiki.teach.contracts import SpecItem, SpecPatchOp, TeachingSpec
from evowiki.teach.rule_judge import Verdict
from evowiki.teach.spec_registry import MemoryRegistry
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.events import RuleDecided
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.spec_store import RepoSpecRegistry
from evowiki.wikiapp.teach import (
    add_item_flow,
    commit_spec,
    edit_item_flow,
    remove_item_flow,
    restore_flow,
    take_over_flow,
)
from evowiki.wikiapp.teach_seed import seed_style
from evowiki.wikiapp.wiki_schemas import SpecItemIn, SpecItemPatchIn
from tests.wikiapp.test_goal import NOW, Ids

U = "u1"


@pytest.fixture
def env():
    repo, ids = MemoryRepo([U]), Ids()
    return repo, ids, RepoSpecRegistry(repo, ids)


def item(repo, item_id) -> SpecItem:
    spec = tspec.effective_spec(repo.load(U).state)
    return next(i for i in spec.items if i.item_id == item_id)


def decisions(repo) -> list[RuleDecided]:
    return list(repo.load(U).state.rule_decisions)


def rsi_add(reg, text, patch_id="pt1", item_id="sr1"):
    """RSI adds a rule (spec_ops + the registry, as the harness round's H7 commit does)."""
    cur = reg.current(U)
    new = spec_ops.apply(cur.spec, SpecPatchOp(op="add_item", item_id=item_id, text=text),
                         patch_id=patch_id)
    return reg.commit(U, new, patch_id=patch_id, at=NOW)


def rsi_edit(reg, item_id, text, patch_id="pt2"):
    cur = reg.current(U)
    new = spec_ops.apply(cur.spec, SpecPatchOp(op="edit_item", item_id=item_id, text=text),
                         patch_id=patch_id)
    return reg.commit(U, new, patch_id=patch_id, at=NOW)


def whole_spec_edit(reg, item_id, text, *, actor="rsi"):
    """A whole-spec writer that bypasses spec_ops (what the commit_spec diff check stops)."""
    cur = reg.current(U)
    items = tuple(i.model_copy(update={"text": text}) if i.item_id == item_id else i
                  for i in cur.spec.items)
    return reg.commit(U, cur.spec.model_copy(update={"items": items}), patch_id="px", at=NOW,
                      actor=actor)


def stated(repo, ids, text="以后都先给例子再讲定义"):
    """A rule he stated in this chat message, as `wikiapp.preferences.land_preference` writes it
    (its ledger row aside: the in-memory repo has none)."""
    commit_spec(repo, repo.load(U), lambda s: tspec.write(s, tspec.AddRule(
        "sc1", text, origin="user_chat", authority="user", origin_ref="s1/m1"), actor="user"),
        actor="user", via="chat", action="guideline", origin="s1/m1", now=NOW, new_id=ids)


# ---- entries stamp their provenance (§8 #1) ----

def test_each_entry_writes_its_origin_and_authority(env):
    repo, ids, reg = env
    add_item_flow(repo, U, SpecItemIn(text="公式后面跟一句人话"), now=NOW, new_id=ids)
    ui = next(i for i in tspec.effective_spec(repo.load(U).state).items
              if i.text == "公式后面跟一句人话")
    assert (ui.origin, ui.authority, ui.rev, ui.rev_by) == ("user_ui", "user", 1, "user")
    stated(repo, ids)
    chat = item(repo, "sc1")
    assert (chat.origin, chat.authority, chat.origin_ref) == ("user_chat", "user", "s1/m1")
    seed_style(repo, U, items=["每段不超过五行"], origin="seed:style.md", exemplar=None,
               analogy=None, now=NOW, new_id=ids)
    seed = next(i for i in tspec.effective_spec(repo.load(U).state).items
                if i.text == "每段不超过五行")
    assert (seed.origin, seed.authority, seed.origin_ref) == ("seed", "rsi", "seed:style.md")
    rsi_add(reg, "先用一个反例")
    b = item(repo, "sr1")
    assert (b.origin, b.authority, b.origin_ref, b.rev_by) == ("rsi_h", "rsi", "pt1", "rsi")
    d = item(repo, "d01")
    assert (d.origin, d.authority, d.protected) == ("default", "rsi", False)


def test_commit_spec_needs_an_actor(env):
    repo, ids, _ = env
    with pytest.raises(TypeError):
        commit_spec(repo, repo.load(U), lambda s: tspec.set_params(s, s.params),  # type: ignore[call-arg]
                    action="params", now=NOW, new_id=ids)


def test_user_edit_of_an_rsi_rule_makes_it_theirs_keeping_origin(env):
    repo, ids, reg = env
    rsi_add(reg, "先用一个反例")
    edit_item_flow(repo, U, "sr1", SpecItemPatchIn(text="先用一个小反例"), now=NOW, new_id=ids)
    i = item(repo, "sr1")
    assert (i.origin, i.origin_ref, i.authority, i.rev, i.rev_by) == (
        "rsi_h", "pt1", "user", 2, "user")
    # a default edited by the user is theirs too (decision 3); the template stays as it was
    edit_item_flow(repo, U, "d03", SpecItemPatchIn(text="新方法先用人话说清"), now=NOW, new_id=ids)
    assert (item(repo, "d03").origin, item(repo, "d03").authority) == ("default", "user")
    from evowiki.teach.default_spec import DEFAULT_ITEMS
    assert DEFAULT_ITEMS[2].text.startswith("新方法先用人话说清它怎样改变了过程")


def test_approving_an_rsi_deploy_does_not_make_it_the_users(env):
    repo, _, reg = env
    rsi_add(reg, "先用一个反例")          # what an approved ask-level patch commits too
    assert item(repo, "sr1").authority == "rsi"


# ---- RSI's limits (§8 #2, #5, protected defaults) ----

def test_rsi_never_touches_a_user_rule_on_any_path(env):
    repo, ids, reg = env
    add_item_flow(repo, U, SpecItemIn(text="公式后面跟一句人话"), now=NOW, new_id=ids)
    mine = next(i for i in tspec.effective_spec(repo.load(U).state).items
                if i.origin == "user_ui")
    # B / H7 / replay all go through spec_ops.apply
    for op in ("edit_item", "remove_item"):
        with pytest.raises(spec_ops.PatchError):
            spec_ops.apply(reg.current(U).spec, SpecPatchOp(op=op, item_id=mine.item_id,
                                                             text="x"), patch_id="pt9")
    # a whole-spec write is stopped by the commit's item-by-item check
    with pytest.raises(RuleAuthorityError):
        whole_spec_edit(reg, mine.item_id, "改掉")
    assert item(repo, mine.item_id).text == "公式后面跟一句人话"


def test_replay_candidate_spec_refuses_a_user_rule(env):
    from evowiki.harness.replay import candidate_spec
    repo, ids, _ = env
    stated(repo, ids)
    spec = tspec.effective_spec(repo.load(U).state)
    with pytest.raises(spec_ops.PatchError):
        candidate_spec(spec, (SpecPatchOp(op="edit_item", item_id="sc1", text="改"),))


def test_memory_registry_runs_the_same_check():
    reg = MemoryRegistry()
    mine = SpecItem(item_id="m1", text="我的规则", origin="user_ui", authority="user")
    reg.seed(U, TeachingSpec(version=1, items=(mine,)), NOW)
    bad = TeachingSpec(version=1, items=(mine.model_copy(update={"text": "改了"}),))
    with pytest.raises(RuleAuthorityError):
        reg.commit(U, bad, patch_id="p", at=NOW)
    assert reg.commit(U, bad, patch_id="p", at=NOW, actor="user").spec.items[0].authority == "user"


# ---- the four acceptance cases ----

def test_acceptance_a_rule_the_user_states_in_chat_is_theirs(env):
    repo, ids, reg = env
    stated(repo, ids)
    i = item(repo, "sc1")
    assert (i.origin, i.authority, i.origin_ref) == ("user_chat", "user", "s1/m1")
    with pytest.raises(spec_ops.PatchError):
        rsi_edit(reg, "sc1", "每页都先给例子")
    with pytest.raises(RuleAuthorityError):
        whole_spec_edit(reg, "sc1", "每页都先给例子")
    cur = reg.current(U).spec
    with pytest.raises(RuleAuthorityError):          # H7 removing it, straight to the registry
        reg.commit(U, cur.model_copy(update={"items": tuple(
            x for x in cur.items if x.item_id != "sc1")}), patch_id="harness:r1", at=NOW)
    assert item(repo, "sc1").text == "以后都先给例子再讲定义"


class FakeJudge:
    def __init__(self, verdict="no_conflict"):
        self.verdict = verdict

    def conflict(self, candidate, scope, domain, against):
        return Verdict(self.verdict, tuple(a.ref for a in against), "", "fake/judge",
                       tuple(a.ref for a in against))


def test_acceptance_an_over_generalised_assistant_rule_stays_pending():
    """Acceptance case 2: the assistant widened his words, so the rule is not his
    (`rules.drafted`, code only; the chat path end to end: tests/assistant/test_stated_preferences.py)."""
    words = ["这里太难了"]
    assert drafted_authority(quote="这里太难了", text="以后所有页面都降低难度", words=words) == "pending"
    assert drafted_authority(quote=None, text="这里太难了", words=words) == "pending"
    ok = ["以后都先给例子再讲定义"]
    assert drafted_authority(quote="以后都先给例子再讲定义", text="以后都先给例子再讲定义。",
                             words=ok) == "user"
    # reworded away from his words → waits for him; not his words → never his
    assert drafted_authority(quote="以后都先给例子再讲定义", text="先给例子", words=ok) == "pending"
    assert drafted_authority(quote="以后都别举例", text="以后都别举例", words=ok) == "pending"
    # a quote shorter than 4 characters counts only as his whole message
    assert drafted_authority(quote="先举例", text="先举例", words=["先举例"]) == "user"
    assert drafted_authority(quote="举例", text="举例", words=["以后都先举例"]) == "pending"
    # the negation guard: the opposite rule is not his
    assert drafted_authority(quote="以后都先给例子再讲定义", text="以后都不要先给例子再讲定义",
                             words=ok) == "pending"


def test_acceptance_rsi_cannot_change_a_rule_the_user_took_over(env):
    repo, ids, reg = env
    rsi_add(reg, "先用一个反例")
    take_over_flow(repo, U, "sr1", now=NOW, new_id=ids)
    i = item(repo, "sr1")
    assert (i.origin, i.authority, i.rev) == ("rsi_h", "user", 1)
    d = decisions(repo)[-1]
    assert (d.action, d.rule_id, d.actor) == ("taken_over", "sr1", "user")
    with pytest.raises(spec_ops.PatchError):
        rsi_edit(reg, "sr1", "先用两个反例")
    with pytest.raises(RuleAuthorityError):
        whole_spec_edit(reg, "sr1", "先用两个反例")
    # RSI's own rollback of the patch that added it is refused (it is the user's now)
    cur, before = reg.current(U).spec, reg.get(U, 1).spec
    with pytest.raises(spec_ops.PatchError):
        spec_ops.revert(cur, before, SpecPatchOp(op="add_item", item_id="sr1",
                                                 text="先用一个反例"))


def test_acceptance_rsi_cannot_restore_a_rule_the_user_rejected(env):
    repo, ids, reg = env
    rsi_add(reg, "先用一个反例")
    before_delete = reg.current(U).spec
    remove_item_flow(repo, U, "sr1", now=NOW, new_id=ids)
    d = decisions(repo)[-1]
    assert (d.action, d.rule_id, d.rule_rev, d.via) == ("revoked", "sr1", 1, "ui")
    # RSI restoring the same rule (same id: an observe rollback / an old version) is refused
    cur = reg.current(U).spec
    with pytest.raises(RuleAuthorityError):
        reg.commit(U, cur.model_copy(update={"items": (*cur.items, item_of(before_delete, "sr1"))}),
                   patch_id="pt1", at=NOW, undo=True)
    # ... and so is the same text under a new id
    with pytest.raises(RuleAuthorityError):
        rsi_add(reg, "先用一个反例。", patch_id="pt3", item_id="sr3")
    # delete ≠ ban: a merely similar rule is not stopped by code; it goes to the guard's judge
    rsi_add(reg, "讲规则时附一个反例作对照", patch_id="pt4", item_id="sr4")
    g = check_rule(reg.current(U).spec, Decisions.of(repo.load(U).state),
                   Candidate("先举一个反面的例子"), judge=FakeJudge(verdict="unsure"))
    assert g.outcome == "confirm"
    # the user may add it back: reaccepted, and RSI may then leave it alone
    add_item_flow(repo, U, SpecItemIn(text="先用一个反例"), now=NOW, new_id=ids)
    assert decisions(repo)[-1].action == "reaccepted"


def item_of(spec: TeachingSpec, item_id: str) -> SpecItem:
    return next(i for i in spec.items if i.item_id == item_id)


# ---- rollbacks and the user's own restore (§8 #9–#11) ----

def test_an_automatic_rollback_skips_what_the_user_changed(env):
    repo, ids, reg = env
    before = reg.current(U).spec
    rsi_add(reg, "先用一个反例")
    edit_item_flow(repo, U, "sr1", SpecItemPatchIn(text="先用一个小反例"), now=NOW, new_id=ids)
    body = SpecPatchOp(op="add_item", item_id="sr1", text="先用一个反例")
    with pytest.raises(spec_ops.PatchError):
        spec_ops.revert(reg.current(U).spec, before, body)
    assert item(repo, "sr1").text == "先用一个小反例"


def test_the_users_restore_keeps_origin_and_reaccepts_what_they_deleted(env):
    repo, ids, reg = env
    rsi_add(reg, "先用一个反例")
    v = reg.current(U).version
    remove_item_flow(repo, U, "sr1", now=NOW + timedelta(minutes=1), new_id=ids)
    restore_flow(repo, U, v, now=NOW + timedelta(minutes=2), new_id=ids)
    i = item(repo, "sr1")
    assert (i.origin, i.origin_ref) == ("rsi_h", "pt1")
    # the user's own restore is always allowed; what they had deleted is recorded re-accepted
    assert [d.action for d in decisions(repo)] == ["revoked", "reaccepted"]


def test_settle_refuses_rsi_handing_a_rule_over_or_adding_a_user_rule():
    base = TeachingSpec(version=1, items=(SpecItem(item_id="a", text="规则", origin="rsi_h", authority="rsi"),))
    up = replace_items(base, base.items[0].model_copy(update={"authority": "user"}))
    with pytest.raises(RuleAuthorityError):
        check(base, up, actor="rsi")
    add = TeachingSpec(version=1, items=(*base.items, SpecItem(
        item_id="b", text="新规则", origin="user_ui", authority="user")))
    with pytest.raises(RuleAuthorityError):
        check(base, add, actor="rsi")
    assert check(base, add, actor="user").items[1].authority == "user"


def replace_items(spec: TeachingSpec, *items: SpecItem) -> TeachingSpec:
    return spec.model_copy(update={"items": items})


def test_memory_backfill_is_a_system_pending_write_then_the_users_confirmation(env):
    """Contract §7.1 #23: the backfill (through S3's land_preference) is the system's write of a
    pending rule; only the user's confirmation, their own write, makes it theirs."""
    repo, ids, _ = env

    def backfill(item_id, **kw):
        return lambda s: tspec.add_item(s, item_id=item_id, text=f"例子放在定义前 {item_id}",
                                        origin_ref="sess1/msg3", **kw)
    commit_spec(repo, repo.load(U), backfill("mb1", origin="memory_backfill", authority="pending"),
                actor="system", action="guideline", now=NOW, new_id=ids)
    i = item(repo, "mb1")
    assert (i.origin, i.authority, i.rev_by) == ("memory_backfill", "pending", "system")
    assert repo.load(U).state.rule_decisions == ()                 # the system decides nothing
    take_over_flow(repo, U, "mb1", now=NOW, new_id=ids)            # the user confirms it
    assert item(repo, "mb1").authority == "user"
    d = repo.load(U).state.rule_decisions[-1]
    assert (d.rule_id, d.action, d.actor) == ("mb1", "reaccepted", "user")


def test_the_system_writes_nothing_but_backfill_and_seed_items(env):
    repo, ids, _ = env
    for kw in ({"origin": "memory_backfill", "authority": "user"},
               {"origin": "user_ui", "authority": "pending"},
               {"origin": "rsi_h", "authority": "rsi"}):
        with pytest.raises(RuleAuthorityError):
            commit_spec(repo, repo.load(U), lambda s, kw=kw: tspec.add_item(
                s, item_id="x1", text="别的", **kw), actor="system", action="guideline",
                now=NOW, new_id=ids)
    commit_spec(repo, repo.load(U), lambda s: tspec.add_item(
        s, item_id="mb1", text="补记", origin="memory_backfill", authority="pending"),
        actor="system", action="guideline", now=NOW, new_id=ids)
    for change in (lambda s: tspec.set_authority(s, "mb1", "user"),
                   lambda s: tspec.edit_item(s, "mb1", text="改了"),
                   lambda s: tspec.remove_item(s, "d01")):
        with pytest.raises(RuleAuthorityError):
            commit_spec(repo, repo.load(U), change, actor="system", action="guideline", now=NOW,
                        new_id=ids)
    with pytest.raises(RuleAuthorityError):                       # RSI may not add a pending rule
        commit_spec(repo, repo.load(U), lambda s: tspec.add_item(
            s, item_id="mb2", text="别的", origin="memory_backfill", authority="pending"),
            actor="rsi", action="guideline", now=NOW, new_id=ids)


def test_a_protected_rule_is_out_of_rsis_reach():
    """Decision 3: a protected base rule is treated like the user's own (the list itself waits for
    the user's review, so the defaults carry none yet)."""
    base = TeachingSpec(version=1, items=(SpecItem(item_id="d12", text="简化要明说", origin="default",
                                                   authority="rsi", protected=True),))
    edited = base.model_copy(update={"items": (base.items[0].model_copy(update={"text": "x"}),)})
    with pytest.raises(RuleAuthorityError):
        check(base, edited, actor="rsi")
    with pytest.raises(RuleAuthorityError):
        check(base, base.model_copy(update={"items": ()}), actor="rsi")
    with pytest.raises(spec_ops.PatchError):
        spec_ops.apply(base, SpecPatchOp(op="edit_item", item_id="d12", text="x"), patch_id="p")
    assert check(base, edited, actor="user").items[0].authority == "user"


def test_an_item_in_the_old_shape_is_refused():
    """User ruling 2026-10-28 (「删了吧」: the old events are deleted, nothing is kept compatible):
    an item with the old `source` and no `authority`, or a free-text `origin`, does not read."""
    from pydantic import ValidationError
    for old in ({"item_id": "x", "text": "t", "source": "rsi"},
                {"item_id": "x", "text": "t", "source": "user", "origin": "gp1"},
                {"item_id": "x", "text": "t", "origin": "gp1", "authority": "user"}):
        with pytest.raises(ValidationError):
            SpecItem.model_validate(old)


def test_a_first_write_saves_the_defaults_as_v1_outside_the_checkpoint(env):
    """The `migrated` v1 (the default spec copied in on a user's first write) is the base the
    checkpoint compares against, never a change it checks: a first write by the system (a seed)
    or by the user works, and v1 holds the defaults as RSI's."""
    repo, ids, _ = env
    seed_style(repo, U, items=["每段不超过五行"], origin="seed:style.md", exemplar=None,
               analogy=None, now=NOW, new_id=ids)
    from evowiki.wikiapp.teach import read_version
    v1 = read_version(repo, repo.load(U), 1)
    assert [s.action for s in repo.load(U).state.spec_versions][:1] == ["migrated"]
    assert all((i.origin, i.authority) == ("default", "rsi") for i in v1.items)
    other = MemoryRepo([U])
    add_item_flow(other, U, SpecItemIn(text="公式后面跟一句人话"), now=NOW, new_id=Ids())
    assert [s.action for s in other.load(U).state.spec_versions] == ["migrated", "edit"]


def test_the_users_classification_of_the_defaults():
    """2026-10-28: d12 / d16 / d18 / d20 are protected; d06 / d13 / d15 are strategy RSI may not
    change yet (their floors are not checked on their own); the other 15 are RSI's to tune; the
    user edits any of them."""
    from evowiki.teach.contracts import rsi_may_change
    from evowiki.teach.default_spec import DEFAULT_ITEMS, default_spec
    by = {i.item_id: i for i in DEFAULT_ITEMS}
    assert {i for i, x in by.items() if x.protected} == {"d12", "d16", "d18", "d20"}
    assert {i for i, x in by.items() if not rsi_may_change(x)} == {
        "d06", "d12", "d13", "d15", "d16", "d18", "d20"}
    spec = default_spec()
    for iid in ("d06", "d12"):
        with pytest.raises(spec_ops.PatchError):
            spec_ops.apply(spec, SpecPatchOp(op="edit_item", item_id=iid, text="x"), patch_id="p")
        with pytest.raises(RuleAuthorityError):
            check(spec, spec.model_copy(update={"items": tuple(
                i for i in spec.items if i.item_id != iid)}), actor="rsi")
    spec_ops.apply(spec, SpecPatchOp(op="edit_item", item_id="d01", text="x"), patch_id="p")
    mine = check(spec, tspec.edit_item(spec, "d12", text="我的写法"), actor="user")
    assert next(i for i in mine.items if i.item_id == "d12").authority == "user"
