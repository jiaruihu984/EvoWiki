import pytest

from evowiki.goals.contracts import Band
from evowiki.goals.intake import IntakeRelation
from evowiki.goals.path import (
    OBJECTIVE_MAX,
    PURPOSE,
    DraftPath,
    PageNotStarted,
    PageStatus,
    PrereqEdge,
    _rule_objective,
    check_path,
    concept_order,
    diagnose_path,
    fringe,
    mark_done,
    new_cells,
    next_page,
    order_pages,
    plan_path,
    prereq_edges,
    rule_groups,
)
from tests.goals.llm_script import Script
from tests.goals.test_path_fakes import (
    ALL,
    GOOD,
    concept,
    goal,
    json_llm,
    k_for,
    pages,
    transformer,
    verified,
)


def _check(raw, g=None, k=None):
    """Blocking violations and diagnostics together, so each sample shows where it lands."""
    g = g or transformer()
    fresh = new_cells([c.id for c in g.concepts], k or k_for(g))
    draft = DraftPath.model_validate(raw)
    return check_path(draft, g, fresh) + diagnose_path(draft, g, fresh)


def _blocking(raw, g=None, k=None):
    g = g or transformer()
    fresh = new_cells([c.id for c in g.concepts], k or k_for(g))
    return check_path(DraftPath.model_validate(raw), g, fresh)


def _rules(vs):
    return sorted({v.rule for v in vs})


# --- new cells -------------------------------------------------------------------------------

def test_new_cells_are_gap_shaky_and_none_only():
    g = goal(concept("a", "甲"))
    k = k_for(g, {"a.term": Band.KNOWN, "a.purpose": Band.LIKELY, "a.notation": Band.SHAKY,
                  "a.procedure": Band.NONE}, default=Band.GAP)
    assert new_cells(["a"], k) == {"a": ("a.notation", "a.procedure", "a.interpret",
                                         "a.mechanism")}


# --- checks (shape, cycle) and diagnostics (coverage, order, objective) ------------------------

def test_good_grouping_passes():
    assert _check(GOOD) == []


def test_page_size_is_not_checked():
    assert _check(pages(["vec", "soft", "dot", "attn", "tf"])) == []      # 30 new cells, one page
    assert _check(pages(["vec"], ["soft"], ["dot"], ["attn"], ["tf"])) == []


def _order(raw, g=None, k=None, taught=None):
    g = g or transformer()
    fresh = new_cells([c.id for c in g.concepts], k or k_for(g))
    return order_pages(DraftPath.model_validate(raw), g, fresh, taught or {})


def test_order_is_repaired_a_prereq_on_a_later_page_moves_first():
    raw = pages(["dot", "attn"], ["vec", "soft"], ["tf"])
    assert _blocking(raw) == []
    vs = _check(raw)
    assert _rules(vs) == ["order"]
    assert any("点积相似度" in v.message and "向量与矩阵" in v.message and "排到了" in v.message
               for v in vs)
    o = _order(raw)
    assert o.order == (2, 1, 3) and o.check.reordered
    assert (o.check.edges, o.check.violations, o.check.hard, o.check.soft) == (3, 2, 0, 2)


def test_hard_edges_violated_are_counted_hard_and_still_repaired():
    o = _order(pages(["dot", "attn"], ["vec", "soft"], ["tf"]), verified(transformer()))
    assert o.blocking == () and o.order == (2, 1, 3)
    assert (o.check.violations, o.check.hard, o.check.soft) == (2, 2, 0)


def test_a_settled_prerequisite_never_constrains_the_order():
    """vec is known in K and soft is taught by an existing page: dot / attn may come first."""
    g = transformer()
    k = k_for(g, {f"vec.{a.value}": Band.KNOWN for a in ALL})
    o = _order(pages(["dot", "attn"], ["vec", "soft"], ["tf"]), g, k, {"soft": "softmax 入门"})
    assert o.order == (1, 2, 3) and o.check.violations == 0 and o.notes == ()


def test_fringe_and_concept_order():
    g = transformer()
    k = k_for(g, {f"vec.{a.value}": Band.KNOWN for a in ALL})
    fresh = new_cells([c.id for c in g.concepts], k)
    assert fringe(g, fresh) == ["soft", "dot"]
    assert fringe(g, fresh, {"soft": "p"}) == ["dot"]
    co = concept_order(g, fresh)
    assert co.order == ("vec", "soft", "dot", "attn", "tf") and co.dropped == ()
    back = goal(*reversed(g.concepts))              # listed backwards: prerequisites still first
    assert concept_order(back, fresh).order == ("soft", "vec", "dot", "attn", "tf")


