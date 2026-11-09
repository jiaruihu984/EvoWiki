"""sources.exam extraction (materials phase 5 F): batching, answer-key quotes, structure checks,
parents, answer-page matching, batch failures. Fake JsonLLM only."""

from itertools import count

import pytest

from evowiki.llm.gateway import LLMParseError
from evowiki.llm.providers import ProviderRejectedError
from evowiki.llm.usage import BudgetExceededError
from evowiki.sources.exam import (
    EXAM_BATCH_CHARS,
    ExamPlan,
    Previous,
    RawAnswer,
    RawItem,
    answer_section_start,
    batches,
    extract_items,
    norm_number,
    problem_code,
)
from evowiki.sources.models import SectionRow
from evowiki.wikiapp.llm import LLMUnavailable
from tests.sources._fakes import FakeLLM


def _sec(i: int, text: str, *, title: str | None = None, page: int | None = 3) -> SectionRow:
    return SectionRow(f"s{i}", "src", None, i, 0, title if title is not None else f"第{i}节",
                      page, page, text, len(text), None, "text")


def _ids():
    c = count(1)
    return lambda prefix: f"{prefix}{next(c)}"


def _run(sections, *plans):
    llm = FakeLLM(*plans)
    return extract_items(sections, llm=llm, new_id=_ids()), llm


Q1 = "〔p.3〕1. 下列哪个是质数？ A. 4 B. 5 C. 6 D. 8 答案：B"


def _single(**kw) -> RawItem:
    base = {"number": "1", "kind": "single", "stem": "下列哪个是质数？",
            "options": ["A. 4", "B. 5", "C. 6", "D. 8"], "page": 3}
    return RawItem(**{**base, **kw})


# ---- batches

def test_batches_skip_heading_only_sections_and_merge_small_ones():
    secs = [_sec(1, "〔p.1〕  "), _sec(2, "〔p.2〕一题"), _sec(3, "〔p.3〕二题"),
            _sec(4, "〔p.4〕" + "长" * 1000)]
    bs = batches(secs)
    assert [b.section_id for b in bs] == ["s2", "s4"]
    assert bs[0].pages == (2, 3) and "一题" in bs[0].text and "二题" in bs[0].text


def test_batches_split_a_big_section_at_page_marks_without_cutting_a_page():
    pages = [f"〔p.{n}〕" + "字" * 2500 for n in range(1, 6)]
    bs = batches([_sec(1, "".join(pages))])
    assert [b.pages for b in bs] == [(1, 2), (3, 4), (5,)]
    assert all(b.section_id == "s1" for b in bs)
    for b in bs:
        for n in b.pages:
            assert f"〔p.{n}〕" + "字" * 2500 in b.text


def test_batches_a_single_page_over_the_limit_is_a_batch_alone():
    text = "〔p.1〕" + "字" * (EXAM_BATCH_CHARS + 100) + "〔p.2〕" + "字" * 10
    bs = batches([_sec(1, text)])
    assert [b.pages for b in bs] == [(1,), (2,)]


def test_small_sections_merge_only_up_to_the_batch_limit():
    secs = [_sec(i, f"〔p.{i}〕" + "字" * 700) for i in range(1, 11)]
    bs = batches(secs)
    assert len(bs) == 2 and bs[0].section_id == "s1" and bs[1].section_id == "s9"
    assert all(len(b.text) <= EXAM_BATCH_CHARS for b in bs)


def test_norm_number():
    assert norm_number("第３题") == "3"
    assert norm_number("（2）") == "2"
    assert norm_number(" 12. ") == "12"
    assert norm_number("一、") == "一"


# ---- answer key

def test_a_quoted_key_is_kept_and_normalized():
    out, llm = _run([_sec(1, Q1)], ExamPlan(items=[_single(answer="b", answer_quote="答案： B")]))
    (item,) = out.items
    assert item.answer == "B" and item.has_key and item.status == "ok" and item.problem is None
    assert item.item_id == "si1" and item.section_id == "s1" and item.page == 3
    assert item.source_id == "src" and item.attach == "pending"
    assert llm.calls[0][:2] == ("generator", "sources.exam")


