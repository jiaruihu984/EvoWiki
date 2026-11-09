"""The page flow of `goals.page_update` (was goals.patch) (feedback 「这部分我懂」, K changed, page opened), verified by call
counts. Since 2026-10-28 (skill design A7 item 5) it makes no model call at all: a conflict is
recorded on the page and the sections that can fix it (`to_write`) are handed to the page writer
(`wikiapp.stale.patch_pending`); the outline and texts stay as they are until the writer writes."""

import pytest

from evowiki.goals.contracts import Band, SectionRole, SectionText
from evowiki.goals.fold import Page, k_snapshot, set_pinned, states_for
from evowiki.goals.guardrails import DraftOutline
from evowiki.goals.outline_context import accept_draft
from evowiki.goals.page_update import (
    after_k_change,
    conflict_refs,
    feedback,
    feedback_outline,
    open_page,
    recheck,
    unresolved_banner,
    writer_instruction,
)
from tests.goals.fakes_k import ZSCORE_OUTLINE, zscore_k, zscore_spec


@pytest.fixture
def built():
    """An organized page whose sections carry stand-in texts (the page writer is not under test)."""
    spec, k = zscore_spec(), zscore_k()
    outline = accept_draft(DraftOutline.model_validate(ZSCORE_OUTLINE), spec, k).outline
    assert outline is not None
    texts = {s.id: SectionText(section_id=s.id, cue=f"{s.id} 的提示", body=s.id, cache_key=s.id)
             for s in outline.sections if s.role is not SectionRole.CHECK}
    page = Page(outline=outline, states=states_for(outline), texts=texts,
                snapshot=k_snapshot(spec, k))
    return spec, k, page


# ---- 「只改这一段」 table ----

def test_know_this_folds_and_carries_observations(built):
    spec, _k, page = built
    r = feedback(spec, page, "s3", "know")
    assert r.page.states["s3"].folded and r.page.states["s3"].fold_locked
    assert [(o.cell, o.polarity, o.strength, o.kind) for o in r.observations] == [
        ("z_score.interpret", "+", 0.6, "feedback")]


def test_rewrite_feedback_is_the_page_writers(built):
    """「再讲深一点 / 换个讲法」 are rewritten by the page writer against `feedback_outline`."""
    spec, _k, page = built
    for kind in ("deeper", "other"):
        with pytest.raises(ValueError, match="page writer"):
            feedback(spec, page, "s3", kind)       # type: ignore[arg-type]
    bare = page.model_copy(update={"texts": {s: t for s, t in page.texts.items() if s != "s3"}})
    with pytest.raises(ValueError, match="no text yet"):
        feedback_outline(bare, "s3", "deeper")
    s1 = feedback_outline(page, "s1", "deeper").sections[0]
    assert s1.role is SectionRole.EXPLAIN and "再讲深一点" in s1.why


def test_k_up_folds_and_hands_nothing_to_the_writer(built):   # 「某段不再需要」 → fold, never delete
    spec, k, page = built
    k.set("mean.procedure", Band.KNOWN)
    k.set("variance.procedure", Band.KNOWN)
    r = after_k_change(spec, page, k)
    assert r.folded == ("s2",) and r.to_write == ()
    assert r.page.outline == page.outline and r.page.texts == page.texts


def test_a_weak_line_is_recorded_and_nothing_is_rewritten(built):
    """A wrong answer never rewrites what he reads without his word (user rulings 2026-11-04)."""
    spec, k, page = built
    k.set("z_score.notation", Band.SHAKY)
    r = after_k_change(spec, page, k)
    assert r.to_write == ()
    assert r.page.outline == page.outline and r.page.texts == page.texts
    assert r.page.conflicts == conflict_refs(r.conflicts) and r.banner