def test_edge_confidence_hard_only_for_verified_prerequisites():
    """Relation layers: existence is not verification — reused concepts no longer make an edge
    hard; only a verified prerequisite relation does. Unverified prerequisites and recommended
    edges are soft; a recommended edge orders like any soft edge."""
    g = goal(concept("a", "均值", reused=True), concept("b", "方差", "a", reused=True),
             concept("c", "z 分数", "a", target=True), concept("d", "标准化"))
    g = g.model_copy(update={"relations": (
        IntakeRelation(kind="prerequisite", src="b", dst="c", verified=True),
        IntakeRelation(kind="recommended", src="d", dst="c"),
        IntakeRelation(kind="prerequisite", src="a", dst="d"),
        IntakeRelation(kind="part_of", src="a", dst="d"))})
    assert prereq_edges(g) == [PrereqEdge("a", "b", False), PrereqEdge("a", "c", False),
                               PrereqEdge("b", "c", True), PrereqEdge("d", "c", False),
                               PrereqEdge("a", "d", False)]


def test_outline_order_is_the_default_and_soft_edges_never_block():
    """No edge: the outline's order is the path order. A soft cycle (two unverified edges) is
    dropped, never a blocking cycle."""
    g = goal(concept("x", "甲"), concept("y", "乙"), concept("z", "丙"))
    fresh = new_cells(["x", "y", "z"], k_for(g))
    assert concept_order(g, fresh).order == ("x", "y", "z")
    g = g.model_copy(update={"relations": (
        IntakeRelation(kind="recommended", src="z", dst="x"),
        IntakeRelation(kind="recommended", src="x", dst="z"))})
    co = concept_order(g, fresh)
    assert co.joined == () and set(co.dropped) == {("z", "x"), ("x", "z")}
    assert _blocking(pages(["x"], ["y"], ["z"]), g) == []


def test_a_cycle_in_the_concept_graph_drops_soft_edges_and_joins_hard_ones():
    g = verified(goal(concept("x", "丙", "y"), concept("y", "丁", "x")))
    g = goal(concept("a", "甲", "b"), concept("b", "乙", "a"), *g.concepts).model_copy(
        update={"relations": g.relations})
    fresh = new_cells(["a", "b", "x", "y"], k_for(g))
    co = concept_order(g, fresh)
    assert set(co.dropped) == {("b", "a"), ("a", "b")}
    assert co.joined == (("x", "y"),)
    assert rule_groups(g, fresh)[-1].concepts == ["x", "y"]


def test_same_page_prereq_is_fine():
    g = transformer()
    k = k_for(g, {f"{c}.{a.value}": Band.KNOWN for c in ("vec", "soft", "tf") for a in ALL})
    assert _check(pages(["dot", "attn", "tf"]), g, k) == []   # prereqs on the same page


def test_coverage_is_a_diagnostic_concept_left_out():
    raw = pages(["vec", "soft"], ["dot", "attn"])
    assert _blocking(raw) == []
    vs = _check(raw)
    assert _rules(vs) == ["coverage"] and "Transformer" in vs[0].message


def test_coverage_target_without_new_cells_still_needs_a_page():
    g = transformer()
    k = k_for(g, {f"tf.{a.value}": Band.KNOWN for a in ALL})
    vs = _check(pages(["vec", "soft"], ["dot", "attn"]), g, k)
    assert _rules(vs) == ["coverage"] and "学习目标" in vs[0].message


def test_known_non_target_concept_may_be_left_out():
    g = transformer()
    k = k_for(g, {f"vec.{a.value}": Band.KNOWN for a in ALL})
    assert _check(pages(["soft", "dot"], ["attn", "tf"]), g, k) == []


def test_hard_cycle_pages_depend_on_each_other_blocks():
    # page 1 holds attn (needs dot on page 2); page 2 holds dot (needs vec on page 1)
    raw = pages(["vec", "attn"], ["dot", "soft"], ["tf"])
    g = verified(transformer())
    vs = _check(raw, g)
    assert _rules(vs) == ["cycle"]          # not reported as order: swapping pages cannot fix it
    assert _blocking(raw, g) == vs          # and it blocks
    assert "重新分组" in vs[0].message


def test_soft_cycle_keeps_the_grouping_and_drops_the_backward_edges():
    """Same grouping, new concepts: the backward soft edges (attn on page 1 needs dot and soft
    on page 2) are dropped and noted; page 1 stays first (vec → dot still holds)."""
    raw = pages(["vec", "attn"], ["dot", "soft"], ["tf"])
    assert _blocking(raw) == []
    vs = _check(raw)
    assert _rules(vs) == ["order"] and all("不确定" in v.message for v in vs)
    o = _order(raw)
    assert o.order == (1, 2, 3) and o.check.dropped == ("dot→attn", "soft→attn")