def test_an_invented_key_is_dropped():
    out, _ = _run([_sec(1, Q1)], ExamPlan(items=[_single(answer="C", answer_quote="答案：C",
                                                          explanation="自己算的")]))
    (item,) = out.items
    assert item.answer is None and not item.has_key and item.problem == "answer_not_in_text"
    assert item.status == "ok" and item.explanation is None


def test_a_quote_in_the_text_that_does_not_carry_the_answer_is_rejected():
    out, _ = _run([_sec(1, Q1)], ExamPlan(items=[_single(answer="C", answer_quote="答案：B")]))
    assert out.items[0].answer is None and out.items[0].problem == "answer_not_in_text"


def test_no_key_in_the_material_is_not_a_problem():
    out, _ = _run([_sec(1, Q1)], ExamPlan(items=[_single()]))
    (item,) = out.items
    assert item.answer is None and not item.has_key and item.problem is None
    assert item.status == "ok"


def test_quote_matching_ignores_whitespace_and_page_marks():
    text = "〔p.3〕1. 判断：地球是圆的。\n答〔p.4〕案：√"
    raw = RawItem(number="1", kind="tf", stem="地球是圆的。", answer="√", answer_quote="答案：√")
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    (item,) = out.items
    assert item.answer == "对" and item.has_key and item.options == ("对", "错")


def test_open_item_key_needs_only_the_quote():
    text = "〔p.3〕2. 简述牛顿第一定律。参考答案：物体在不受外力时保持静止或匀速直线运动。"
    raw = RawItem(number="2", kind="open", stem="简述牛顿第一定律。",
                  answer="物体在不受外力时保持静止或匀速直线运动。",
                  answer_quote="物体在不受外力时保持静止或匀速直线运动。")
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    assert out.items[0].has_key and out.items[0].kind == "open"


def test_open_item_key_must_be_inside_the_quote():
    """The stored reference answer is the material's text, never the model's own words."""
    text = "〔p.3〕2. 简述牛顿第一定律。参考答案：物体在不受外力时保持静止或匀速直线运动。"
    raw = RawItem(number="2", kind="open", stem="简述牛顿第一定律。",
                  answer="这是模型自己编的答案",
                  answer_quote="物体在不受外力时保持静止或匀速直线运动。")
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    item = out.items[0]
    assert not item.has_key and item.answer is None and item.problem == "answer_not_in_text"
    assert item.status == "ok"
    # a part of the quote, with different whitespace, is kept
    raw = RawItem(number="2", kind="open", stem="简述牛顿第一定律。",
                  answer="保持静止 或匀速直线运动",
                  answer_quote="参考答案：物体在不受外力时保持静止或匀速直线运动。")
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    assert out.items[0].has_key and out.items[0].answer == "保持静止 或匀速直线运动"


def test_a_quote_of_the_items_own_option_or_stem_is_no_key():
    out, _ = _run([_sec(1, Q1)], ExamPlan(items=[_single(answer="B", answer_quote="B. 5")]))
    assert not out.items[0].has_key and out.items[0].problem == "answer_not_in_text"
    text = "〔p.3〕1. 判断：下列说法对吗？地球是平的。"
    raw = RawItem(number="1", kind="tf", stem="下列说法对吗？地球是平的。", answer="对",
                  answer_quote="说法对吗")
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    assert not out.items[0].has_key


def test_a_quote_running_from_an_option_into_the_next_question_is_no_key():
    text = "〔p.3〕1. 下列哪个是质数？ A. 4 B. 5\n2. 再选 A. 6 B. 7"
    plan = ExamPlan(items=[
        _single(options=["A. 4", "B. 5"], answer="B", answer_quote="B. 5\n2. 再选"),
        RawItem(number="2", kind="single", stem="再选", options=["A. 6", "B. 7"])])
    out, _ = _run([_sec(1, text)], plan)
    assert not out.items[0].has_key and out.items[0].problem == "answer_not_in_text"


def test_an_inline_key_must_sit_between_its_own_item_and_the_next():
    text = "〔p.3〕1. 选第一个 A. x B. y\n2. 选第二个 A. x B. y\n2的答案：A"
    plan = ExamPlan(items=[
        RawItem(number="1", kind="single", stem="选第一个", options=["A. x", "B. y"],
                answer="A", answer_quote="答案：A"),
        RawItem(number="2", kind="single", stem="选第二个", options=["A. x", "B. y"],
                answer="A", answer_quote="答案：A")])
    out, _ = _run([_sec(1, text)], plan)
    q1, q2 = out.items
    assert not q1.has_key and q1.problem == "answer_not_in_text"
    assert q2.has_key and q2.answer == "A"


