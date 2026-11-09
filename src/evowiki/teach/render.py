"""Rendering the teaching spec into the `guidelines` lines every writer prompt already reads
(`PageSpec.guidelines`): the item texts in spec order, then 4 fixed Chinese lines for `RepParams`
(always, defaults included)."""

from collections.abc import Iterable

from evowiki.i18n import current_language
from evowiki.teach.contracts import RepParams, SpecItem

ANALOGY = ("不用类比", "类比只借用他已经确定会的东西", "可以多用类比，但要说明类比在哪里不成立")
FADING = ("例题每一步都写全", "例题先写全一遍，再逐步留空让他自己补", "例题尽早留空让他自己做")
INTERACTIVE = ("尽量不用交互，用文字和静态例子", "确实需要时才用交互", "能用交互演示的优先用交互")


def recap_line(n: int) -> str:
    return "这一页不放提醒卡" if n == 0 else f"每页提醒卡（已会内容的一句提醒）最多 {n} 张"


def param_effect(path: str, value: int) -> str:
    """What one param's value does to the next pages, in the words the writer reads."""
    if current_language() == "en":
        if path == "recap_max_per_page":
            return "No recap cards" if value == 0 else f"At most {value} recap cards per page"
        choices = {
            "analogy_level": ("Avoid analogies", "Use analogies only from established knowledge",
                              "Use analogies and explain their limits"),
            "fading_level": ("Show every worked-example step", "Show a complete example before gradually leaving gaps",
                             "Let the learner fill gaps early"),
            "interactive_bias": ("Prefer text and static examples", "Use interaction when needed",
                                 "Prefer interaction when it helps demonstrate the idea"),
        }
        return choices[path][value]
    if path == "recap_max_per_page":
        return recap_line(value)
    return {"analogy_level": ANALOGY, "fading_level": FADING, "interactive_bias": INTERACTIVE}[path][value]


def param_lines(params: RepParams) -> tuple[str, ...]:
    """Always 4 lines: recap, analogy, fading, interactive."""
    return (recap_line(params.recap_max_per_page), ANALOGY[params.analogy_level],
            FADING[params.fading_level], INTERACTIVE[params.interactive_bias])


def item_text(item: SpecItem) -> str:
    """An item as a model reads it: its text, and for a domain item the domain it is for, so the
    reader (the writer on a page without a domain, the assistant) judges whether it fits."""
    return f"{item.text}（只用于「{item.domain}」的内容）" if item.domain else item.text


def guideline_lines(items: Iterable[SpecItem], params: RepParams) -> tuple[str, ...]:
    return (*(item_text(i) for i in items), *param_lines(params))
