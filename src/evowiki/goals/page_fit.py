"""Does a page written for one goal fit another goal that has the same knowledge point (user rulings
2026-11-04: a point is an abstract concept, a page explains it for one goal; sharing the point
shares neither the page nor what is asked of the learner)?

ONE call (purpose `goals.page_fit`, page-fit@v3, step 「已有页面能否复用」) reads the point, this goal
and the page's whole text and fills structured fields; code derives the verdict from them:
- `full`: every check holds (the page is not written around another object, by the model's own check
  with its evidence), everything is left, no difference to state. Opened as it is, no write;
- `partial`: most of it is left and the model states the differences, each with what holds here. The
  page is opened as it is, `note` beside it; the page itself is never changed;
- `none`: the page's core answer belongs to the other object, little is left, or it can state no
  difference although the page does not fit as it is. This goal gets its own page.

Whether a page fits is the model's judgement, on its evidence; code matches no words in the page (user
ruling 2026-11-04). An answer that contradicts itself is asked again once with the problem, then
`none`: an own page is always correct. A call error propagates. Pure: the LLM is injected; goals/
never imports the LLM layer."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evowiki.goals.intake import _fence
from evowiki.goals.language import Language, language_rule
from evowiki.goals.observe import JsonLLM, refused

PURPOSE = "goals.page_fit"
STEP = "page_fit"
FIT_VERSION = "page-fit@v3"
Verdict = Literal["full", "partial", "none"]


class PageFit(BaseModel):
    """One judgement, kept per point, page version and goal context (the key is `fresh`'s): opening
    the point again reads this, no call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    map_id: str
    node_id: str
    page_id: str
    page_version: str               # goals never hash a page: wikiapp.page_version.page_text_fingerprint
    context: str                    # `fit_context`
    verdict: Verdict
    why: str = ""
    note: str = ""                  # partial: what this goal adds; "" otherwise
    written_for: str = ""           # the goal the page was written for

    def fresh(self, page_version: str, context: str) -> bool:
        return self.page_version == page_version and self.context == context


def fit_context(goal: str, point: str, scope: str, limits: Sequence[str]) -> str:
    """What a judgement depends on besides the page: a change here means judging again (the way of
    judging too)."""
    text = "\n".join((FIT_VERSION, goal, point, scope, *limits))
    return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()


class _Check(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    fits: bool = False
    evidence: str = Field(default="", description="不成立时：从页面原文摘一句；成立时：一句话说为什么")


class _Difference(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    page_says: str = ""
    here: str = Field(default="", description="这个目标里的具体说法")


class _FitOut(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    core_question: str = ""
    same_answer: bool = False
    definition: _Check = _Check()
    scope: _Check = _Check()
    examples: _Check = _Check()
    steps: _Check = _Check()
    own_object: _Check = _Check()
    remaining: Literal["all", "most", "little"] = "little"
    differences: list[_Difference] = []
    why: str = Field(default="", description="一句话，为什么")
    note: str = ""


FIT_PROMPT = """学习者在学「{goal}」，现在要学其中的知识点「{point}」。{about}
Wiki 里已有一页讲这个知识点，是为「{written_for}」写的：
{page}

判断这一页能不能给这个目标用。只看内容是否相容，不看字面相似：
⟦g:page_fit⟧- core_question：这一页要回答的那个核心问题，一句话。
- definition：页面讲的这个知识点，和这个目标要的是不是同一件事。
- scope：讲解范围够不够这个目标用，有没有大段内容在这个目标里用不上。
- examples：例子和说法放到这个目标里是否成立。
- steps：具体操作、数值和判断标准放到这个目标里是否成立。
- own_object：学习者在学的是「{goal}」。页面的页头、开头或例子如果在讲另一个具体对象（比如另一种动物、另一款产品），读的人会以为这页在讲那个对象：fits 为 false，evidence 摘那句原文。只是提到一个知识点或一门学问的名字，不算：fits 为 true。
- same_answer：只看 core_question 这一句，不看页面里顺带讲的通用内容。它问的是做法或原理，换个对象思路还能用：true；它问的就是那个对象自己的东西（它的数值、它特有的结构），换个对象就是另一个问题：false。
- remaining：把只对那个对象成立的内容（它的数值、做法、例子）都去掉，还剩多少对这个目标有用：all 全部；most 主要内容还在；little 只剩零散的常识或框架。
- differences：这个目标和页面不同的地方，每条写页面的说法和这个目标里的具体说法，按你知道的写。说不出具体说法就不写这条；「以资料为准」「自己查一下」不算。
- note：有 differences 时，学习者读完这一页接着读的一小段话，把这些差异讲清楚，不重复页面讲过的，用普通文字，不用 Markdown。

例子（只演示怎么判）：
- 《这款卡车的标准胎压是多少》给「轿车」：答案就是这款卡车的数值，same_answer 是 false，remaining 是 little。
- 《你在给卡车选轮胎》给「轿车」：选法相同，只有规格不同，same_answer 是 true，own_object 的 fits 是 false，remaining 是 most，differences 写轿车的规格。
- 《什么是摩擦系数》给「轿车」：不提任何车，same_answer 是 true，own_object 的 fits 是 true，remaining 是 all，differences 为空。⟦/g⟧
{retry}只回复一个 JSON 对象，字段按这个顺序：core_question、definition、scope、examples、steps、own_object（这五项各有 fits、evidence）、differences（各有 page_says、here）、same_answer、remaining、why、note。先写前面的，再根据它们定 same_answer 和 remaining。
{language_rule}"""
RETRY = "你上一次的回答没法使用：{problem}\n"
FAILED_CHECK = "{names} 不成立，却没有写出和这个目标不同的地方。写出具体差异；说不出来就把 remaining 写成 little。"


@dataclass(frozen=True)
class PageText:
    """What the page says, as it is judged: its header line and its sections."""

    title: str
    profile: str
    sections: tuple[tuple[str, str], ...]   # heading, body

    def rendered(self) -> str:
        head = [f"标题：{self.title}", *([f"页头：{self.profile}"] if self.profile else [])]
        return "\n\n".join(["\n".join(head), *(f"## {h}\n{b}" for h, b in self.sections)])


def fit_prompt(*, goal: str, point: str, about: str, written_for: str, page: PageText, problem: str = "",
               language: Language = "zh") -> str:
    return FIT_PROMPT.format(goal=goal, point=point, about=about, written_for=written_for,
                             page=_fence(page.rendered()), retry=RETRY.format(problem=problem) if problem else "",
                             language_rule=language_rule(language))


def _stated(out: _FitOut) -> bool:
    """Does the answer state a difference, with what holds here, and a note on it?"""
    return any(d.here.strip() for d in out.differences) and bool(out.note.strip())


def _problem(out: _FitOut) -> str:
    """Why an answer with no difference stated cannot be read as a full fit ("" when it can)."""
    failed = [n for n, c in (("definition", out.definition), ("scope", out.scope), ("examples", out.examples),
                             ("steps", out.steps), ("own_object", out.own_object)) if not c.fits]
    if failed or out.remaining != "all":
        return FAILED_CHECK.format(names="、".join(failed) or "remaining")
    return ""


def judge_fit(llm: JsonLLM, *, goal: str, point: str, definition: str, scope: str, page: PageText,
              written_for: str, language: Language = "zh") -> tuple[Verdict, str, str]:
    """(verdict, why, note) for the page on this goal's point (module doc)."""
    about = "".join(f"{x}。" for x in (definition, scope if scope != definition else "") if x)
    problem = ""
    for attempt in range(2):
        out = llm("generator", PURPOSE, fit_prompt(goal=goal, point=point, about=about, written_for=written_for,
                                                   page=page, problem=problem, language=language), _FitOut)
        why = out.why.strip()
        if out.remaining == "little" or not out.same_answer:
            return "none", why, ""
        if _stated(out):
            return "partial", why, out.note.strip()
        problem = _problem(out)
        if not problem:
            return "full", why, ""
        if attempt == 0:
            refused(llm, problem)
    return "none", problem, ""
