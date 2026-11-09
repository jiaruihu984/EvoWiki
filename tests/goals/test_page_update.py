from evowiki.goals.contracts import Band, SectionState
from evowiki.goals.fold import set_pinned, states_for
from evowiki.goals.guardrails import DraftOutline, page_bands, to_outline
from evowiki.goals.page_update import DraftOp, apply_ops, find_conflicts, writer_sections
from tests.goals.fakes_k import ZSCORE_OUTLINE, zscore_k, zscore_spec


def _outline():
    spec, k = zscore_spec(), zscore_k()
    return to_outline(DraftOutline.model_validate(ZSCORE_OUTLINE), spec, page_bands(spec, k))


def _conf(k, **kw):
    spec, o = zscore_spec(), _outline()
    return find_conflicts(spec, o, kw.pop("states", states_for(o)), page_bands(spec, k), **kw)


def test_no_conflicts_on_the_fresh_page():
    assert _conf(zscore_k()) == []


def test_line_with_weak_cell_is_a_conflict_unless_skipped():
    k = zscore_k({"z_score.notation": Band.SHAKY})
    cs = _conf(k)
    assert [c.kind for c in cs] == ["line_weak"] and cs[0].section == "s1"
    assert _conf(k, skip="s1") == []


def test_pinned_line_with_weak_cell_is_an_uncovered_conflict():
    """Carried ruling 1: the pinned line cannot change, so its weak cell needs a reminder section
    (otherwise the coverage guardrail rejects every patch with no conflict explaining why)."""
    k = zscore_k({"z_score.notation": Band.SHAKY})
    o = _outline()
    pinned = set_pinned(o, states_for(o), "s1")
    cs = _conf(k, states=pinned)
    assert [(c.kind, c.cells) for c in cs] == [("uncovered", ("z_score.notation",))]
    assert "钉住" in cs[0].message and "提醒" in cs[0].message
    assert _conf(k, states=pinned, skip="s1") == []


def test_pinned_line_weak_cell_taught_elsewhere_is_no_conflict():
    spec, o = zscore_spec(), _outline()
    k = zscore_k({"z_score.notation": Band.SHAKY})
    d, _ = apply_ops(o, [DraftOp(op="insert", after="s1", section={
        "id": "n1", "heading": "z 公式提醒", "role": "recap", "cells": ["z_score.notation"]})])
    o2 = to_outline(d, spec, page_bands(spec, k))
    states = set_pinned(o2, states_for(o2), "s1")
    assert find_conflicts(spec, o2, states, page_bands(spec, k)) == []


def test_assumed_cell_no_longer_known_is_a_conflict():
    cs = _conf(zscore_k({"mean.term": Band.LIKELY}))
    assert [c.kind for c in cs] == ["assumed_lost"] and cs[0].cells == ("mean.term",)


def test_uncovered_weak_cell_is_a_conflict():
    spec = zscore_spec()
    o = _outline()
    o2 = o.model_copy(update={"assumed": tuple(a for a in o.assumed if a != "mean.term")})
    bands = page_bands(spec, zscore_k({"mean.term": Band.GAP}))
    assert [c.kind for c in find_conflicts(spec, o2, states_for(o2), bands)] == ["uncovered"]


def test_apply_ops_rewrite_insert_move_assume_and_no_delete():
    o = _outline()
    d, applied = apply_ops(o, [
        DraftOp(op="rewrite", id="s1", role="recap"),
        DraftOp(op="insert", after="s1", section={"id": "s2", "heading": "新段", "role": "explain",
                                                  "cells": ["z_score.interpret"]}),
        DraftOp(op="move", id="s3", after=None),
        DraftOp(op="assume", cells=["mean.term"]),
        DraftOp(op="delete", id="s2"),
    ])
    assert [s.id for s in d.sections] == ["s3", "s1", "s2x", "s2"]
    assert d.sections[1].role == "recap" and d.assumed == ["mean.term"]
    assert [a.op for a in applied] == ["rewrite", "insert", "move", "assume"]
    assert applied[1].section.id == "s2x"


def _line_weak_setup():
    spec, o = zscore_spec(), _outline()
    k = zscore_k({"z_score.notation": Band.SHAKY})
    return spec, o, k, find_conflicts(spec, o, states_for(o), page_bands(spec, k))


# ---- what a K change hands to the page writer (goals.patch model step removed 2026-10-28) ----

def test_writer_gets_the_line_weak_section_only():
    _, o, _, cs = _line_weak_setup()
    assert writer_sections(o, states_for(o), cs) == ("s1",)


def test_writer_never_gets_a_pinned_section_or_a_stale_pinned_id():
    """Carried ruling 2: pinned = pinned states ∩ outline section ids; a pinned line is the
    banner's (a reorganize), never the scoped writer's."""
    spec, o, k, _ = _line_weak_setup()
    states = {**states_for(o), "gone": SectionState(section_id="gone", pinned=True)}
    cs = find_conflicts(spec, o, states, page_bands(spec, k))
    assert writer_sections(o, states, cs) == ("s1",)
    pinned = set_pinned(o, states_for(o), "s1")
    cs = find_conflicts(spec, o, pinned, page_bands(spec, k))
    assert [c.code for c in cs] == ["pinned_line_weak"] and writer_sections(o, pinned, cs) == ()


