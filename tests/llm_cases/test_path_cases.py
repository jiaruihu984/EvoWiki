"""goals.path (path@v6) offline cases. Information flow (the product specification
2026-10-11-llm-information-flow.md, rule PATH): path planning sees the goal, every concept of the
goal with its id, name, new cells and prerequisites — listed in the code's topological order, with
the KST fringe (【现在就能学】) and the concepts existing pages teach (【已有页《…》讲过】) marked —
the learning targets, and the user's existing pages and paths (`plan_path(existing=..., taught=...)`,
built by wikiapp/goal.py `_existing_lines` / `_taught`). It never sees K numbers (m / u), the
learner's description or quiz transcript, and no page-count or capacity limit. There is no
verifier call (role "generator" only).
Hybrid harness (reuse decision 学习路径): the model chunks and writes title / abstract / concepts /
objective; code orders the pages. A raw order that puts a concept before an unsettled
prerequisite is repaired (pages moved, a note, `LearningPath.order` counts it); a page cycle of
soft edges keeps the grouping (note); a cycle of hard edges goes back, every time → the rule
version. Shape errors and unparseable output go back verbatim, an over-big single page is
accepted, a stored path with the same goal key is respected (no call), a replanned path keeps its
started pages unchanged."""

import re

import pytest

from evowiki.goals.contracts import Band
from evowiki.goals.path import (
    EXISTING_BLOCK,
    MAX_RETRIES,
    PURPOSE,
    RULE_OBJECTIVE,
    PageStatus,
    plan_path,
)
from tests.goals.test_path_fakes import (
    ALL,
    GOOD,
    concept,
    goal,
    k_for,
    pages,
    transformer,
    verified,
)
from tests.wikiapp.flat_goal import flat_goal

EXISTING = ("- 页《向量入门》（学完）：讲 向量与矩阵",
            "- 学习路径「学线性代数」第 2 页（还没开始）：学完这页你能求特征值；讲 特征值与特征向量")
EXISTING_HEAD = EXISTING_BLOCK.strip().splitlines()[0]


class FakeLLM:
    """Queued outputs (dict → validated, Exception → raised); only the generator role exists."""

    def __init__(self, *outs):
        self.outs = list(outs)
        self.prompts: list[str] = []

    def __call__(self, role, purpose, prompt, schema):
        assert role == "generator" and purpose == PURPOSE
        self.prompts.append(prompt)
        out = self.outs.pop(0)
        if isinstance(out, Exception):
            raise out
        return schema.model_validate(out)


def plan(*outs, g=None, k=None, **kw):
    g = g or transformer()
    llm = FakeLLM(*outs)
    return plan_path(g, k or k_for(g), llm, **kw), llm


# --- what the prompt must and must not contain ------------------------------------------------

def test_prompt_knows_goal_concepts_new_cells_prereqs_and_targets():
    g = transformer()
    _, llm = plan(GOOD, g=g, k=k_for(g, {"vec.term": Band.KNOWN, "vec.notation": Band.LIKELY}))
    p = llm.prompts[0]
    for s in ("学习目标：「我想学 Transformer」", "- vec 向量与矩阵：新格子 4 个",
              "- soft softmax：新格子 6 个", "前置：dot、soft", "- tf Transformer", "【学习目标】"):
        assert s in p, s
    assert p.count("【学习目标】") == 1


def test_prompt_has_no_k_numbers_no_counts_and_no_capacity():
    _, llm = plan(GOOD)
    p = llm.prompts[0]
    assert "m=" not in p and "u=" not in p
    assert not re.search(r"最多\s*\d|不超过\s*\d|容量|\d+\s*个新格子以内", p)


def test_existing_pages_and_paths_reach_the_prompt_verbatim():
    _, llm = plan(GOOD, existing=EXISTING)
    p = llm.prompts[0]
    assert EXISTING_HEAD in p and "不要重讲" in p
    for line in EXISTING:
        assert line in p


def test_no_existing_means_no_existing_block():
    _, llm = plan(GOOD)
    assert EXISTING_HEAD not in llm.prompts[0]


def test_existing_lines_reach_every_retry():
    _, llm = plan(ValueError("bad json"), pages(["vec"], ["vec", "tf"]), GOOD, existing=EXISTING)
    assert len(llm.prompts) == 3
    assert all(line in p for p in llm.prompts for line in EXISTING)