def test_an_inline_key_before_an_item_not_found_in_the_text_is_dropped():
    text = "〔p.3〕1. 选第一个 A. x B. y\n2. 选第二个 A. x B. y\n2的答案：A"
    plan = ExamPlan(items=[
        RawItem(number="1", kind="single", stem="选第一个", options=["A. x", "B. y"],
                answer="A", answer_quote="答案：A"),
        RawItem(number="2", kind="single", stem="请选第二个", options=["A. x", "B. y"])])
    out, _ = _run([_sec(1, text)], plan)
    assert not out.items[0].has_key and out.items[0].problem == "answer_not_in_text"


def test_an_item_whose_stem_is_not_in_the_text_is_incomplete_without_a_key():
    """sources-exam@v3: a stem the model rewrote is not the material's question: the item is
    kept as incomplete (never practiced, never attached), and its key is not read."""
    text = "〔p.3〕1. 下列哪个是质数？ A. 4 B. 5\n答案：B"
    raw = _single(stem="哪个数是质数", options=["A. 4", "B. 5"], answer="B",
                  answer_quote="答案：B")
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    (item,) = out.items
    assert (item.status, item.problem, item.has_key) == ("incomplete", "stem_not_in_text", False)
    assert any("stem_not_in_text" in d for d in out.diagnostics)


def test_another_questions_option_is_no_key():
    text = "〔p.3〕1. 下列哪个是质数？ A. 4 B. 5\n2. 再选 A. 7 B. 6"
    plan = ExamPlan(items=[
        _single(options=["A. 4", "B. 5"], answer="B", answer_quote="B. 6"),
        RawItem(number="2", kind="single", stem="再选", options=["A. 7", "B. 6"])])
    out, _ = _run([_sec(1, text)], plan)
    assert not out.items[0].has_key


def test_an_inline_key_needs_an_answer_cue_before_the_answer():
    text = "〔p.3〕1. 下列哪个是质数？ A. 4 B. 5\n（B）\n答：B"
    plan_bare = ExamPlan(items=[_single(options=["A. 4", "B. 5"], answer="B",
                                        answer_quote="（B）")])
    out, _ = _run([_sec(1, text)], plan_bare)
    assert not out.items[0].has_key
    plan_cue = ExamPlan(items=[_single(options=["A. 4", "B. 5"], answer="B",
                                       answer_quote="答：B")])
    out, _ = _run([_sec(1, text)], plan_cue)
    assert out.items[0].has_key and out.items[0].answer == "B"


@pytest.mark.parametrize("answer, ok", [("A", False), ("B", True)])
def test_letters_in_english_words_are_not_the_answer(answer, ok):
    text = "〔p.3〕1. Which is prime? A. 4 B. 5\nThe answer is B"
    raw = RawItem(number="1", kind="single", stem="Which is prime?", options=["A. 4", "B. 5"],
                  answer=answer, answer_quote="The answer is B")
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    assert out.items[0].has_key is ok


@pytest.mark.parametrize("quote, answer, ok", [
    ("答案：不正确", "对", False), ("答案：不正确", "错", True), ("答：不对", "错", True),
    ("答案：正确", "对", True), ("答案：×，因为 T 不是质数", "对", False),
    ("Answer: F", "错", True), ("Answer: FACT", "错", False),
    ("正确答案：错", "错", True), ("正确答案：错", "对", False), ("正确答案：对", "对", True),
    ("对", "对", False)])
def test_tf_quote_words_negations_first(quote, answer, ok):
    text = f"〔p.3〕1. 判断：2 是质数。\n{quote}"
    raw = RawItem(number="1", kind="tf", stem="2 是质数。", answer=answer, answer_quote=quote)
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    assert out.items[0].has_key is ok


def test_numeric_numbers_are_read_as_text():
    text = "〔p.3〕3. 选一个 A. x B. y\n答案：3. B"
    plan = ExamPlan.model_validate({"items": [{"number": 3, "parent": None, "kind": "single",
                                               "stem": "选一个", "options": ["A. x", "B. y"]}],
                                    "answers": [{"number": 3, "answer": "B",
                                                 "answer_quote": "3. B"}]})
    out, _ = _run([_sec(1, text)], plan)
    assert out.items[0].number == "3" and out.items[0].has_key and out.items[0].answer == "B"


