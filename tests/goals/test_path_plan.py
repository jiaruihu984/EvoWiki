from evowiki.goals.contracts import Band
from evowiki.goals.path import (
    MAX_RETRIES,
    OBJECTIVE_MAX,
    PURPOSE,
    PageStatus,
    as_draft,
    check_path,
    goal_key,
    new_cells,
    plan_path,
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

# pages 1 and 2 depend on each other: with hard edges (verified prerequisites) unusable, so it goes back
CYCLE_BAD = pages(["vec", "attn"], ["dot", "soft"], ["tf"])


def _fresh(g, k):
    return new_cells([c.id for c in g.concepts], k)


def test_single_concept_is_one_page_without_any_llm_call(tmp_path):
    g = goal(concept("b", "z 分数", target=True), text="学 z-score")
    script = Script()                          # nothing scripted: any LLM call fails the test
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    assert script.calls() == 0
    assert not path.split and path.source == "single" and len(path.pages) == 1
    page = path.pages[0]
    assert page.targets == ("b",) and page.concepts == ("b",)
    assert page.objective == "学 z-score" and page.new_cells == 6
    assert page.status is PageStatus.NOT_STARTED
    assert path.capacity == 0 and path.notes == ()


def test_two_concepts_ask_the_model_and_one_page_is_a_valid_answer(tmp_path):
    g = goal(concept("a", "均值"), concept("b", "z 分数", "a", target=True), text="学 z-score")
    script = Script().push(PURPOSE, pages(["a", "b"]))
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    assert script.calls(PURPOSE) == 1
    assert not path.split and path.source == "llm" and len(path.pages) == 1
    assert path.pages[0].targets == ("a", "b") and path.pages[0].new_cells == 12


def test_a_big_goal_on_one_page_is_accepted(tmp_path):
    g = transformer()                          # 30 new cells: no capacity any more
    script = Script().push(PURPOSE, pages(["vec", "soft", "dot", "attn", "tf"]))
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    assert path.source == "llm" and not path.split and path.rejected == ()


def test_big_goal_is_split_by_the_llm(tmp_path):
    g = transformer()
    script = Script().push(PURPOSE, GOOD)
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    assert script.calls(PURPOSE) == 1
    assert path.split and path.source == "llm" and path.new_cells == 30
    assert [p.targets for p in path.pages] == [("vec", "soft"), ("dot", "attn"), ("tf",)]
    assert [p.new_cells for p in path.pages] == [12, 12, 6]
    assert path.pages[1].objective == GOOD["pages"][1]["objective"]
    assert path.pages[1].concepts == ("vec", "soft", "dot", "attn")   # support first
    assert all(p.status is PageStatus.NOT_STARTED for p in path.pages)
    assert check_path(as_draft(path), g, _fresh(g, k_for(g))) == []


def test_prompt_carries_concepts_new_cells_and_prereqs_and_no_capacity(tmp_path):
    g = transformer()
    script = Script().push(PURPOSE, GOOD)
    plan_path(g, k_for(g, {"vec.term": Band.KNOWN}), json_llm(script, tmp_path))
    p = script.requests(PURPOSE)[0].prompt
    for s in ("我想学 Transformer", "怎样分页最好", "vec 向量与矩阵：新格子 5 个",
              "attn 注意力", "前置：dot、soft", "【学习目标】", "学完这页你能"):
        assert s in p, s
    for s in ("容量", "最多", "不超过"):
        assert s not in p, s


def test_violations_go_back_verbatim_then_pass(tmp_path):
    g = verified(transformer())
    script = Script().push(PURPOSE, CYCLE_BAD, GOOD)
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    assert path.source == "llm" and script.calls(PURPOSE) == 2
    assert [v.rule for v in path.rejected[0]] == ["cycle"]
    retry = script.requests(PURPOSE)[1].prompt
    assert path.rejected[0][0].message in retry and "第 2 次" in retry


def test_two_retries_then_rule_version(tmp_path):
    g = verified(transformer())
    script = Script().push(PURPOSE, *[CYCLE_BAD] * (1 + MAX_RETRIES))
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    assert script.calls(PURPOSE) == 1 + MAX_RETRIES == 3
    assert path.source == "fallback" and len(path.rejected) == 3 and path.split
    # one page per required concept, in prerequisite topological order
    assert [p.targets for p in path.pages] == [("vec",), ("soft",), ("dot",), ("attn",), ("tf",)]
    assert all(p.objective.startswith("学完这页你能") for p in path.pages)
    assert check_path(as_draft(path), g, _fresh(g, k_for(g))) == []
    assert "规则版" in path.log[-1]


def test_unparseable_output_counts_as_a_failed_attempt(tmp_path):
    g = transformer()
    script = Script().push(PURPOSE, "not json", GOOD)
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    assert path.source == "llm" and path.rejected[0][0].rule == "shape"


def test_coverage_order_and_long_objective_are_notes_not_rejections(tmp_path):
    g = transformer()
    raw = pages(["dot", "attn"], ["vec", "soft"])        # tf left out, dot before vec
    raw["pages"][0]["objective"] = "学完这页你能" + "很" * OBJECTIVE_MAX
    script = Script().push(PURPOSE, raw)
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    assert path.source == "llm" and script.calls(PURPOSE) == 1 and path.rejected == ()
    assert sorted({v.rule for v in path.notes}) == ["coverage", "objective", "order"]
    assert all(any(v.message in line for line in path.log) for v in path.notes)
    # code moved the vec / soft page first (dot needs vec); the long objective is page 2 now
    assert [p.targets for p in path.pages] == [("vec", "soft"), ("dot", "attn")]
    assert len(path.pages[1].objective) == OBJECTIVE_MAX
    assert path.order is not None and path.order.reordered and path.order.violations == 2


def test_rule_version_leaves_out_known_non_target_concepts(tmp_path):
    g = transformer()
    k = k_for(g, {f"vec.{a.value}": Band.KNOWN for a in ALL})
    script = Script().push(PURPOSE, *[{"pages": []}] * 3)
    path = plan_path(g, k, json_llm(script, tmp_path))
    assert [p.targets for p in path.pages] == [("soft",), ("dot",), ("attn",), ("tf",)]
    assert "vec" in path.pages[1].concepts           # still there, as support for dot


def test_same_goal_is_not_replanned_even_if_k_changed(tmp_path):
    g = transformer()
    script = Script().push(PURPOSE, GOOD)
    llm = json_llm(script, tmp_path)
    first = plan_path(g, k_for(g), llm)
    again = plan_path(transformer(text=" 我想学  transformer "), k_for(g, default=Band.SHAKY),
                      llm, previous=first)
    assert again is first and script.calls(PURPOSE) == 1


def test_changed_goal_is_replanned(tmp_path):
    g = transformer()
    script = Script().push(PURPOSE, GOOD, GOOD)
    llm = json_llm(script, tmp_path)
    first = plan_path(g, k_for(g), llm)
    other = transformer(text="我想学 BERT")
    second = plan_path(other, k_for(other), llm, previous=first)
    assert second is not first and second.goal_key != first.goal_key
    assert script.calls(PURPOSE) == 2


def test_goal_key_normalizes_text_and_target_order():
    assert goal_key("我想学 Transformer", ["b", "a"]) == goal_key(" 我想学transformer", ["a", "b"])
    assert goal_key("我想学 Transformer", ["a"]) != goal_key("我想学 Transformer", ["a", "b"])


def test_the_page_title_is_the_models_even_for_one_concept(tmp_path):
    # path@v10: a page's title is never derived from a node name; no title → the targets' names
    g = transformer()
    titles = ["从向量到权重", "", "把注意力叠起来"]
    answer = {"pages": [{**p, "title": t} for p, t in zip(GOOD["pages"], titles, strict=True)]}
    script = Script().push(PURPOSE, answer)
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    names = {c.id: c.name for c in g.concepts}
    assert path.pages[2].targets == ("tf",) and path.pages[2].title == "把注意力叠起来"
    assert path.pages[1].title == "、".join(names[c] for c in path.pages[1].targets)
    assert path.pages[0].title == "从向量到权重"
    # one concept and no model call: the only name there is
    single = goal(concept("b", "z 分数", target=True), text="学 z-score")
    only = plan_path(single, k_for(single), json_llm(Script(), tmp_path))
    assert only.pages[0].title == "z 分数"


def test_path_prompt_lets_concepts_share_a_page_default_separate(tmp_path):
    g = transformer()
    script = Script().push(PURPOSE, GOOD)
    plan_path(g, k_for(g), json_llm(script, tmp_path))
    p = script.requests(PURPOSE)[0].prompt
    assert "一个点一页" not in p and "标题就写这个概念的名字" not in p and "一页能讲清" not in p
    assert "可以合成一页" in p and "拿不准时分开" in p and "（1/3）" in p
    assert "只是提到或用到的不写" in p          # concepts = what the page teaches
    assert "真正互不相关的两件事才分两页" not in p


def test_each_page_keeps_the_question_it_answers_and_the_prompt_asks_for_it(tmp_path):
    g = transformer()
    questions = ["怎么把一组分数变成权重", "怎么看出哪个词最该被注意", "数据怎么一层层流过去"]
    answer = {"pages": [{**p, "question": f"  {q}  "} for p, q in zip(GOOD["pages"], questions)]}
    script = Script().push(PURPOSE, answer)
    path = plan_path(g, k_for(g), json_llm(script, tmp_path))
    assert [p.question for p in path.pages] == questions
    assert [p.question for p in as_draft(path).pages] == questions
    p = script.requests(PURPOSE)[0]
    assert '"question"' in p.prompt and "不写方法和计划" in p.prompt and "过拟合" in p.prompt
    # an older answer without the field still plans (the row shows no line)
    (tmp_path / "b").mkdir()
    plain = plan_path(g, k_for(g), json_llm(Script().push(PURPOSE, GOOD), tmp_path / "b"))
    assert [p.question for p in plain.pages] == ["", "", ""]
