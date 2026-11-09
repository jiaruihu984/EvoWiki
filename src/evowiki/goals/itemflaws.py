"""Item-writing flaws of a single-answer check question (spec 2026-10-11-llm-wheels-reuse §三,
information-flow rules ITEM_FLAWS / FOLLOWUP_NEW). Pure code, no LLM.

`ITEM_RULES_ZH` is the prompt fragment every check / confirm producer carries: item-writing
guidelines in the Haladyna tradition and the usual item-writing-flaw list, paraphrased in Chinese,
plus the distractor procedure (solve it first, then give each wrong option the wrong idea behind
it). `item_flaws` catches the flaws code can see (overlapping options, the key clearly longest,
all / none of the above, absolute words in options, a negative stem); `near_duplicate` says when a
new item only re-tests an old one. Each returns short Chinese reasons; () = clean. A producer that
gets reasons asks once more with them, then fails closed."""

import re
from collections.abc import Sequence

from evowiki.goals.contracts import CheckItem
from evowiki.teach.textsim import bigram_jaccard

# 2026-10-28 (outline rework): 14 rules -> 7; rule 2 follows the cell's aspect (it replaced
# 「考理解和运用：给一个具体情境……不要只考认词或背定义」, which fought term / definition cells).
# The flaws code can see are still checked below (item_flaws), whatever the prompt says.
ITEM_RULES_ZH = """出题规则（单选确认题）：
1. 一道题只考一个知识点。题干本身是一个完整、清楚的正面问题（不用「哪个不是 / 除了……」这类否定问法），解题需要的条件都写在题干里。
2. 按格子的层面出题：术语 / 定义格子可以考它是什么意思、和相近的概念怎么区分；计算过程格子给一个具体情境让人算出结果；用途格子问什么时候用、为什么用它；解读格子给一个结果让人解读；原理格子问为什么会这样。
3. 只有一个明确正确的选项，懂的人不会有争议；不设陷阱，不靠文字游戏让人选错。
4. 选项属于同一类东西，长短和写法相近；正确选项不要明显比其他选项长、更具体，题干不要重复它的关键词。
5. 选项之间不重复、不互相包含，范围不重叠；不用「以上都对 / 以上都不对」，不用「总是、永远、一定、所有」这类绝对词。
6. 错误选项要可信：先自己把题解一遍，确认正确答案，再为每个错误选项写出它对应的错误想法（学习者为什么会选它），想不出就换掉。
7. 题目和正文一致，只用学习者在这一页里能学到的东西；选项顺序系统会打乱，只要给对答案序号。"""

_STRIP = re.compile(r"[\s，。、；：！？,;:!?\"'“”‘’「」『』《》…·]+|(?<!\d)\.|\.(?!\d)")
_NUMERIC = re.compile(r"[\d.,+\-−/%]+")
_ALL_NONE_ZH = ("以上都对", "以上都不对", "以上皆是", "以上皆非", "都对", "都不对")
_ALL_NONE_EN = re.compile(r"\b(?:all|none)\s+of\s+the\s+above\b", re.IGNORECASE)
# 所有 / 全部 / 完全 are not here: 「所有最短路径」「全部节点」 are ordinary technical words, and a refused
# question also refuses what hangs on it (「这个我早会了」); the prompt still asks not to lean on them
_ABSOLUTE_ZH = ("总是", "永远", "绝不", "从不", "一定")
# ordinary forms that contain a listed word but are not absolute / not a negative stem
_ABSOLUTE_ZH_OK = ("一定程度", "不一定")
_ABSOLUTE_EN = re.compile(r"\b(?:always|never|all|none)\b", re.IGNORECASE)
_NEGATIVE_ZH = ("不是", "不正确", "不属于", "不包括", "错误的是", "不对的是", "不能", "除了")
_NEGATIVE_ZH_OK = ("为什么不能", "为什么不", "是不是", "能不能", "会不会")   # longest first
# English: NOT / EXCEPT in capitals (the item-writing convention), or a lowercase "not" in a
# which / what question ("Which of these is not ..."); a plain "does not" in a condition is fine
_NEGATIVE_EN = re.compile(r"\b(?:NOT|EXCEPT)\b|\b[Ww](?:hich|hat)\b[^?.!]*?\bnot\b")
LONGEST_RATIO = 1.5
LONGEST_GAP = 8
STEM_JACCARD = 0.7


def norm(text: str) -> str:
    """Comparison form: case folded, whitespace and sentence punctuation removed (math symbols,
    signs, brackets and decimal points stay: "-1" and "1" are different options)."""
    return _STRIP.sub("", text).casefold()


def _numeric(text: str) -> bool:
    return bool(_NUMERIC.fullmatch(text))


def _overlap(options: tuple[str, ...]) -> str | None:
    ns = [norm(o) for o in options]
    for i, a in enumerate(ns):
        for j, b in enumerate(ns):
            if i >= j:
                continue
            if a == b:
                return f"选项「{options[i]}」和「{options[j]}」重复"
            short, long_, si, li = (a, b, i, j) if len(a) <= len(b) else (b, a, j, i)
            if len(short) >= 2 and not _numeric(short) and short in long_:
                return f"选项「{options[si]}」包含在「{options[li]}」里，选项重叠"
    return None