def test_multi_answer_sorted_and_deduplicated():
    text = "〔p.3〕3. 哪些是偶数？ A. 2 B. 3 C. 4 答案：CA"
    raw = RawItem(number="3", kind="multi", stem="哪些是偶数？", options=["A. 2", "B. 3", "C. 4"],
                  answer="C、A", answer_quote="答案：CA")
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    assert out.items[0].answer == "AC" and out.items[0].has_key


# ---- structure

@pytest.mark.parametrize("raw, problem", [
    (_single(options=["A. 4"]), "too_few_options"),
    (_single(stem="  "), "empty_stem"),
    (RawItem(number="1", kind="tf", stem="对吗", options=["对", "错", "不知道"]), "bad_options"),
    (RawItem(number="1", kind="open", stem="简述", options=["A. x"]), "bad_options"),
    (_single(answer="E", answer_quote="答案：B"), "bad_answer"),
])
def test_structure_failures_are_incomplete(raw, problem):
    out, _ = _run([_sec(1, Q1 + " E")], ExamPlan(items=[raw]))
    (item,) = out.items
    assert item.status == "incomplete" and item.problem == problem


def test_unreadable_item_drops_any_answer():
    raw = _single(status="unreadable", problem="选项被截断", answer="B", answer_quote="答案：B")
    out, _ = _run([_sec(1, Q1)], ExamPlan(items=[raw]))
    (item,) = out.items
    assert item.status == "unreadable" and item.answer is None and not item.has_key
    assert item.problem == "other: 选项被截断"         # no reason code: other


@pytest.mark.parametrize("fields, problem", [
    ({"problem_code": "handwriting_over_print", "problem": "手写压在选项上"},
     "handwriting_over_print: 手写压在选项上"),
    ({"problem_code": "Missing_Part", "problem": "选项 C、D 被截断"}, "missing_part: 选项 C、D 被截断"),
    ({"problem_code": "smudged", "problem": "看不清"}, "other: 看不清"),          # unknown code
    ({"problem_code": "needs_figure", "problem": None}, "needs_figure"),
    ({"problem": "illegible: 乱码"}, "illegible: 乱码"),        # the code written in the sentence
    ({}, "other"),
])
def test_an_unreadable_items_problem_is_a_code_and_a_sentence(fields, problem):
    raw = RawItem.model_validate({"number": "1", "kind": "single", "stem": "下列哪个是质数？",
                                  "options": ["A. 4", "B. 5"], "status": "unreadable", **fields})
    out, _ = _run([_sec(1, Q1)], ExamPlan(items=[raw]))
    assert out.items[0].problem == problem
    assert problem_code(out.items[0].problem) == problem.split(":")[0]
    assert any(d.startswith("unreadable by reason: ") for d in out.diagnostics)


def test_problem_code_of_other_problems_is_none():
    assert problem_code("answer_not_in_text") is None and problem_code(None) is None
    assert problem_code("illegible") == "illegible"


# ---- parents

def test_parent_links_to_the_group_in_the_same_batch_else_the_only_one():
    text1 = "〔p.3〕二、阅读材料……（1）题一"
    text2 = "〔p.4〕（2）题二（3）题三" + "字" * 900
    plan1 = ExamPlan(items=[RawItem(number="二", kind="group", stem="阅读材料……"),
                            RawItem(number="(1)", parent="二、", kind="open", stem="题一")])
    plan2 = ExamPlan(items=[RawItem(number="(2)", parent="二", kind="open", stem="题二"),
                            RawItem(number="(3)", parent="五", kind="open", stem="题三")])
    out, _ = _run([_sec(1, text1), _sec(2, text2)], plan1, plan2)
    group, q1, q2, q3 = out.items
    assert [i.ord for i in out.items] == [0, 1, 2, 3]
    assert q1.parent_id == group.item_id and q2.parent_id == group.item_id
    assert q3.parent_id is None and q3.status == "incomplete" and q3.problem == "parent_missing"


