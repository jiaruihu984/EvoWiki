"""teach.textsim.near_duplicate: a rule and its negation are never duplicates."""

from evowiki.teach.textsim import near_duplicate, negations

RULE = "讲新概念时先给一个能算出结果的小例子，再给定义和公式，最后总结要点"


def test_near_copies_are_duplicates():
    assert near_duplicate(RULE, RULE + "。")
    assert near_duplicate(RULE, RULE.replace("小例子", "小例题"))


def test_a_negated_rule_is_not_a_duplicate():
    for neg in ("不", "没", "别", "勿", "非", "无"):
        other = RULE.replace("先给", f"{neg}先给")
        assert not near_duplicate(RULE, other), neg
    en = "Always give a small worked example before the definition of each new concept"
    for word in ("not", "never", "don't", "no"):
        assert not near_duplicate(en, en.replace("Always give", f"Always {word} give")), word
    assert not near_duplicate(en, en.replace("Always give", "Always don’t give"))   # curly quote


def test_common_words_that_contain_a_negation_character_do_not_count():
    assert negations("这一点非常重要，无论哪一页都要先讲") == frozenset()
    assert near_duplicate(RULE, RULE.replace("最后总结要点", "最后非常简短地总结要点"))
    assert negations("不要用术语，don't skip steps") == {"不", "don't"}
