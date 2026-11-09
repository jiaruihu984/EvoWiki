"""sources/use.py: the use of a material when the client does not say — from the goal text
(guess_text_use) or from the material itself (guess_material_use: exam first, then paper, else
learn). Pure functions, one test per branch."""

from datetime import UTC, datetime

from evowiki.sources.models import SectionRow, SourceRow
from evowiki.sources.use import (
    QUESTION_LINES_MIN,
    guess_material_use,
    guess_text_use,
)

T = datetime(2026, 10, 21, tzinfo=UTC)


def src(title="讲义", pages: int | None = 10) -> SourceRow:
    return SourceRow(source_id="s1", user_id="u1", kind="pdf", title=title, sha256="x", bytes=1,
                     pages=pages, status="ready", error=None, unreadable_pages=(),
                     toc_mode="outline", progress={}, origin=None, created_at=T, updated_at=T,
                     deleted_at=None)


def secs(*titles: str, text: str = "正文。") -> list[SectionRow]:
    return [SectionRow(section_id=f"x{i}", source_id="s1", parent_id=None, ord=i, depth=0,
                       title=t, page_from=None, page_to=None, text=text, tokens=1, summary=None,
                       read_by="text") for i, t in enumerate(titles)]


PAPER = ("摘要", "1 引言", "2 方法", "3 实验", "参考文献")
BOOK = ("前言", "第1章 向量", "第2章 矩阵", "习题", "索引")


# --- guess_text_use ---------------------------------------------------------------------------

def test_text_asking_to_understand_this_material():
    assert guess_text_use("帮我读懂这篇") == "understand"
    assert guess_text_use("我想看懂它") == "understand"
    assert guess_text_use("看懂一下这份资料") == "understand"


def test_text_stating_a_reference_or_learning_by_it():
    """Spec 2026-10-28 §2.5: 「参考」 and 「按……学」 are stated intents too."""
    assert guess_text_use("学机器学习，这本书作为参考") == "reference"
    assert guess_text_use("参考这本学线性代数") == "reference"
    assert guess_text_use("按这本教材学机器学习") == "follow"
    assert guess_text_use("按这份学线性代数") == "follow"
    assert guess_text_use("跟着《线性代数》学") == "follow"
    assert guess_text_use("帮我读懂这篇，参考一下") == "understand"   # understand first


def test_text_that_does_not_ask_is_undecided():
    assert guess_text_use("学机器学习") is None
    assert guess_text_use("整理一下参考文献") is None          # 参考文献 is no reference intent
    assert guess_text_use("读懂。这篇") is None              # a sentence break between
    assert guess_text_use("读懂线性代数里面的这篇") is None    # more than 6 characters between


def test_a_blank_goal_is_undecided():
    assert guess_text_use("") is None and guess_text_use("  \n") is None


# --- guess_material_use -----------------------------------------------------------------------

def test_exam_word_in_the_title():
    assert guess_material_use(src("2024 期末试卷"), secs(*PAPER)) == "exam"
    assert guess_material_use(src("Final Exams"), secs(*BOOK)) == "exam"


def test_exam_word_in_a_section_title_wins_over_paper():
    assert guess_material_use(src(), secs(*PAPER, "附录 模拟题")) == "exam"


def test_examples_is_no_exam():
    assert guess_material_use(src("Worked Examples"), secs(*BOOK)) == "learn"


def _lines(questions: int, prose: int) -> str:
    qs = [f"{i + 1}. 下列说法正确的是（ ）" for i in range(questions)]
    return "\n".join([*qs, *(["这是一段讲解。"] * prose), ""])


def test_mostly_numbered_questions_is_an_exam():
    n = QUESTION_LINES_MIN
    assert guess_material_use(src(), secs("一", text=_lines(n, 4 * n))) == "exam"   # 20 %
    options = "\n".join(["（1）求极限。", "A．1", "B、2", "(2) 填空 ( )"] * 4)
    assert guess_material_use(src(), secs("一", text=options)) == "exam"


def test_too_few_question_lines_is_no_exam():
    n = QUESTION_LINES_MIN
    assert guess_material_use(src(), secs("一", text=_lines(n - 1, 0))) == "learn"


def test_too_small_a_share_of_question_lines_is_no_exam():
    n = QUESTION_LINES_MIN
    assert guess_material_use(src(), secs("一", text=_lines(n, 4 * n + 1))) == "learn"


def test_numbered_headings_inside_the_text_are_no_questions():
    text = "\n".join(f"{i}.1 小节" for i in range(1, 40))
    assert guess_material_use(src(), secs("一", text=text)) == "learn"


def test_paper_like_material_understands():
    assert guess_material_use(src(pages=12), secs(*PAPER)) == "understand"
    assert guess_material_use(src(pages=None), secs(*PAPER)) == "understand"   # TOC alone
    english = secs("ABSTRACT", "1 Introduction", "Bibliography")
    assert guess_material_use(src("Paper", pages=20), english) == "understand"


def test_a_long_paper_like_material_learns():
    assert guess_material_use(src(pages=61), secs(*PAPER)) == "learn"


def test_headings_not_at_the_start_of_a_title_learn():
    assert guess_material_use(src(), secs("关于摘要", "1 引言", "参考文献")) == "learn"


def test_without_references_learns():
    assert guess_material_use(src(), secs(*PAPER[:-1])) == "learn"


def test_a_textbook_learns():
    assert guess_material_use(src(pages=300), secs(*BOOK)) == "learn"