def test_a_group_later_in_the_material_is_never_a_parent():
    text1 = "〔p.3〕（1）题一" + "字" * 900
    text2 = "〔p.4〕二、阅读材料……"
    plan1 = ExamPlan(items=[RawItem(number="(1)", parent="二", kind="open", stem="题一")])
    plan2 = ExamPlan(items=[RawItem(number="二", kind="group", stem="阅读材料……")])
    out, _ = _run([_sec(1, text1), _sec(2, text2)], plan1, plan2)
    q1, group = out.items
    assert group.kind == "group" and q1.parent_id is None
    assert q1.status == "incomplete" and q1.problem == "parent_missing"


@pytest.mark.parametrize("group", [
    RawItem(number="二", kind="group", stem="阅读材料……", status="unreadable", problem="乱码"),
    RawItem(number="二", kind="group", stem=""),           # incomplete: empty stem
])
def test_a_sub_question_of_a_group_that_is_not_ok_is_incomplete(group):
    text = "〔p.3〕二、阅读材料……（1）题一"
    plan = ExamPlan(items=[group, RawItem(number="(1)", parent="二", kind="open", stem="题一")])
    out, _ = _run([_sec(1, text)], plan)
    g, q1 = out.items
    assert g.status != "ok"
    assert q1.parent_id == g.item_id and q1.status == "incomplete"
    assert q1.problem == "parent_not_ok"
    assert any("parent_not_ok" in d for d in out.diagnostics)


# ---- answer pages

def test_answer_page_entries_match_by_number_and_parent():
    questions = ("〔p.3〕5. 选一个 A. x B. y\n一、材料甲（1）问甲\n二、材料乙（1）问乙\n"
                 "6. 再选 A. x B. y")
    key_page = "〔p.9〕参考答案 5. A 一（1）甲的答案 二（1）乙的答案 6. B 7. C"
    plan_q = ExamPlan(items=[
        RawItem(number="5", kind="single", stem="选一个", options=["A. x", "B. y"]),
        RawItem(number="一", kind="group", stem="材料甲"),
        RawItem(number="（1）", parent="一", kind="open", stem="问甲"),
        RawItem(number="二", kind="group", stem="材料乙"),
        RawItem(number="（1）", parent="二", kind="open", stem="问乙"),
        RawItem(number="6", kind="single", stem="再选", options=["A. x", "B. y"])])
    plan_a = ExamPlan(answers=[
        RawAnswer(number="5", answer="A", answer_quote="5. A"),
        RawAnswer(number="(1)", parent="一", answer="甲的答案", answer_quote="一（1）甲的答案"),
        RawAnswer(number="(1)", parent="二", answer="乙的答案", answer_quote="二（1）乙的答案"),
        RawAnswer(number="6", answer="B", answer_quote="编的 B"),
        RawAnswer(number="7", answer="C", answer_quote="7. C")])
    out, _ = _run([_sec(1, questions + "字" * 900), _sec(2, key_page + "字" * 900, page=9)],
                  plan_q, plan_a)
    q5, _, a1, _, b1, q6 = out.items
    assert q5.answer == "A" and q5.has_key
    assert a1.answer == "甲的答案" and a1.has_key and b1.answer == "乙的答案" and b1.has_key
    assert q6.answer is None and not q6.has_key          # its quote is not in the answer page
    assert any("answer 7" in d for d in out.diagnostics)


def test_a_top_level_number_next_to_a_same_numbered_sub_question_is_not_matched():
    questions = "〔p.3〕1. 选一个 A. x B. y\n二、材料乙（1）再选 A. x B. y"
    key_page = "〔p.9〕参考答案 （1）B"
    plan_q = ExamPlan(items=[
        RawItem(number="1", kind="single", stem="选一个", options=["A. x", "B. y"]),
        RawItem(number="二", kind="group", stem="材料乙"),
        RawItem(number="（1）", parent="二", kind="single", stem="再选",
                options=["A. x", "B. y"])])
    plan_a = ExamPlan(answers=[RawAnswer(number="（1）", answer="B", answer_quote="（1）B")])
    out, _ = _run([_sec(1, questions + "字" * 900), _sec(2, key_page + "字" * 900, page=9)],
                  plan_q, plan_a)
    assert all(not i.has_key and i.answer is None for i in out.items)
    assert any("2 questions match" in d for d in out.diagnostics)


