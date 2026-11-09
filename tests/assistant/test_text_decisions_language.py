from evowiki.assistant.contracts import MessageIn, Selection
from evowiki.assistant.loop import quotes_wrong_answer
from evowiki.assistant.tools.propose import _SHORTER
from evowiki.assistant.turn_skills import _ABOUT_WAITING
from evowiki.goals.path import _topic_of
from evowiki.goals.regions import GOAL_ASK
from evowiki.harness.evidence import _REFUSAL
from evowiki.sources.chat_cite import _BARE_PAGE
from evowiki.sources.printed import _LONE_NUMBER


def test_wrong_answer_context_is_a_protocol_field_not_localized_text():
    assert quotes_wrong_answer(MessageIn(text="Explain", selection=Selection(
        kind="wrong_answer", text="An English question and its answer")))
    assert not quotes_wrong_answer(MessageIn(text="Explain", selection=Selection(
        kind="text", text="题目：原文\n我选的：A\n正确答案：B")))


def test_bilingual_natural_text_recognizers():
    for text in ("Make it shorter", "请缩短", "A concise explanation"):
        assert _SHORTER.search(text)
    assert _ABOUT_WAITING.search("my background preferences")
    assert _REFUSAL.search("I cannot read this file")
    assert _REFUSAL.search("找不到")
    assert _topic_of("I want to learn matrices") == "matrices"
    assert _topic_of("我要学习矩阵") == "矩阵"
    assert GOAL_ASK.sub("", "Learn probability") == "probability"
    assert GOAL_ASK.sub("", "我想学概率") == "概率"


def test_source_grammar_reads_both_languages():
    assert _LONE_NUMBER.fullmatch("Page 12").group(1) == "12"
    assert _LONE_NUMBER.fullmatch("第12页").group(1) == "12"
    assert _BARE_PAGE.search("the paper page 12").group("page") == "12"
    assert _BARE_PAGE.search("资料第12页").group("page") == "12"
