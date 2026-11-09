"""goals.itemflaws: code checks for item-writing flaws and near-duplicate follow-ups (pure)."""

import pytest

from evowiki.goals.contracts import CheckItem
from evowiki.goals.itemflaws import ITEM_RULES_ZH, item_flaws, near_duplicate, stem_similarity

CELL = "z.interpret"
CLEAN = CheckItem(cell=CELL, stem="z = 2 表示这个值？",
                  options=("高于均值 2 个标准差", "高于均值 2 分", "是 2%"), answer=0)


def item(stem="z = 2 表示这个值？", options=("高于均值 2 个标准差", "高于均值 2 分", "是 2%"),
         answer=0):
    return CheckItem(cell=CELL, stem=stem, options=tuple(options), answer=answer)


def test_rules_fragment_has_the_guidelines_and_the_distractor_procedure():
    assert "先自己把题解一遍" in ITEM_RULES_ZH
    assert "为每个错误选项写出它对应的错误想法（学习者为什么会选它）" in ITEM_RULES_ZH
    for s in ("只考一个知识点", "否定问法", "以上都对", "绝对词", "重叠", "明显比其他选项长"):
        assert s in ITEM_RULES_ZH, s
    assert len(ITEM_RULES_ZH.splitlines()) == 8          # 2026-10-28: 14 rules -> 7
    assert "按格子的层面出题" in ITEM_RULES_ZH


def test_clean_item_has_no_flaws():
    assert item_flaws(CLEAN) == ()
    assert item_flaws(item(stem="(x − μ) / σ 算的是？",
                           options=("(x − μ) / σ", "(x − σ) / μ", "x / σ"))) == ()


@pytest.mark.parametrize("options, word", [
    (("均值", "均值的两倍", "方差"), "包含在"),                      # containment
    (("Mean", "mean.", "方差"), "重复"),                            # same after normalising
    (("1", "12", "-1"), None),                                     # numeric: no containment
])
def test_overlapping_options(options, word):
    flaws = item_flaws(item(options=options))
    if word is None:
        assert flaws == ()
    else:
        assert any(word in f for f in flaws), flaws


def test_key_clearly_longest():
    long_key = item(options=("它比均值高出整整两个标准差的距离单位", "高 2 分", "是 2%"))
    assert any("明显比其他选项长" in f for f in item_flaws(long_key))
    # 1.5x but the gap is under 8 characters: fine
    assert item_flaws(item(options=("高两个标准差", "高 2 分", "是 2%"))) == ()


@pytest.mark.parametrize("opt", ["以上都对", "以上都不对", "以上皆是", "以上皆非", "两个都对",
                                 "All of the above", "none of the above"])
def test_all_or_none_of_the_above(opt):
    assert any("以上都对" in f for f in item_flaws(item(options=("高一点", "低一点", opt))))


@pytest.mark.parametrize("opt, word", [("总是高于均值", "总是"), ("一定是正数", "一定"),
                                       ("It is always positive", "always"),
                                       ("NEVER negative", "NEVER")])
def test_absolute_words_in_options(opt, word):
    assert any(f"绝对词「{word}」" in f for f in item_flaws(item(options=("高一点", "低一点", opt))))


def test_absolute_english_words_need_a_word_boundary_and_stems_are_not_checked():
    assert item_flaws(item(options=("small", "tall", "ball"))) == ()
    assert item_flaws(item(stem="所有人的 z 分数之和是？", options=("0", "1", "n"))) == ()


@pytest.mark.parametrize("stem", ["下面哪个不是 z 分数的用途？", "哪项不正确？", "除了均值还要什么？",
                                  "Which is NOT a z-score use?", "All EXCEPT which?",
                                  "Which of these is not a z-score use?",
                                  "What does a z-score not tell you?"])
def test_negative_stem(stem):
    assert any("否定问法" in f for f in item_flaws(item(stem=stem)))


@pytest.mark.parametrize("stem", ["z = 2 是不是高于均值？", "能不能用 z 比较两次考试？", "z 会不会是负数？",
                                  "为什么不能直接比较原始分？", "为什么不直接比较原始分？",
                                  "If the mean does not change, what is z?",
                                  "Everything except the mean stays; what is z?"])
def test_ordinary_question_forms_are_not_negative_stems(stem):
    assert item_flaws(item(stem=stem)) == ()


@pytest.mark.parametrize("opt", ["一定程度上高于均值", "不一定高于均值"])
def test_ordinary_forms_are_not_absolute_words(opt):
    assert item_flaws(item(options=("高一点", "低一点", opt))) == ()
    assert item_flaws(item(options=("高一点", "低一点", opt + "，而且一定是正数")))   # still caught


def test_near_duplicate_stem_option_set_and_reused_distractor():
    assert near_duplicate(item(stem="z = 3 表示什么？", options=("低 3 分", "高 3 个标准差", "无意义"),
                               answer=1), CLEAN) == ()
    same_stem = near_duplicate(item(options=("x", "y", "z")), CLEAN)
    assert same_stem == ("题干和原题几乎一样",)
    assert stem_similarity("z = 2 表示这个值？", "z=2 表示这个值") == 1.0
    reshuffled = near_duplicate(item(stem="完全不同的问题在这里", options=("是 2%", "高于均值 2 分",
                                                                    "高于均值 2 个标准差"),
                                     answer=2), CLEAN)
    assert "选项和原题一样（只是换了顺序也不行）" in reshuffled
    reused = near_duplicate(item(stem="完全不同的问题在这里", options=("对", "高于均值 2 分", "错")),
                            CLEAN)
    assert reused == ("沿用了原题的错误选项：「高于均值 2 分」",)
    # live 2026-11-04: "how many components" twice refused for offering 2 and 4 again
    counting = item(stem="这个网络有几个连通分量？", options=("3", "1", "2", "4"), answer=0)
    assert near_duplicate(item(stem="另一个网络里最大的分量有几个节点？", options=("5", "2", "4"), answer=0),
                          counting) == ()
    # the old KEY reused is not a reused distractor
    assert near_duplicate(item(stem="完全不同的问题在这里",
                               options=("高于均值 2 个标准差", "对", "错")), CLEAN) == ()


def test_rekey_lets_an_old_distractor_become_the_key():
    """A check rewrite after 「答案不对」: the corrected key may be an old distractor, also with the
    same stem and option set; the new distractors still must not reuse old ones."""
    fixed = item(answer=1)                              # same stem + options, key moved
    assert near_duplicate(fixed, CLEAN) != ()             # strict (follow-up): a re-test
    assert near_duplicate(fixed, CLEAN, rekey=True) == ()
    new_stem = item(stem="完全不同的问题在这里", options=("高于均值 2 分", "对", "错"))
    assert near_duplicate(new_stem, CLEAN, rekey=True) == ()
    reuse = item(stem="完全不同的问题在这里", options=("高于均值 2 分", "是 2%", "错"))
    assert near_duplicate(reuse, CLEAN, rekey=True) == ("沿用了原题的错误选项：「是 2%」",)
    # a new key that is not an old distractor: the stem check stays strict
    same_stem = item(options=("新的正确说法", "x", "y"))
    assert near_duplicate(same_stem, CLEAN, rekey=True) == ("题干和原题几乎一样",)