def test_page_feedback_naming_a_concept_hands_the_weak_line_to_the_writer(built):
    spec, k, page = built
    k.set("z_score.notation", Band.SHAKY)
    r = after_k_change(spec, page, k, declared_too=True)
    assert r.to_write == ("s1",)
    assert r.page.outline == page.outline and r.page.texts == page.texts   # the writer's to change
    assert not r.page.states["s1"].folded


def test_an_assumed_cell_lost_is_the_banners(built):
    spec, k, page = built
    k.set("mean.term", Band.LIKELY)      # assumed cell no longer known → conflict
    r = after_k_change(spec, page, k)
    assert [c.code for c in r.conflicts] == ["assumed_lost"] and r.to_write == ()
    assert r.banner and r.page.outline == page.outline


def test_a_pinned_line_is_never_handed_over(built):
    """Carried ruling 1: a pinned line holding a now-shaky cell is an "uncovered" conflict; the
    pinned section stays as it is and the reminder is a reorganize's (the banner)."""
    spec, k, page = built
    page = page.model_copy(update={"states": set_pinned(page.outline, page.states, "s1")})
    k.set("z_score.notation", Band.SHAKY)
    r = after_k_change(spec, page, k)
    assert [c.kind for c in r.conflicts] == ["uncovered"] and r.to_write == ()
    assert r.page.outline == page.outline and r.page.states["s1"].pinned


def _with_s1(page, **update):
    s1 = page.outline.sections[0].model_copy(update=update)
    outline = page.outline.model_copy(update={"sections": (s1, *page.outline.sections[1:])})
    return page.model_copy(update={"outline": outline})


def test_weak_cell_only_in_an_organizer_hands_that_section_over(built):
    spec, k, page = built
    page = _with_s1(page, role=SectionRole.ORGANIZER)
    k.set("z_score.notation", Band.SHAKY)
    r = after_k_change(spec, page, k, declared_too=True)
    assert [c.code for c in r.conflicts] == ["uncovered_untaught"] and r.to_write == ("s1",)


def test_section_just_deepened_is_not_auto_folded_by_the_skip_pass(built):
    """Final review 2: after 「再讲深一点」 the caller's after_k_change(skip=sid) leaves the
    section's fold state alone; a later pass without skip folds it normally."""
    spec, k, page = built
    deepened = page.model_copy(update={"outline": feedback_outline(page, "s1", "deeper")})
    r2 = after_k_change(spec, deepened, k, skip="s1")
    assert not r2.page.states["s1"].folded and "s1" not in r2.folded
    assert not r2.page.states["s1"].fold_locked
    r3 = after_k_change(spec, r2.page, k)
    assert r3.page.states["s1"].folded and "s1" in r3.folded


# ---- 「自你上次来……」 ----

def test_open_page_folds_notes_and_rewrites_nothing(built):
    spec, k, page = built
    k.set("mean.procedure", Band.KNOWN)
    k.set("variance.procedure", Band.KNOWN)
    k.set("z_score.notation", Band.SHAKY)
    r = open_page(spec, page, k)
    assert r.note.startswith("自你上次来：") and "已折叠「回顾：均值与方差怎么算」" in r.note
    assert "待重写" not in r.note and r.to_write == ()
    assert r.page.snapshot["z_score.notation"] is Band.SHAKY


def test_open_page_with_k_moved_but_no_conflict_folds(built):
    spec, k, page = built
    k.set("mean.procedure", Band.KNOWN)
    k.set("variance.procedure", Band.KNOWN)
    r = open_page(spec, page, k)
    assert r.folded == ("s2",) and r.to_write == () and r.conflicts == ()
    assert "已折叠" in r.note and r.page.snapshot["mean.procedure"] is Band.KNOWN


def test_open_unchanged_page_changes_nothing(built):
    spec, k, page = built
    r = open_page(spec, page, k)
    assert r.note == "" and r.page == page


# ---- unresolved conflicts: recorded until the writer (or a reorganize) fixes them ----

BANNER_1 = "这页有 1 处内容可能不够，点『按当前状态重新组织』补上"


