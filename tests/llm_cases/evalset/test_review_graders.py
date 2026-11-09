from tests.llm_cases.evalset import graders


def rec(m_before, m_after, correct=False, in_review=True) -> dict:
    return {"turns": [], "answers": [{"page": "P", "cell": "c8.notation", "asked_correct": correct,
                                      "m_before": m_before, "m_after": m_after, "in_review": in_review,
                                      "review_cards": 1}]}


def test_a_wrong_answer_that_raises_mastery_fails():
    # pages.md 待审核 1, the real numbers: m 0.30 -> 0.3125 after a wrong answer
    assert not graders.answer_k(rec(0.30, 0.3125), change="not_up").ok
    assert graders.answer_k(rec(0.30, 0.20), change="not_up").ok
    assert graders.answer_k(rec(0.30, 0.30), change="not_up").ok


def test_a_right_answer_must_raise_it_and_an_unreadable_cell_fails():
    assert graders.answer_k(rec(0.30, 0.53, correct=True), change="up").ok
    assert not graders.answer_k(rec(0.30, 0.30, correct=True), change="up").ok
    assert not graders.answer_k(rec(None, None), change="not_up").ok


def test_page_question_missing_from_review_fails():
    assert not graders.page_question_in_review(rec(0.3, 0.3, in_review=False)).ok
    assert graders.page_question_in_review(rec(0.3, 0.3)).ok


def test_self_contradicting_question_in_tool_arguments_fails():
    bad = {"turns": [{"answer": "好", "tools": [], "proposals": []}],
           "_": 0}
    bad["turns"][0]["tools"] = [{"name": "make_cards", "args": {"cards": [{
        "cue": "节点 8 没有任何边（注意：节点 8 不在这个网络的节点集合里）"}]}, "result": ""}]
    assert not graders.turn_text_consistent(bad).ok
    bad["turns"][0]["tools"][0]["args"] = {"cards": [{"cue": "没有边的节点还算节点吗？"}]}
    assert graders.turn_text_consistent(bad).ok