# --- shape -----------------------------------------------------------------------------------

def test_shape_violations_block():
    assert _rules(_blocking({"pages": []})) == ["shape"]
    unknown = _blocking(pages(["vec", "soft"], ["dot", "attn"], ["tf", "bert"]))
    assert _rules(unknown) == ["shape"] and "bert" in unknown[0].message
    dup = _blocking(pages(["vec", "soft"], ["dot", "attn"], ["tf", "vec"]))
    assert _rules(dup) == ["shape"] and "只放一页" in dup[0].message
    raw = pages(["vec", "soft"], ["dot", "attn"], ["tf"])
    raw["pages"][0]["objective"] = "  "
    assert _rules(_blocking(raw)) == ["shape"]
    raw = pages(["vec", "soft"], [], ["dot", "attn"], ["tf"])
    assert _rules(_blocking(raw)) == ["shape"]


def test_long_objective_is_a_diagnostic():
    raw = pages(["vec", "soft"], ["dot", "attn"], ["tf"])
    raw["pages"][0]["objective"] = "很" * (OBJECTIVE_MAX + 1)
    assert _blocking(raw) == []
    vs = _check(raw)
    assert _rules(vs) == ["objective"] and vs[0].page == 1


# --- hub members, bounded rule objective, done only on started pages ------------------------

PREREQ_ID, TARGET_ID = "a", "b"


def _small_goal_with_prereq():
    return goal(concept(PREREQ_ID, "均值"), concept(TARGET_ID, "z 分数", PREREQ_ID, target=True),
                text="学 z-score")


def _goal_with_many_long_names(n: int, name_len: int):
    return goal(*(concept(f"c{i}", f"{i:02d}" + "长" * (name_len - 2)) for i in range(n)))


def _big_path(tmp_path):
    g = transformer()
    return plan_path(g, k_for(g), json_llm(Script().push(PURPOSE, GOOD), tmp_path))


def test_single_page_hub_lists_prerequisites(tmp_path):
    g = _small_goal_with_prereq()
    script = Script().push(PURPOSE, pages([PREREQ_ID, TARGET_ID]))   # the model: one page
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    assert script.calls(PURPOSE) == 1
    assert not path.split
    hub = path.hub()
    # the goal's prerequisite concept is a member, before the target
    assert hub.members.index(PREREQ_ID) < hub.members.index(TARGET_ID)
    assert set(hub.members) == {c.id for c in g.concepts}


def test_split_hub_members_are_page_concepts_in_order(tmp_path):
    path = _big_path(tmp_path)
    # pages: [vec, soft], [vec, soft, dot, attn], [attn, tf] -> first appearance, no repeats
    assert [list(p.concepts) for p in path.pages] == [
        ["vec", "soft"], ["vec", "soft", "dot", "attn"], ["attn", "tf"]]
    assert list(path.hub().members) == ["vec", "soft", "dot", "attn", "tf"]


def test_rule_objective_is_bounded():
    g = _goal_with_many_long_names(n=12, name_len=20)     # the names far over 80 chars
    objective = _rule_objective([c.name for c in g.concepts])
    assert len(objective) <= OBJECTIVE_MAX
    assert objective.startswith("学完这页你能讲清并用上：")
    assert objective.endswith("等 12 个概念")                 # 「…等 N 个概念」


def test_rule_groups_are_one_page_per_concept_with_whole_short_names():
    g = goal(concept("a", "均值"), concept("b", "方差"))
    fresh = new_cells(["a", "b"], k_for(g))
    assert [(p.concepts, p.objective, p.title) for p in rule_groups(g, fresh)] == [
        (["a"], "学完这页你能讲清并用上：均值", "均值"), (["b"], "学完这页你能讲清并用上：方差", "方差")]


def test_rule_objective_one_very_long_name_is_cut():
    g = goal(concept("a", "长" * 100))
    fresh = new_cells(["a"], k_for(g))
    [page] = rule_groups(g, fresh)
    objective = page.objective
    assert len(objective) <= OBJECTIVE_MAX
    assert objective.endswith("…等 1 个概念")


def test_mark_done_refuses_a_page_not_started(tmp_path):
    path = _big_path(tmp_path)
    with pytest.raises(PageNotStarted):
        mark_done(path, path.pages[1].page_id)
    started, _ = next_page(path)
    done = mark_done(started, started.pages[0].page_id)
    assert done.pages[0].status is PageStatus.DONE
    _, spec = next_page(done)
    assert spec.page_id == path.pages[1].page_id             # nothing skipped