def test_prompt_lists_concepts_in_code_order_with_fringe_and_taught_marks():
    """Intake listed the target first; the prompt lists prerequisites first. vec is known, so dot
    is ready to learn; softmax is taught by an existing page."""
    g = transformer()
    back = goal(*reversed(g.concepts), text=g.goal)
    _, llm = plan(GOOD, g=back, k=k_for(back, {f"vec.{a.value}": Band.KNOWN for a in ALL}),
                  taught={"soft": "softmax 入门"})
    p = llm.prompts[0]
    rows = [line for line in p.splitlines()   # the order list, not the fenced scope list (D09)
            if re.match(r"- (vec|soft|dot|attn|tf) ", line) and "新格子" in line]
    assert [r.split()[1] for r in rows] == ["soft", "vec", "dot", "attn", "tf"]
    assert "【现在就能学】" in rows[2] and "【现在就能学】" not in rows[3]
    assert "【已有页《softmax 入门》讲过】" in rows[0] and "【现在就能学】" not in rows[0]
    for s in ('"title"', '"abstract"', "先后顺序由代码定", "不要再放进新页"):
        assert s in p, s


def test_title_and_abstract_are_kept_and_a_missing_title_gets_the_names():
    out = {"pages": [
        {"title": "向量与 softmax", "abstract": "先把分数变成权重。", "concepts": ["vec", "soft"],
         "objective": "学完这页你能把分数变成权重"},
        {"concepts": ["dot", "attn", "tf"], "objective": "学完这页你能算注意力"}]}
    path, _ = plan(out)
    assert (path.pages[0].title, path.pages[0].abstract) == ("向量与 softmax", "先把分数变成权重。")
    assert path.pages[1].title == "点积相似度、注意力、Transformer"


# --- harness: order is code's -----------------------------------------------------------------

def test_raw_order_violation_is_repaired_by_moving_pages_and_counted():
    path, llm = plan(pages(["dot", "attn"], ["tf"], ["vec", "soft"]))
    assert len(llm.prompts) == 1 and path.source == "llm" and path.rejected == ()
    assert [p.targets for p in path.pages] == [("vec", "soft"), ("dot", "attn"), ("tf",)]
    assert [p.page_id[-3:] for p in path.pages] == ["-p1", "-p2", "-p3"]
    o = path.order
    assert o is not None and o.reordered and (o.edges, o.violations, o.soft) == (3, 2, 2)
    assert any("排到了" in line for line in path.log)
    assert any("原始顺序违反前置 2/3 条" in line for line in path.log)


def test_soft_page_cycle_keeps_the_grouping_without_a_retry():
    path, llm = plan(pages(["vec", "attn"], ["dot", "soft"], ["tf"]))
    assert len(llm.prompts) == 1 and path.source == "llm" and path.rejected == ()
    assert path.order is not None and path.order.dropped == ("dot→attn", "soft→attn")
    assert path.pages[0].concepts == ("dot", "soft", "vec", "attn")    # dot / soft as support


def test_hard_page_cycle_goes_back_then_passes():
    g = verified(transformer())
    path, llm = plan(pages(["vec", "attn"], ["dot", "soft"], ["tf"]), GOOD, g=g)
    assert len(llm.prompts) == 2 and path.source == "llm"
    assert "互相依赖" in llm.prompts[1] and path.rejected[0][0].rule == "cycle"


def test_reteaching_a_taught_concept_is_a_note_not_a_rejection():
    path, llm = plan(GOOD, taught={"soft": "softmax 入门"})
    assert len(llm.prompts) == 1 and path.rejected == ()
    assert any(v.rule == "reteach" and "softmax 入门" in v.message for v in path.notes)


def test_replanned_path_keeps_started_pages_unchanged():
    first, _ = plan(GOOD)
    started = first.model_copy(update={"pages": (
        first.pages[0].model_copy(update={"status": PageStatus.DONE, "done_by": "user"}),
        *first.pages[1:])})
    g = transformer("我想学 Transformer 的注意力")
    path, llm = plan(pages(["dot", "attn"], ["tf"]), g=g, previous=started)
    assert path.pages[0] == started.pages[0]                         # never changed
    assert [p.targets for p in path.pages[1:]] == [("dot", "attn"), ("tf",)]
    assert "【已有页" in llm.prompts[0]                                 # vec / soft count as taught
    assert len({p.page_id for p in path.pages}) == 3


# --- harness: outputs -------------------------------------------------------------------------

def test_valid_output_is_used_as_is():
    path, llm = plan(GOOD)
    assert len(llm.prompts) == 1 and path.source == "llm" and path.split
    assert [p.targets for p in path.pages] == [("vec", "soft"), ("dot", "attn"), ("tf",)]
    assert path.pages[2].objective == GOOD["pages"][2]["objective"]


def test_unparseable_output_goes_back_then_passes():
    path, llm = plan(ValueError("not json"), GOOD)
    assert path.source == "llm" and len(llm.prompts) == 2
    assert "上一版输出不是合法的路径 JSON" in llm.prompts[1] and "这是第 2 次" in llm.prompts[1]
    assert path.rejected and path.rejected[0][0].rule == "shape"


