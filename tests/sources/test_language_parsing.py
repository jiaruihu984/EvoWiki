import pytest

from evowiki.harness.understand_format import classify
from evowiki.sources.exam import norm_number
from evowiki.sources.toc import _heading_number
from evowiki.sources.use import QUESTION_LINE, guess_text_use


@pytest.mark.parametrize(("text", "number"), [
    ("Chapter 3 Linear algebra", (3,)), ("Part IV: Probability", (4,)),
    ("chapter 2 矩阵", (2,)), ("第3章 Matrices", (3,)),
])
def test_mixed_chapter_headings(text, number):
    assert _heading_number(text) == number


@pytest.mark.parametrize(("text", "use"), [
    ("Help me understand this paper", "understand"),
    ("Use this book as a reference", "reference"),
    ("Learn from this textbook", "follow"), ("按这本教材学", "follow"),
])
def test_english_and_chinese_material_intent(text, use):
    assert guess_text_use(text) == use


def test_question_labels_and_legacy_format_rules():
    assert QUESTION_LINE.search("Question 12: Find x")
    assert norm_number("Question 12.") == "12"
    assert norm_number("第12题") == "12"
    assert classify("Create one page per section")[0] == "clear"
    assert classify("Use a read array")[0] == "clear"
