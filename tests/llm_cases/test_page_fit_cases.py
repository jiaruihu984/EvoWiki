"""The judgement of an existing page on another goal's point: code derives the verdict from the model's
structured fields, here scripted. live/test_live_page_fit.py is the live side."""

from evowiki.goals.page_fit import FIT_VERSION, PageText, _FitOut, fit_context, judge_fit

SHRIMP = PageText(title="鱼缸选择", profile="你在给黑壳虾挑鱼缸", sections=(("先想清楚三件事", "大小、材质、位置"),))
PRIOR = PageText(title="先验概率", profile="", sections=(("定义", "贝叶斯定理里，看证据之前的概率叫先验"),))
OK = {"same_answer": True, "definition": {"fits": True}, "scope": {"fits": True}, "examples": {"fits": True}, "steps": {"fits": True}, "own_object": {"fits": True}}
FULL = {**OK, "remaining": "all"}
DIFF = {"differences": [{"page_says": "22 度", "here": "24 到 28 度"}], "note": "孔雀鱼要 24 到 28 度"}


def _llm(*answers):
    seen = []
    queue = list(answers)

    def call(role, purpose, prompt, schema, *, params=None):
        seen.append(prompt)
        return _FitOut.model_validate(queue.pop(0))
    call.seen = seen
    return call


def _fish(llm, page, written_for="黑壳虾"):
    return judge_fit(llm, goal="孔雀鱼", point="鱼缸选择", definition="选缸", scope="", page=page, written_for=written_for)


def test_full_stands_on_the_models_own_checks_whatever_the_page_names():
    llm = _llm(FULL)
    verdict = judge_fit(llm, goal="贝叶斯推断", point="先验概率", definition="", scope="", page=PRIOR, written_for="贝叶斯定理")[0]
    assert verdict == "full" and len(llm.seen) == 1


def test_a_page_the_model_says_is_about_another_object_is_not_full():
    answer = {**FULL, "own_object": {"fits": False, "evidence": "你在给黑壳虾挑鱼缸"}}
    llm = _llm(answer, answer)
    assert _fish(llm, SHRIMP)[0] == "none"
    assert "own_object" in llm.seen[1] and "具体差异" in llm.seen[1]


def test_little_left_is_none_whatever_else_it_wrote():
    verdict, _, note = _fish(_llm({**OK, **DIFF, "remaining": "little"}), SHRIMP)
    assert (verdict, note) == ("none", "")


def test_a_core_answer_that_changes_with_the_object_is_none():
    verdict, _, note = _fish(_llm({**OK, **DIFF, "same_answer": False, "remaining": "most"}), SHRIMP)
    assert (verdict, note) == ("none", "")


def test_stated_differences_are_partial():
    verdict, _, note = _fish(_llm({**OK, **DIFF, "remaining": "most"}), SHRIMP)
    assert (verdict, note) == ("partial", "孔雀鱼要 24 到 28 度")


def test_a_failed_check_with_no_difference_is_asked_again_then_none():
    bad = {**FULL, "steps": {"fits": False, "evidence": "水温 22 度"}}
    llm = _llm(bad, bad)
    assert _fish(llm, PRIOR)[0] == "none" and len(llm.seen) == 2


def test_a_new_way_of_judging_judges_again():
    assert FIT_VERSION == "page-fit@v3"
    assert fit_context("孔雀鱼", "水温", "", ()) != fit_context("孔雀鱼", "水温", "别的范围", ())
