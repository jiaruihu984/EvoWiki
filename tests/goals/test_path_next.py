import pytest

from evowiki.goals.contracts import Band
from evowiki.goals.guardrails import OtherPage
from evowiki.goals.outline_context import k_summary
from evowiki.goals.path import (
    PURPOSE,
    PageStatus,
    mark_done,
    next_page,
    page_learned,
    plan_path,
    refresh,
)
from tests.goals.llm_script import Script
from tests.goals.test_path_fakes import ALL, GOOD, concept, goal, json_llm, k_for, transformer


def _path(tmp_path, script=None):
    g = transformer()
    script = script or Script()
    script.push(PURPOSE, GOOD)
    return g, plan_path(g, k_for(g), json_llm(script, tmp_path)), script


def _known(*concepts, band=Band.KNOWN):
    return {f"{c}.{a.value}": band for c in concepts for a in ALL}


# --- next page: one at a time, only on request -----------------------------------------------

def test_planning_generates_no_page(tmp_path):
    _, path, _ = _path(tmp_path)
    assert all(p.status is PageStatus.NOT_STARTED for p in path.pages)


def test_next_page_starts_exactly_one_page_and_calls_no_llm(tmp_path):
    _, path, script = _path(tmp_path)
    before = script.calls()
    path1, spec1 = next_page(path, guidelines=("先给例子",))
    assert script.calls() == before
    assert [p.status for p in path1.pages] == [PageStatus.LEARNING, PageStatus.NOT_STARTED,
                                               PageStatus.NOT_STARTED]
    assert path.pages[0].status is PageStatus.NOT_STARTED        # input not mutated
    assert spec1.page_id == path.pages[0].page_id and spec1.targets == ("vec", "soft")
    assert spec1.guidelines == ("先给例子",)
    assert "第 1/3 页" in spec1.goal and GOOD["pages"][0]["objective"] in spec1.goal


def test_second_page_carries_support_concepts_and_earlier_pages(tmp_path):
    _, path, _ = _path(tmp_path)
    path1, _ = next_page(path)
    extra = OtherPage(page_id="p-x", title="别的页", concepts=("x",))
    path2, spec2 = next_page(path1, other_pages=(extra,))
    assert [c.id for c in spec2.concepts] == ["vec", "soft", "dot", "attn"]
    assert spec2.targets == ("dot", "attn")
    assert spec2.roles == {"vec": "support", "soft": "support"}
    assert spec2.concept("dot").prereqs == ("vec",)
    assert spec2.concept("attn").prereqs == ("dot", "soft")
    assert [o.page_id for o in spec2.other_pages] == [path.pages[0].page_id, "p-x"]
    assert path2.pages[0].status is PageStatus.LEARNING         # not auto-finished


def test_next_page_is_none_when_every_page_has_started(tmp_path):
    _, path, _ = _path(tmp_path)
    for _ in range(3):
        path, _ = next_page(path)
    assert next_page(path) is None


def test_single_page_path_spec_matches_phase1(tmp_path):
    g = goal(concept("b", "z 分数", target=True), text="学 z-score")
    path = plan_path(g, k_for(g), json_llm(Script(), tmp_path))
    _, spec = next_page(path)
    assert spec.goal == "学 z-score" and spec.targets == ("b",)
    assert [c.id for c in spec.concepts] == ["b"] and spec.roles == {}


def test_model_single_page_spec_keeps_the_goal_and_no_roles(tmp_path):
    g = goal(concept("a", "均值"), concept("b", "z 分数", "a", target=True), text="学 z-score")
    script = Script().push(PURPOSE, {"pages": [{"concepts": ["a", "b"],
                                                "objective": "学完这页你能算 z 分数"}]})
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    _, spec = next_page(path)
    assert spec.goal == "学 z-score" and spec.targets == ("a", "b")
    assert [c.id for c in spec.concepts] == ["a", "b"] and spec.roles == {}


def test_page_spec_feeds_the_outline_inputs(tmp_path):
    script = Script()
    _, path, _ = _path(tmp_path, script)
    _, spec = next_page(path)
    g = transformer()
    text = k_summary(spec, k_for(g))              # the writer's outline part reads it
    assert spec.targets == ("vec", "soft") and "- vec" in text and "- soft" in text


# --- done ------------------------------------------------------------------------------------

def test_page_learned_needs_every_target_cell_likely_or_better(tmp_path):
    g, path, _ = _path(tmp_path)
    page = path.pages[0]
    assert not page_learned(page, k_for(g))
    mixed = {**_known("vec"), **_known("soft", band=Band.LIKELY)}
    assert page_learned(page, k_for(g, mixed))
    assert not page_learned(page, k_for(g, {**mixed, "soft.term": Band.SHAKY}))
    assert page_learned(path.pages[1], k_for(g, _known("dot", "attn")))  # support cells don't count


def test_refresh_finishes_only_pages_being_learned(tmp_path):
    g, path, _ = _path(tmp_path)
    path1, _ = next_page(path)
    k = k_for(g, _known("vec", "soft", "dot", "attn"))
    done = refresh(path1, k)
    assert done.pages[0].status is PageStatus.DONE and done.pages[0].done_by == "k"
    assert done.pages[1].status is PageStatus.NOT_STARTED       # learned elsewhere: no skipping
    assert refresh(done, k_for(g)).pages[0].status is PageStatus.DONE   # sticky
    assert refresh(path1, k_for(g)) is path1                     # nothing changed


def test_user_can_mark_a_page_done(tmp_path):
    _, path, _ = _path(tmp_path)
    path1, _ = next_page(path)
    done = mark_done(path1, path1.pages[0].page_id)
    assert done.pages[0].status is PageStatus.DONE and done.pages[0].done_by == "user"
    with pytest.raises(KeyError):
        mark_done(path1, "nope")


# --- V: goal hub with ordered members ---------------------------------------------------------

def test_path_is_a_goal_hub_with_ordered_members(tmp_path):
    _, path, _ = _path(tmp_path)
    hub = path.hub()
    assert hub.kind == "goal" and hub.name == "我想学 Transformer" and hub.hub_id == path.path_id
    assert hub.members == ("vec", "soft", "dot", "attn", "tf")
    assert [p for p, _ in hub.pages] == [p.page_id for p in path.pages]
