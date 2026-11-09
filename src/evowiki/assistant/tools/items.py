"""Check items the assistant writes in a tool's arguments (stage 3 T11 / T12): the quiz, a
diagnosis check, a review card's 确认题. One typed shape (`ItemArg`: a nested object, never JSON in a
string) and one check, run in the tool step on the cell CODE names (the model cannot move the cell):
it must build as a gradable `CheckItem` (2–8 distinct options, the key in range), carry no LaTeX
and none of the existing item-writing flaws (`goals.itemflaws.judge_check`, the same checks the
removed generator calls retried on). A refused item is a tool error naming why, so the assistant
writes it again; nothing is stored. The options are shuffled deterministically (`_shuffled`)."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from evowiki.goals.contracts import CheckItem
from evowiki.goals.diagnostic import LATEX_WHY, _shuffled, item_latex
from evowiki.goals.itemflaws import judge_check

Option = Annotated[str, Field(min_length=1, max_length=200)]


class ItemArg(BaseModel):
    """A single-answer question that tests one cell."""

    model_config = ConfigDict(extra="forbid")

    stem: str = Field(min_length=1, max_length=400,
                      description="题干：问清楚问的是什么，纯文本，不用 LaTeX")
    options: list[Option] = Field(min_length=2, max_length=8,
                                  description="选项，只有一个对；不要写「不知道」，界面会自己加")
    answer: int = Field(ge=0, le=7, description="正确选项的序号（从 0 开始）")


class ItemRefused(ValueError):
    """Why an item cannot be asked (the tool error's text)."""


def checked_item(cell: str, arg: ItemArg) -> CheckItem:
    """`arg` as a verified, shuffled `CheckItem` on `cell`; ItemRefused when it is not gradable,
    uses LaTeX or has an item-writing flaw."""
    options = tuple(" ".join(o.split()) for o in arg.options)
    built, flaws = judge_check(cell, " ".join(arg.stem.split()), options, arg.answer)
    if built is None:
        raise ItemRefused("这道题没法判分：" + ("；".join(flaws) if flaws
                                              else "选项要 2 到 8 个、互不相同，答案序号要在范围内"))
    if (found := item_latex(built)) is not None:
        flaws = (*flaws, LATEX_WHY.format(found=found))
    if flaws:
        raise ItemRefused("这道题要改：" + "；".join(flaws))
    return _shuffled(built).model_copy(update={"verified": True})