@pytest.mark.parametrize(("bad", "message"), [
    (pages(["vec", "soft", "ghost"], ["dot", "attn", "tf"]), "引用了不存在的概念 ghost"),
    (pages(["vec", "soft", "dot"], ["dot", "attn", "tf"]), "同时出现在第 1 页和第 2 页"),
    ({"pages": [{"concepts": ["vec", "soft", "dot", "attn", "tf"], "objective": " "}]},
     "第 1 页 没有写「学完这页你能做什么」"),
    ({"pages": []}, "没有任何页"),
])
def test_shape_errors_go_back_verbatim(bad, message):
    path, llm = plan(bad, GOOD)
    assert path.source == "llm" and message in llm.prompts[1]
    assert message not in llm.prompts[0]


def test_hard_cycle_every_time_falls_back_to_the_rule_version_in_prereq_order():
    cyc = pages(["vec", "attn"], ["dot", "soft"], ["tf"])
    path, llm = plan(*[cyc] * (1 + MAX_RETRIES), g=verified(transformer()))
    assert len(llm.prompts) == 1 + MAX_RETRIES and path.source == "fallback"
    assert all("互相依赖" in p for p in llm.prompts[1:])
    order = [p.targets[0] for p in path.pages]
    assert order == ["vec", "soft", "dot", "attn", "tf"]
    assert all(p.objective.startswith(RULE_OBJECTIVE) for p in path.pages)
    assert len(path.rejected) == 1 + MAX_RETRIES


def test_over_big_single_page_is_accepted_not_a_fallback():
    """Capacity was removed (no hard-coded guardrails): 30 new cells on one page is the model's
    call, kept as an LLM path with no rejection."""
    path, llm = plan(pages(["vec", "soft", "dot", "attn", "tf"]))
    assert path.source == "llm" and not path.split and path.rejected == ()
    assert path.pages[0].new_cells == 30 and len(llm.prompts) == 1


def test_order_and_coverage_departures_are_notes_not_rejections():
    path, llm = plan(pages(["dot", "attn"], ["vec", "soft"]))         # tf left out, prereqs later
    assert path.source == "llm" and path.rejected == () and len(llm.prompts) == 1
    assert {v.rule for v in path.notes} == {"coverage", "order"}


def test_a_stored_path_with_the_same_goal_is_returned_without_a_call():
    first, _ = plan(GOOD)
    g = transformer()
    again, llm = plan(g=g, k=k_for(g, default=Band.KNOWN), previous=first, existing=EXISTING)
    assert again is first and llm.prompts == []


def test_single_concept_needs_no_call():
    g = goal(concept("z", "z 分数", target=True), text="学 z 分数")
    path, llm = plan(g=g, existing=EXISTING)
    assert path.source == "single" and llm.prompts == []


# --- through the goal flow (wikiapp/goal.py _existing_lines) -----------------------------------

def test_flow_second_goal_sees_started_page_and_unstarted_path_pages():
    """A 3-page path whose first page was created, then a second goal: the second path prompt
    lists the started page (as a page, with its status: no body yet) and only the not-started path
    pages."""
    from evowiki.wikiapp.llm import CountingGateway
    from evowiki.wikiapp.paths import next_path_page
    from evowiki.wikiapp.repo import MemoryRepo
    from tests.wikiapp.goal_script import push_no_question
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_goal import INTAKE_1, NOW, Ids
    from tests.wikiapp.test_paths import big_goal

    env = (MemoryRepo(["u1"]), Script(), Ids())
    repo, s, ids = env
    res, _ = big_goal(env)
    assert EXISTING_HEAD not in s.requests(PURPOSE)[0].prompt          # nothing yet
    next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids)
    s.push("goals.intake", INTAKE_1)
    push_no_question(s)
    s.push(PURPOSE, {"pages": [{"concepts": ["c7", "c8"], "objective": "学完这页你能说清 z"}]})
    flat_goal(repo, CountingGateway(s.gateway()), "u1", "看懂 z 分数", "", now=NOW, new_id=ids)
    p = s.requests(PURPOSE)[-1].prompt
    assert EXISTING_HEAD in p
    assert "- 页《总体与样本、抽样分布、零假设》（还没写好）：讲 总体与样本、抽样分布、零假设" in p
    assert ("学习路径「学统计推断」第 2 页《置信区间、检验统计量》（还没开始）："
            "学完这页你能算置信区间和检验统计量") in p
    assert "学习路径「学统计推断」第 3 页《p 值》（还没开始）" in p
    assert "第 1 页（还没开始）" not in p                               # started: listed as a page
    assert s.roles(PURPOSE) == ["anthropic", "anthropic"]               # generator only