@pytest.mark.parametrize("entry, ok", [
    (RawAnswer(number="2", answer="A", answer_quote="1. A"), False),     # quote of another number
    (RawAnswer(number="2", answer="A", answer_quote="1. A 2. B"), False),  # answer of the next one
    (RawAnswer(number="2", answer="B", answer_quote="2. B"), True),
])
def test_an_answer_page_quote_must_carry_the_entry_number_next_to_the_answer(entry, ok):
    q = "〔p.3〕2. 选一个 A. x B. y" + "字" * 900
    key = "〔p.9〕答案 1. A 2. B 3. C" + "字" * 900
    plan_q = ExamPlan(items=[RawItem(number="2", kind="single", stem="选一个",
                                     options=["A. x", "B. y"])])
    out, _ = _run([_sec(1, q), _sec(2, key, page=9)], plan_q, ExamPlan(answers=[entry]))
    assert out.items[0].has_key is ok


def test_a_repeated_number_without_parent_is_not_matched():
    text = "〔p.3〕一、材料甲（1）问甲\n二、材料乙（1）问乙\n答案：(1) 甲"
    plan = ExamPlan(items=[RawItem(number="一", kind="group", stem="材料甲"),
                           RawItem(number="(1)", parent="一", kind="open", stem="问甲"),
                           RawItem(number="二", kind="group", stem="材料乙"),
                           RawItem(number="(1)", parent="二", kind="open", stem="问乙")],
                    answers=[RawAnswer(number="(1)", answer="甲", answer_quote="(1) 甲")])
    out, _ = _run([_sec(1, text)], plan)
    assert all(not i.has_key for i in out.items)


def test_a_stray_numbered_letter_in_the_question_area_is_no_key():
    """A handwritten 「9. A」 the OCR read among the questions is not an answer page entry."""
    text = "〔p.1〕9. 下列哪个是质数？ A. 4 B. 5\n9. A\n10. 再选 A. x B. y"
    plan = ExamPlan(items=[_single(number="9", options=["A. 4", "B. 5"], page=1),
                           RawItem(number="10", kind="single", stem="再选",
                                   options=["A. x", "B. y"])],
                    answers=[RawAnswer(number="9", answer="A", answer_quote="9. A")])
    out, _ = _run([_sec(1, text, page=1)], plan)
    assert all(not i.has_key for i in out.items)
    assert any("answer_not_in_answer_section" in d for d in out.diagnostics)


def test_an_entry_in_a_printed_answer_section_is_a_key():
    text = "〔p.1〕9. 下列哪个是质数？ A. 4 B. 5\n10. 再选 A. x B. y\n参考答案\n9. B 10. A"
    plan = ExamPlan(items=[_single(number="9", options=["A. 4", "B. 5"], page=1),
                           RawItem(number="10", kind="single", stem="再选",
                                   options=["A. x", "B. y"])],
                    answers=[RawAnswer(number="9", answer="B", answer_quote="9. B"),
                             RawAnswer(number="10", answer="A", answer_quote="10. A")])
    out, _ = _run([_sec(1, text, page=1)], plan)
    assert [(i.answer, i.has_key) for i in out.items] == [("B", True), ("A", True)]


def test_an_entry_before_the_answer_heading_is_no_key():
    text = "〔p.1〕9. 下列哪个是质数？ A. 4 B. 5\n9. B\n参考答案\n10. A"
    plan = ExamPlan(items=[_single(number="9", options=["A. 4", "B. 5"], page=1)],
                    answers=[RawAnswer(number="9", answer="B", answer_quote="9. B")])
    out, _ = _run([_sec(1, text, page=1)], plan)
    assert not out.items[0].has_key


