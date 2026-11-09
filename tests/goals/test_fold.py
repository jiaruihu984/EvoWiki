import pytest

from evowiki.goals.contracts import Band
from evowiki.goals.fold import (
    FoldResult,
    Page,
    collapse_page,
    fold_pass,
    is_stale,
    k_snapshot,
    know_this,
    set_pinned,
    since_last_visit,
    states_for,
    toggle_fold,
)
from evowiki.goals.guardrails import DraftOutline, page_bands, to_outline
from tests.goals.fakes_k import ZSCORE_OUTLINE, zscore_k, zscore_spec


def _outline():
    spec, k = zscore_spec(), zscore_k()
    return to_outline(DraftOutline.model_validate(ZSCORE_OUTLINE), spec, page_bands(spec, k))


def test_k_up_folds_and_k_down_unfolds():
    o, k = _outline(), zscore_k()
    st = states_for(o)
    assert fold_pass(o, st, k).folded == ()
    k.set("mean.procedure", Band.KNOWN)
    k.set("variance.procedure", Band.KNOWN)
    r = fold_pass(o, st, k)
    assert r.folded == ("s2",) and r.states["s2"].folded and not r.states["s2"].fold_locked
    k.set("variance.procedure", Band.SHAKY)
    r2 = fold_pass(o, r.states, k)
    assert r2.unfolded == ("s2",) and not r2.states["s2"].folded


def test_likely_cells_do_not_fold_and_line_sections_never_fold():
    o, k = _outline(), zscore_k({"mean.procedure": Band.KNOWN, "variance.procedure": Band.LIKELY})
    assert fold_pass(o, states_for(o), k).folded == ()


def test_fold_locked_sections_are_left_alone():
    o, k = _outline(), zscore_k()
    st = toggle_fold(o, states_for(o), "s3")          # user folded the explain section by hand
    assert st["s3"].folded and st["s3"].fold_locked
    assert fold_pass(o, st, k).unfolded == ()          # gap cell, still folded: locked
    st = toggle_fold(o, st, "s3")
    assert not st["s3"].folded and st["s3"].fold_locked


def test_toggle_on_a_line_section_is_refused():
    o = _outline()
    with pytest.raises(ValueError):
        toggle_fold(o, states_for(o), "s1")


def test_know_this_folds_and_locks_only_foldable_sections():
    o = _outline()
    st = know_this(o, states_for(o), "s3")
    assert st["s3"].folded and st["s3"].fold_locked
    assert know_this(o, states_for(o), "s1") == states_for(o)


def test_collapse_folds_every_unlocked_foldable_section():
    o = _outline()
    k = zscore_k({"mean.procedure": Band.KNOWN, "variance.procedure": Band.KNOWN})
    st = toggle_fold(o, toggle_fold(o, states_for(o), "s3"), "s3")   # s3 locked open
    r = collapse_page(o, st, k)
    assert r.folded == ("s2",) and not r.states["s3"].folded and not r.states["s1"].folded


def test_collapse_skips_sections_with_a_weak_cell():
    o = _outline()
    k = zscore_k({"mean.procedure": Band.KNOWN, "variance.procedure": Band.KNOWN,
                  "z_score.interpret": Band.SHAKY})
    r = collapse_page(o, states_for(o), k)
    assert r.folded == ("s2",)                          # all known: folds
    assert not r.states["s3"].folded                    # a shaky cell: stays open
    k.set("variance.procedure", Band.GAP)
    assert collapse_page(o, states_for(o), k).folded == ()


def test_collapse_leaves_fold_locked_sections_alone():
    o = _outline()
    k = zscore_k({"mean.procedure": Band.KNOWN, "variance.procedure": Band.KNOWN})
    st = toggle_fold(o, toggle_fold(o, states_for(o), "s2"), "s2")   # s2 locked open
    st = toggle_fold(o, st, "s3")                                     # s3 locked folded (gap)
    r = collapse_page(o, st, k)
    assert r.folded == () and not r.states["s2"].folded and r.states["s3"].folded


def test_pin_and_states_for_keep_existing_state():
    o = _outline()
    st = set_pinned(o, states_for(o), "s1")
    assert st["s1"].pinned and states_for(o, st)["s1"].pinned


def test_since_last_visit_note_and_staleness():
    spec, o, k = zscore_spec(), _outline(), zscore_k()
    before = k_snapshot(spec, k)
    page = Page(outline=o, states=states_for(o), snapshot=before)
    assert not is_stale(spec, page, k)
    k.set("mean.procedure", Band.KNOWN)
    k.set("variance.procedure", Band.KNOWN)
    assert is_stale(spec, page, k)
    r = fold_pass(o, page.states, k)
    note = since_last_visit(spec, o, before, k_snapshot(spec, k), r)
    assert note.startswith("自你上次来：")
    assert "均值·计算过程 模糊→确定会" in note and "已折叠「回顾：均值与方差怎么算」" in note
    assert since_last_visit(spec, o, before, before, FoldResult(states=page.states)) == ""
