"""Model steps and their editable guidance (plan 2026-10-21 参数 redesign, D1 / D2).

Every LLM purpose of the product (tests/llm_cases/inventory.py INVENTORY) belongs to exactly one
step. A step's "how to do it" rules sit inside its prompts between `⟦g:<key>⟧` and `⟦/g⟧`; the role
line, the per-call placeholders, the output format and tool names stay outside, so an edited
guidance can never change an output contract. The gateway (`llm/gateway.py`) replaces each marked
span by the user's override for that step, when there is one, and strips every marker before the
request is hashed, logged or sent: with no override the text is byte for byte the unmarked prompt.
Anything that shows a prompt (service/engine.py) strips the markers the same way.

Only the exact marker forms are touched: other ⟦…⟧ text in a prompt (the writer's ⟦待查证：…⟧) is
left alone."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

MARK_OPEN = "⟦g:{key}⟧"
MARK_CLOSE = "⟦/g⟧"

_SPAN = re.compile(r"⟦g:([a-z0-9_]+)⟧(.*?)⟦/g⟧", re.DOTALL)
_STRAY = re.compile(r"⟦g:[a-z0-9_]+⟧|⟦/g⟧")


@dataclass(frozen=True)
class Step:
    """`purposes`: the LLM purposes of this step. `prompt_of`: the purpose whose prompt carries
    this step's marked guidance (default: the first purpose); a step whose rules live inside
    another step's prompt (页面里的确认题 in the writer's) names that purpose and has none of its
    own. `anchor`: a step of the RSI loops' own machinery that no automatic improvement may change
    (harness/anchors.H1_CLOSED_STEPS; the user still edits it): "judge" = it decides whether a
    change is better or allowed (none since RSI-B's gate and reviewer retired, 2026-10-29),
    "evidence" = it makes what the RSI loops learn from (the feedback reader turns feedback into
    their signals): rewriting it would change the evidence."""

    key: str
    name: str
    purposes: tuple[str, ...]
    description: str
    prompt_of: str = ""
    anchor: Literal["", "judge", "evidence"] = ""

    @property
    def source(self) -> str:
        return self.prompt_of or self.purposes[0]


STEPS: tuple[Step, ...] = (
    # S2 (goals/intake.py): entering a block splits it one level and estimates from his words
    # (goals.enter, enter@v2: one call; the separate gap step 估计你已经会什么 is gone; 知识点细分 —
    # 看不懂, answers right and wrong, asked to split — is the same call and rule since 2026-10-29)
    Step("decompose", "知识点拆分", ("goals.enter",), description="进入一个模块，或你说看不懂、答题有对有错、要求拆细时，把它拆成更细的知识点，并根据你说过的话估计哪些已经会了。修改后影响拆分粒度和初始掌握判断。"),
    # only in S1's prompt when a material is attached (按这份学 / 读懂这篇, intake@v31)
    Step("source_outline", "按资料排大纲", (), prompt_of="goals.intake", description="根据上传资料的目录安排学习内容。修改后影响资料目录如何转成学习大纲。"),
    # the same, when the material is only a reference (stage 3, spec 2026-10-28 §2.5)
    Step("source_reference", "资料只作参考", (), prompt_of="goals.intake", description="资料只作参考时安排学习内容。修改后影响资料里的内容如何补进学习大纲，而不是照着资料的目录排。"),
    Step("understand", "读懂一篇资料", (), prompt_of="goals.intake", description="围绕一份资料安排学习内容。修改后影响如何识别资料重点和需要补充的基础知识。"),
    # S1 (goals/blocks.py): the guidance sits in the blocks prompt (goals.intake, intake@v31: one
    # call per goal, it looks its draft over itself)
    Step("blocks", "先分几大块", ("goals.intake",), description="把较大的学习目标划分为几个模块。修改后影响模块的范围、重点和划分方式。"),
    # a 读懂 goal's unit entered (reading-mode spec §3.5): what it teaches / uses (goals/reading_unit.py)
    Step("page_fit", "已有页面能否复用", ("goals.page_fit",), description="一个知识点已有别的目标写的页面时，判断它是否适合当前目标。修改后影响哪些页面直接打开、哪些只补一段说明、哪些重新写。"),
    Step("reading_unit", "看这一段讲什么", ("goals.reading.unit",), description="读一篇资料时，判断进入的这一段讲什么、用到哪些知识。修改后影响对这一段内容和前置知识的判断。"),
    Step("path", "学习路径规划", ("goals.path",), description="安排知识点的学习顺序。修改后影响先学什么、后学什么，以及每页覆盖的内容。"),
    # the outline and its check items are the writer's plan_page (stage 4): their guidance sits in
    # the outline part of deep.write's first message (deep.agent.outline_part, marks applied there)
    Step("outline", "页面大纲生成", (), prompt_of="deep.write", description="为学习页面安排段落和重点。修改后影响页面结构，以及哪些内容需要重点讲解。"),
    Step("write", "页面内容生成", ("deep.write",), description="生成学习页面的正文。修改后影响讲解方式、例子和表达，不改变固定的输出格式。"),
    Step("check", "页面确认题生成", (), prompt_of="deep.write", description="为页面生成理解确认题。修改后影响题目考查的要点、问法和选项设计。"),
    Step("followup", "替换确认题生成", ("review.followup_check",), description="围绕同一知识点换一道确认题。修改后影响新题如何换问法、换例子，并避免重复原题。"),
    Step("card", "复习卡生成", ("review.card_rewrite",), description="调整复习卡的问法和答案。修改后影响卡片是否清楚、准确，以及是否只考一个要点。"),
    Step("assistant", "陪读助手", ("assistant.turn",), description="控制陪读助手如何回答问题、追问和提出修改建议。修改后影响后续对话中的回答方式。"),
    Step("compact", "对话摘要", ("assistant.compact",), description="对话较长时整理前面的内容，供后续回答参考。修改后影响交接里怎么写来龙去脉、保留哪些内容。"),
    Step("feedback", "学习反馈分析", ("wiki.feedback_understand",), anchor="evidence", description="分析你对学习内容的反馈。修改后影响如何识别没懂的地方，以及需要补充或调整的内容。"),
    # the graph's automatic groups that code cannot name (wikiapp/graph_groups.py, group-naming-v1)
    Step("graph_group_naming", "知识图谱分组起名", ("graph.group_naming",), description="给知识图谱自动分出的组起名字、写一句描述。修改后影响分组的名字和描述，不改变知识点、分组本身或页面内容。"),
    # materials (资料): the import's calls (guidance marked in the toc prompt only: the summary and
    # glossary prompts keep their own rules) and the supplement match
    Step("source_match", "找资料讲到的页", ("sources.match",), description="找出新资料与哪些已有页面相关。修改后影响资料更新时选择哪些页面作为更新候选。"),
)

STEP_BY_KEY: dict[str, Step] = {s.key: s for s in STEPS}
_BY_PURPOSE: dict[str, Step] = {p: s for s in STEPS for p in s.purposes}


def step_of(purpose: str) -> Step | None:
    """The step a purpose belongs to (None: not a product purpose)."""
    return _BY_PURPOSE.get(purpose)


def open_mark(key: str) -> str:
    return MARK_OPEN.format(key=key)


def apply_marks(text: str, overrides: Mapping[str, str]) -> tuple[str, bool]:
    """`text` with every marked span of a step in `overrides` replaced by its override, the other
    spans kept as written, and every marker removed. The flag: an override was applied."""
    if "⟦" not in text:
        return text, False
    applied = False

    def one(m: re.Match[str]) -> str:
        nonlocal applied
        key = m.group(1)
        if key in overrides:
            applied = True
            return overrides[key]
        return m.group(2)

    return _STRAY.sub("", _SPAN.sub(one, text)), applied


def strip_marks(text: str) -> str:
    """`text` without markers (each span keeps its default wording)."""
    return apply_marks(text, {})[0]


def marked(text: str) -> dict[str, str]:
    """The default guidance of each step marked in `text` (first span per key), unmarked."""
    out: dict[str, str] = {}
    for m in _SPAN.finditer(text):
        out.setdefault(m.group(1), strip_marks(m.group(2)))
    return out
