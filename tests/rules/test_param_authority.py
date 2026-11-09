"""The checkpoint over the teaching spec's params (RSI-B retired, 2026-10-29: `set_param` moved to
the harness loop's H7): the user's own setting is stamped in `user_params` and out of RSI's reach,
a system write (a seed style pack) only sets a param the user did not, and the user's undo of an RSI param change does not count as his
setting. Pure, plus the wiki's user write path (MemoryRepo). No model call."""

import pytest

from evowiki.rules.authority import RuleAuthorityError, check
from evowiki.teach import spec_ops
from evowiki.teach.contracts import ParamPatchOp, RepParams, TeachingSpec
from evowiki.teach.rule_authority import USER_PARAM, settle_items
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.teach import restore_flow, set_params_flow
from tests.wikiapp.test_goal import NOW, Ids

U = "u1"
BASE = TeachingSpec(version=1, items=())


def moved(spec: TeachingSpec, **params) -> TeachingSpec:
    return spec.model_copy(update={"params": spec.params.model_copy(update=params)})


def test_the_users_param_change_is_stamped_and_an_undo_is_not():
    new, _ = settle_items(BASE, moved(BASE, analogy_level=2), actor="user")
    assert new.user_params == ("analogy_level",)
    again, _ = settle_items(new, moved(new, recap_max_per_page=2, analogy_level=2), actor="user")
    assert again.user_params == ("recap_max_per_page", "analogy_level")    # RepParams order
    undone, _ = settle_items(BASE, moved(BASE, fading_level=2), actor="user", undo=True)
    assert undone.user_params == ()


def test_rsi_may_not_move_a_param_the_user_set():
    mine = settle_items(BASE, moved(BASE, analogy_level=2), actor="user")[0]
    with pytest.raises(RuleAuthorityError) as e:
        check(mine, moved(mine, analogy_level=0), actor="rsi")
    assert USER_PARAM in str(e.value) and "类比允许程度" in str(e.value)
    # another param is RSI's to tune, and the stamp stays as it was
    out = check(mine, moved(mine, recap_max_per_page=3), actor="rsi")
    assert out.params.recap_max_per_page == 3 and out.user_params == ("analogy_level",)


def test_an_automatic_write_never_changes_user_params_and_the_system_only_unset_ones():
    mine = settle_items(BASE, moved(BASE, analogy_level=2), actor="user")[0]
    out = check(mine, mine.model_copy(update={"user_params": ()}), actor="rsi")
    assert out.user_params == ("analogy_level",)
    with pytest.raises(RuleAuthorityError) as e:
        check(mine, moved(mine, analogy_level=0), actor="system")
    assert USER_PARAM in str(e.value)
    seeded = check(mine, moved(mine, interactive_bias=2), actor="system")    # a seed style pack
    assert seeded.params.interactive_bias == 2 and seeded.user_params == ("analogy_level",)


def test_user_params_name_distinct_repparams_paths():
    with pytest.raises(ValueError):
        TeachingSpec(version=1, items=(), user_params=("nope",))
    with pytest.raises(ValueError):
        TeachingSpec(version=1, items=(), user_params=("analogy_level", "analogy_level"))


def test_spec_ops_keep_user_params():
    mine = BASE.model_copy(update={"user_params": ("analogy_level",)})
    out = spec_ops.apply(mine, ParamPatchOp(track="B", path="fading_level", value=2), patch_id="p")
    assert out.user_params == ("analogy_level",)
    back = spec_ops.revert(out, mine, ParamPatchOp(track="B", path="fading_level", value=2))
    assert back.user_params == ("analogy_level",) and back.params == RepParams()


def test_the_settings_route_and_a_restore_stamp_through_commit_spec():
    repo, ids = MemoryRepo([U]), Ids()
    set_params_flow(repo, U, RepParams(analogy_level=0), now=NOW, new_id=ids)
    spec = tspec.effective_spec(repo.load(U).state)
    assert spec.user_params == ("analogy_level",)
    set_params_flow(repo, U, RepParams(analogy_level=0, interactive_bias=2), now=NOW, new_id=ids)
    assert tspec.effective_spec(repo.load(U).state).user_params == ("analogy_level",
                                                                     "interactive_bias")
    restore_flow(repo, U, 1, now=NOW, new_id=ids)          # back to the defaults: his choice too
    out = tspec.effective_spec(repo.load(U).state)
    assert out.params == RepParams() and out.user_params == ("analogy_level", "interactive_bias")