def test_a_conflict_is_recorded_with_a_banner_until_fixed(built):
    spec, k, page = built
    assert page.unresolved_conflicts == ()
    k.set("z_score.notation", Band.SHAKY)
    r = after_k_change(spec, page, k)
    assert r.page.snapshot["z_score.notation"] is Band.SHAKY
    assert r.page.unresolved_conflicts == tuple(c.message for c in r.conflicts)
    assert len(r.page.conflicts) == sum(len(c.cells) for c in r.conflicts) == 1
    assert r.page.conflicts[0].model_dump() == {
        "kind": "line_weak", "section_id": "s1", "cell": "z_score.notation"}
    assert r.unresolved == r.page.unresolved_conflicts and r.banner == BANNER_1
    assert unresolved_banner(r.page) == BANNER_1
    opened = open_page(spec, r.page, k)                 # K unchanged since snapshot: nothing new
    assert opened.page == r.page and opened.banner == BANNER_1
    # the writer turned s1 into a recap: re-found, nothing is left
    fixed = recheck(spec, _with_s1(r.page, role=SectionRole.RECAP), k)
    assert fixed.unresolved_conflicts == () and fixed.conflicts == ()
    assert unresolved_banner(fixed) == ""


def test_the_writers_instruction_carries_the_conflicts(built):
    spec, k, page = built
    k.set("z_score.notation", Band.SHAKY)
    r = after_k_change(spec, page, k)
    text = writer_instruction(r.page.unresolved_conflicts)
    assert r.page.unresolved_conflicts[0] in text and "只改写这次范围内的段" in text


def test_unresolved_cleared_when_no_conflicts_remain_but_kept_under_skip(built):
    spec, k, page = built
    k.set("z_score.notation", Band.SHAKY)
    stuck = after_k_change(spec, page, k).page
    assert stuck.unresolved_conflicts
    k.set("z_score.notation", Band.KNOWN)                 # K moved back: nothing left to fix
    kept = after_k_change(spec, stuck, k, skip="s3")
    assert kept.page.unresolved_conflicts == stuck.unresolved_conflicts and kept.banner
    assert stuck.conflicts and kept.page.conflicts == stuck.conflicts
    cleared = open_page(spec, stuck, k)
    assert cleared.to_write == () and cleared.page.unresolved_conflicts == ()
    assert cleared.page.conflicts == ()
    assert cleared.banner == "" and cleared.unresolved == ()


def test_feedback_results_carry_the_unresolved_banner(built):
    spec, _k, page = built
    page = page.model_copy(update={"unresolved_conflicts": ("（测试）旧冲突",)})
    r = feedback(spec, page, "s3", "know")
    assert r.unresolved == ("（测试）旧冲突",) and r.banner == BANNER_1


def test_two_cells_count_every_cell_then_clear(built):
    spec, k, page = built
    k.set("z_score.notation", Band.SHAKY)
    k.set("z_score.procedure", Band.GAP)
    r = after_k_change(spec, page, k)
    assert len(r.page.unresolved_conflicts) == len(r.conflicts)
    assert len(r.page.conflicts) == sum(len(c.cells) for c in r.conflicts) == 2
    assert {c.section_id for c in r.page.conflicts} == {"s1"}
    k.set("z_score.notation", Band.KNOWN)
    k.set("z_score.procedure", Band.KNOWN)               # nothing left to fix: both cleared
    again = open_page(spec, r.page, k)
    assert again.page.unresolved_conflicts == () and again.page.conflicts == ()


def test_old_page_json_without_conflicts_reads_as_empty(built):
    _spec, _k, page = built
    raw = page.model_copy(update={"unresolved_conflicts": ("（旧）冲突",)}).model_dump(mode="json")
    raw.pop("conflicts", None)
    old = Page.model_validate(raw)
    assert old.unresolved_conflicts == ("（旧）冲突",) and old.conflicts == ()