def test_an_answer_section_titled_by_its_section_or_running_on_into_the_next_batch():
    q = "〔p.3〕1. 选一个 A. x B. y\n2. 再选 A. x B. y" + "字" * 900
    plan_q = ExamPlan(items=[
        RawItem(number="1", kind="single", stem="选一个", options=["A. x", "B. y"]),
        RawItem(number="2", kind="single", stem="再选", options=["A. x", "B. y"])])
    # the section's title is the heading (every batch of it starts with the title)
    key = "〔p.9〕" + "字" * 5990 + "〔p.10〕1. B 2. A"
    plan_a = ExamPlan(answers=[RawAnswer(number="1", answer="B", answer_quote="1. B")])
    out, llm = _run([_sec(1, q), _sec(2, key, title="参考答案与解析", page=9)],
                    plan_q, ExamPlan(), plan_a)
    assert len(llm.calls) == 3                       # the key section is two batches
    assert out.items[0].has_key and out.items[0].answer == "B"
    # a heading in an earlier batch of the same section carries on into the next one
    key2 = "〔p.9〕参考答案\n" + "字" * 5990 + "〔p.10〕2. A"
    plan_b = ExamPlan(answers=[RawAnswer(number="2", answer="A", answer_quote="2. A")])
    out, llm = _run([_sec(1, q), _sec(2, key2, title="其他", page=9)], plan_q, ExamPlan(),
                    plan_b)
    assert len(llm.calls) == 3
    assert out.items[1].has_key and out.items[1].answer == "A"


@pytest.mark.parametrize("line, heading", [
    ("参考答案", True), ("## 参考答案与解析", True), ("一、选择题答案", True), ("Answer Key", True),
    ("参考答案：1. B 2. C", True), ("答案 1. A 2. B", True), ("Answers: 1. A", True),
    ("第二部分 参考答案", True),
    ("答案：B", False), ("【答案】B", False), ("本题答案 B", False), ("Answer: B", False),
    ("解析：本题考查均值", False), ("KEY POINTS", False), ("9. A", False)])
def test_answer_section_headings(line, heading):
    assert (answer_section_start(f"〔p.1〕题目\n{line}") is not None) is heading


# ---- replaying an earlier run (the exam workflow re-extracts only the changed batches)

def test_a_replay_calls_only_the_changed_batches_and_keeps_the_other_ids():
    s1 = _sec(1, Q1 + "字" * 900)
    s2 = _sec(2, "〔p.4〕2. 读不清的题" + "字" * 900, page=4)
    first, _ = _run([s1, s2], ExamPlan(items=[_single(answer="B", answer_quote="答案：B")]),
                    ExamPlan(items=[RawItem(number="2", kind="open", stem="读不清的题",
                                            status="unreadable",
                                            problem_code="handwriting_over_print")]))
    assert first.calls == 2 and len(first.plans) == 2 and len(first.origins) == 2
    prev = Previous(dict(first.plans),
                    dict(zip(first.origins, (i.item_id for i in first.items), strict=True)))
    s2b = _sec(2, "〔p.4〕2. 读清楚的题" + "字" * 900, page=4)
    llm = FakeLLM(ExamPlan(items=[RawItem(number="2", kind="open", stem="读清楚的题", page=4)]))
    again = extract_items([s1, s2b], llm=llm, new_id=lambda p: f"{p}-new", previous=prev)
    assert len(llm.calls) == 1 and again.calls == 1 and "读清楚的题" in llm.calls[0][2]
    q1, q2 = again.items
    assert q1.item_id == first.items[0].item_id and q1.has_key and q1.answer == "B"
    assert q2.item_id == "si-new" and q2.status == "ok"


def test_a_replay_does_not_retry_a_batch_that_failed():
    first, _ = _run([_sec(1, Q1 + "字" * 900)], LLMParseError("bad"))
    prev = Previous(dict(first.plans), {})
    llm = FakeLLM()
    again = extract_items([_sec(1, Q1 + "字" * 900)], llm=llm, new_id=_ids(), previous=prev)
    assert llm.calls == [] and again.failed_batches == again.batches == 1


def test_answer_page_clears_a_dropped_key_problem():
    q = "〔p.3〕1. 选一个 A. x B. y" + "字" * 900
    key = "〔p.9〕答案 1. B" + "字" * 900
    plan_q = ExamPlan(items=[RawItem(number="1", kind="single", stem="选一个",
                                     options=["A. x", "B. y"], answer="A", answer_quote="编的")])
    plan_a = ExamPlan(answers=[RawAnswer(number="1", answer="B", answer_quote="1. B")])
    out, _ = _run([_sec(1, q), _sec(2, key, page=9)], plan_q, plan_a)
    (item,) = out.items
    assert item.answer == "B" and item.has_key and item.problem is None


# ---- failures