def test_writer_gets_the_prose_section_of_an_untaught_cell_and_nothing_for_absent_ones():
    *_, cs = _variant([DraftOp(op="rewrite", id="s1", role="organizer")])
    o = _variant([DraftOp(op="rewrite", id="s1", role="organizer")])[1]
    assert [c.code for c in cs] == ["uncovered_untaught"]
    assert writer_sections(o, states_for(o), cs) == ("s1",)
    cs = _conf(zscore_k({"mean.term": Band.GAP}))                  # assumed_lost: no section
    assert writer_sections(_outline(), states_for(_outline()), cs) == ()


_CHECK = {"cell": "z_score.notation", "stem": "z 的公式是？",
          "options": ["(x − μ) / σ", "(x − σ) / μ", "x / σ"], "answer": 0}


def _variant(ops):
    spec, k = zscore_spec(), zscore_k({"z_score.notation": Band.SHAKY})
    d, _ = apply_ops(_outline(), ops)
    o = to_outline(d, spec, page_bands(spec, k))
    return spec, o, k, find_conflicts(spec, o, states_for(o), page_bands(spec, k))


def test_weak_cell_only_in_an_organizer_section_is_uncovered():
    *_, cs = _variant([DraftOp(op="rewrite", id="s1", role="organizer")])
    assert [(c.kind, c.cells) for c in cs] == [("uncovered", ("z_score.notation",))]
    assert "开篇 / 确认题" in cs[0].message


def test_weak_cell_only_in_a_check_section_is_uncovered():
    *_, cs = _variant([
        DraftOp(op="rewrite", id="s1", cells=["z_score.purpose", "z_score.procedure"]),
        DraftOp(op="insert", after="s1", section={"id": "c1", "heading": "小测", "role": "check",
                                                  "cells": ["z_score.notation"],
                                                  "check": _CHECK})])
    assert [(c.kind, c.cells) for c in cs] == [("uncovered", ("z_score.notation",))]


def test_line_weak_cell_taught_elsewhere_is_no_conflict():
    *_, cs = _variant([DraftOp(op="insert", after="s1", section={
        "id": "n1", "heading": "z 公式提醒", "role": "recap", "cells": ["z_score.notation"]})])
    assert cs == []


def test_assumed_weak_cell_is_reported_once():
    cs = _conf(zscore_k({"mean.term": Band.GAP}))
    assert [(c.kind, c.cells) for c in cs] == [("assumed_lost", ("mean.term",))]


# ---- K2 Task 6: fixed conflict codes next to the Chinese messages ----

def test_find_conflicts_codes_and_anchors_keep_kind_and_message():
    from evowiki.goals.contracts import SectionRole as R
    from evowiki.goals.fold import ConflictRef
    from evowiki.goals.page_update import conflict_refs
    spec, o = zscore_spec(), _outline()
    # line_weak, two cells in one section -> two refs with the same section_id
    k = zscore_k({"z_score.notation": Band.SHAKY, "z_score.procedure": Band.GAP})
    [c] = _conf(k)
    assert (c.kind, c.code, c.section, c.anchor) == ("line_weak", "line_weak", "s1", None)
    assert c.message.startswith("「z 的公式」只一行带过")
    assert conflict_refs([c]) == (
        ConflictRef(kind="line_weak", section_id="s1", cell="z_score.notation"),
        ConflictRef(kind="line_weak", section_id="s1", cell="z_score.procedure"))
    # pinned line: kind/section unchanged (patch inserts), code + anchor say which section
    pinned = set_pinned(o, states_for(o), "s1")
    [p] = _conf(zscore_k({"z_score.notation": Band.SHAKY}), states=pinned)
    assert (p.kind, p.code, p.section, p.anchor) == ("uncovered", "pinned_line_weak", None, "s1")
    assert "钉住" in p.message
    assert conflict_refs([p]) == (
        ConflictRef(kind="pinned_line_weak", section_id="s1", cell="z_score.notation"),)
    # assumed cell lost
    [a] = _conf(zscore_k({"mean.term": Band.LIKELY}))
    assert (a.kind, a.code, a.section, a.anchor) == ("assumed_lost", "assumed_lost", None, None)
    assert a.message.startswith("默认你会的")
    assert conflict_refs([a]) == (ConflictRef(kind="assumed_lost", cell="mean.term"),)
    # weak cell on the page but only in an organizer section: uncovered_untaught
    o3 = o.model_copy(update={"sections": tuple(
        s.model_copy(update={"role": R.ORGANIZER}) if s.id == "s1" else s for s in o.sections)})
    bands = page_bands(spec, zscore_k({"z_score.notation": Band.SHAKY}))
    [u] = find_conflicts(spec, o3, states_for(o3), bands)
    assert (u.kind, u.code, u.section, u.anchor) == ("uncovered", "uncovered_untaught", None, None)
    assert "只出现在开篇" in u.message
    # weak cell nowhere on the page: uncovered_absent
    o2 = o.model_copy(update={"assumed": tuple(x for x in o.assumed if x != "mean.term")})
    bands = page_bands(spec, zscore_k({"mean.term": Band.GAP}))
    [n] = find_conflicts(spec, o2, states_for(o2), bands)
    assert (n.kind, n.code, n.section, n.anchor) == ("uncovered", "uncovered_absent", None, None)
    assert "页面上没有任何段覆盖它" in n.message
    assert conflict_refs([n]) == (ConflictRef(kind="uncovered_absent", cell="mean.term"),)
    assert conflict_refs([]) == ()