def _key_longest(item: CheckItem) -> str | None:
    lens = [len(" ".join(o.split())) for o in item.options]
    key = lens[item.answer]
    other = max(n for j, n in enumerate(lens) if j != item.answer)
    if key >= LONGEST_RATIO * other and key - other >= LONGEST_GAP:
        return "正确选项明显比其他选项长，长度就透露了答案"
    return None


def _all_none(options: tuple[str, ...]) -> str | None:
    for o in options:
        if any(w in o for w in _ALL_NONE_ZH) or _ALL_NONE_EN.search(o):
            return f"选项「{o}」是「以上都对 / 以上都不对」一类"
    return None


def _without(text: str, phrases: tuple[str, ...]) -> str:
    for p in phrases:
        text = text.replace(p, "|")   # a gap, so the remains do not join into a word
    return text


def _absolute(options: tuple[str, ...]) -> str | None:
    for o in options:
        plain = _without(o, _ABSOLUTE_ZH_OK)
        hit = next((w for w in _ABSOLUTE_ZH if w in plain), None)
        m = _ABSOLUTE_EN.search(o)
        if hit or m:
            return f"选项「{o}」用了绝对词「{hit or m.group(0)}」"
    return None


def _negative(stem: str) -> str | None:
    plain = _without(stem, _NEGATIVE_ZH_OK)
    hit = next((w for w in _NEGATIVE_ZH if w in plain), None)
    m = _NEGATIVE_EN.search(stem)
    if hit or m:
        return f"题干是否定问法（「{hit or m.group(0)}」）"
    return None


def item_flaws(item: CheckItem) -> tuple[str, ...]:
    """The item-writing flaws code can see; () = clean."""
    found = (_overlap(item.options), _key_longest(item), _all_none(item.options),
             _absolute(item.options), _negative(item.stem))
    return tuple(r for r in found if r is not None)


REPEATED_OPTIONS = "选项重复"


def _repeats(options: Sequence[str]) -> bool:
    """CheckItem's own repeat test (whitespace collapsed, case folded)."""
    return len({" ".join(o.split()).casefold() for o in options}) != len(options)


def judge_check(cell: str, stem: str, options: Sequence[str], answer: int, *,
                verified: bool = False) -> tuple[CheckItem | None, tuple[str, ...]]:
    """Build a producer's check item and find its flaws, in one place for every producer.

    (item, item_flaws(item)) when the item builds; (None, (REPEATED_OPTIONS,)) when it does not
    build because two options repeat (CheckItem refuses that, `check_outline` does not: it is an
    item flaw, dropped or retried like the others); (None, ()) for any other malformed check,
    which is not judged here (`check_outline` / the producer's own format check refuses it)."""
    try:
        item = CheckItem(cell=cell, stem=stem.strip(), options=tuple(options), answer=answer,
                         verified=verified)
    except ValueError:
        return None, ((REPEATED_OPTIONS,) if _repeats(options) else ())
    return item, item_flaws(item)


def stem_similarity(a: str, b: str) -> float:
    """Character-bigram Jaccard of the normalised stems (1.0 for equal one-character stems)."""
    na, nb = norm(a), norm(b)
    return 1.0 if na == nb else bigram_jaccard(na, nb)


def near_duplicate(new: CheckItem, old: CheckItem, *, rekey: bool = False) -> tuple[str, ...]:
    """Why `new` only re-tests `old`; () = a new question.

    `rekey`: `new` may correct `old`'s key (a check rewrite after 「答案不对」). Then an old
    distractor may become the new key. Keeping the stem and the options and only moving the key
    there is a correction, not a re-test (nothing refused); with other options, the new
    distractors must not reuse old ones, and the stem may stay. A follow-up question uses the
    strict default."""
    out: list[str] = []
    distractors = {norm(o) for j, o in enumerate(old.options) if j != old.answer}
    rekeyed = rekey and norm(new.options[new.answer]) in distractors
    if not rekeyed and stem_similarity(new.stem, old.stem) >= STEM_JACCARD:
        out.append("题干和原题几乎一样")
    same_set = {norm(o) for o in new.options} == {norm(o) for o in old.options}
    if rekeyed and same_set:
        return ()                      # only the key was corrected: a fix, not a re-test
    if same_set:
        out.append("选项和原题一样（只是换了顺序也不行）")
    # a number is not a reused idea: a new counting question has the same few small numbers to offer
    reused = [o for j, o in enumerate(new.options)
              if norm(o) in distractors and not _numeric(o) and not (rekey and j == new.answer)]
    if reused:
        out.append("沿用了原题的错误选项：" + "、".join(f"「{o}」" for o in reused))
    return tuple(out)


def flaws_prompt(flaws: tuple[str, ...]) -> str:
    """The retry prompt's addition: the previous item's flaws; "" when there are none."""
    if not flaws:
        return ""
    return "\n\n上一版的题有这些问题，这次必须改掉：\n- " + "\n- ".join(flaws)