def test_a_failing_batch_is_counted_and_the_others_continue():
    secs = [_sec(1, Q1 + "字" * 900), _sec(2, "〔p.4〕x" + "字" * 900),
            _sec(3, Q1 + "字" * 900)]
    out, llm = _run(secs, LLMParseError("bad"), LLMUnavailable("none"),
                    ExamPlan(items=[_single(answer="B", answer_quote="答案：B")]))
    assert out.failed_batches == 2 and out.batches == 3 and len(llm.calls) == 3
    assert len(out.items) == 1 and out.items[0].section_id == "s3" and out.items[0].ord == 0


def test_every_batch_failing_shows_in_the_counts():
    out, _ = _run([_sec(1, Q1 + "字" * 900)], LLMParseError("bad"))
    assert out.failed_batches == out.batches == 1 and out.items == ()


class _Cap(BudgetExceededError):
    def __init__(self):
        RuntimeError.__init__(self, "cap")


@pytest.mark.parametrize("exc", [ProviderRejectedError("insufficient balance", status_code=402),
                                 _Cap()])
def test_provider_rejection_and_budget_propagate(exc):
    with pytest.raises(type(exc)):
        _run([_sec(1, Q1 + "字" * 900), _sec(2, Q1 + "字" * 900)], exc, ExamPlan())


def test_a_replay_whose_changed_batch_fails_keeps_the_earlier_plan_of_its_pages():
    s1 = _sec(1, Q1 + "字" * 900)
    s2 = _sec(2, "〔p.4〕2. 读不清的题" + "字" * 900, page=4)
    first, _ = _run([s1, s2], ExamPlan(items=[_single()]),
                    ExamPlan(items=[RawItem(number="2", kind="open", stem="读不清的题",
                                            status="unreadable", problem_code="illegible")]))
    prev = Previous(dict(first.plans),
                    dict(zip(first.origins, (i.item_id for i in first.items), strict=True)),
                    dict(zip(first.slots, (k for k, _ in first.plans), strict=True)))
    s2b = _sec(2, "〔p.4〕2. 读清楚的题" + "字" * 900, page=4)
    again = extract_items([s1, s2b], llm=FakeLLM(LLMParseError("bad")), new_id=_ids(),
                          previous=prev)
    assert again.failed_batches == 0
    assert [i.item_id for i in again.items] == [i.item_id for i in first.items]
    assert again.items[1].status == "unreadable"
    assert any("earlier extraction of these pages is kept" in d for d in again.diagnostics)


# ---- sources-exam@v3: the stem and options are checked against the batch's text

def test_a_stem_and_options_match_across_spacing_line_breaks_full_width_and_math_delimiters():
    text = ("〔p.2〕前面的说明\n〔p.3〕1．已知向量  $ \\vec{a}=(2,-3) $ ，\n则 $ |a| = $ （ ）\n"
            "（A）$ \\sqrt{13} $   （B）5")
    raw = _single(stem="已知向量 \\( \\vec{a}=(2,-3) \\)，则 \\( |a| = \\)（ ）",
                  options=["A. \\( \\sqrt{13} \\)", "B. 5"])
    raw.page = 2                                    # the model's page; the text says 3
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    (item,) = out.items
    assert (item.status, item.problem, item.page) == ("ok", None, 3)


def test_a_stem_may_skip_handwriting_mixed_into_the_print_but_not_more():
    plan = ExamPlan(items=[_single(stem="下列哪个是质数？", options=["A. 4", "B. 5"])])
    text = "〔p.3〕1. 下列哪个B是质数？ A. 4 B. 5"     # a handwritten B in the stem
    ok, _ = _run([_sec(1, text)], plan)
    assert ok.items[0].status == "ok"
    far = "〔p.3〕1. 下列" + "涂改" * 20 + "哪个是质数？ A. 4 B. 5"
    out, _ = _run([_sec(1, far)], plan)
    assert out.items[0].problem == "stem_not_in_text"


def test_an_option_not_in_the_text_makes_the_item_incomplete():
    text = "〔p.3〕1. 下列哪个是质数？ A. 4 B. 5"
    raw = _single(options=["A. 4", "B. 7"])
    out, _ = _run([_sec(1, text)], ExamPlan(items=[raw]))
    assert (out.items[0].status, out.items[0].problem) == ("incomplete", "option_not_in_text")


def test_prompt_v3_version():
    from evowiki.wikiapp.llm import JSON_PROMPT_VERSION
    assert JSON_PROMPT_VERSION["sources.exam"] == "sources-exam@v3"
