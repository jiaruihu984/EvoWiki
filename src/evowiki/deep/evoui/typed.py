"""Typed block templates: the EvoUI blocks a model may write through STRUCTURED OUTPUT. Pure data — no IO, no LLM.

A full EvoUI block is a recursive node tree with open `state` / `derived` maps; Anthropic structured outputs accept
neither recursion nor open objects, and allow at most 16 union-typed parameters per request (live docs 2026-10-23), so
the tree cannot be the response schema. These templates can: each is a closed, fully typed object (no nullable field:
an absent text is ""), the set of them is ONE union (`BlockSpec`, told apart by `kind`), and `compile_block` turns the
chosen one into the EvoUI block the validators and the renderer already know (`deep.evoui.validate`, evoui.js). So the
model cannot answer with a block of the wrong shape (wikiapp.understand); what a schema cannot say (an
expression's grammar, a quiz cell of this page, the render check) is checked right after the call, in the same step,
and fed back for a repair turn.

The node fields used here are the contract's (`contracts.NODE_FIELDS` / `QUESTION_FIELDS` / `CARD_FIELDS` /
`FILL_OPTION_FIELDS`); tests/deep/test_evoui_typed_templates.py checks every compiled field against those tables and every
compiled example against the validator, so the templates cannot drift from the contract."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evowiki.deep.evoui.contracts import EVOUI_TYPE


class _T(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Head(_T):
    title: str = Field(description="块的标题，短")
    question: str = Field(description="它要解决的理解障碍，一句问句")
    fallback: str = Field(description="块显示不出来时给的纯文字，讲清同一件事")


class QuizQuestion(_T):
    q: str = Field(description="题干")
    options: list[str] = Field(description="2–8 个选项")
    correct: int = Field(description="正确选项的序号，从 0 开始")
    why: str = Field(description="为什么对、错的错在哪，一两句")
    hint: str = Field(description="点开才看的提示，没有就空字符串")
    cell: str = Field(description="考到的格子（概念.层面），只能用这一页的格子；没有就空字符串")


class QuizSpec(_Head):
    kind: Literal["quiz"]
    layout: Literal["list", "paged"] = Field(description="list=一次全给，paged=一页一题当场判")
    questions: list[QuizQuestion]


class Card(_T):
    front: str
    back: str
    hint: str = Field(description="没有就空字符串")
    cell: str = Field(description="考到的格子，只能用这一页的格子；没有就空字符串")


class FlashcardsSpec(_Head):
    kind: Literal["flashcards"]
    cards: list[Card] = Field(description="至少 2 张")


class FillOption(_T):
    label: str
    why: str = Field(description="选它时给的一句解释，没有就空字符串")


class FillItem(_T):
    sentence: str = Field(description="一句纯文字，正好一个 ___")
    options: list[FillOption] = Field(description="2–8 个候选")
    correct: int = Field(description="正确候选的序号，从 0 开始")
    why: str
    cell: str = Field(description="没有就空字符串")


class FillBlankSpec(_Head):
    kind: Literal["fill_blank"]
    items: list[FillItem] = Field(description="一道或几道填空；几道时一页一道")


class SentenceItem(_T):
    sentence: str = Field(description="纯文字，1–8 个 ___")
    words: list[str] = Field(description="词库：至少和空一样多，最多 12 个，不重复，可以多给干扰词")
    answer: list[int] = Field(description="每个空填的词的序号（从 0 开始），按空的顺序")
    why: str
    cell: str = Field(description="没有就空字符串")


class SentenceSpec(_Head):
    kind: Literal["sentence"]
    items: list[SentenceItem] = Field(description="拼句 / 配对：按顺序点词填满所有空；一道或几道，几道时一页一道")


class StepsSpec(_Head):
    kind: Literal["steps"]
    steps: list[str] = Field(description="一步一步揭开的几段话（1–12 步），每步一小段纯文字")


class TimelineEvent(_T):
    when: str = Field(description="时间或阶段的标签（如 1905 年、第一阶段）；只是步骤顺序时给空字符串，会自动编号")
    title: str = Field(description="这一项是什么，短")
    text: str = Field(description="一两句说明，没有就空字符串")
    tone: Literal["default", "accent", "warning", "success", "danger"] = Field(
        description="圆点的颜色：关键的一项用 accent，其余 default")


class TimelineSpec(_Head):
    kind: Literal["timeline"]
    intro: str = Field(description="时间线上方的一句说明：按什么排、看什么；没有就空字符串")
    events: list[TimelineEvent] = Field(description="按先后顺序的事件或步骤，至少 2 项")


class DeepDiveSpec(_Head):
    kind: Literal["deep_dive"]
    intro: str = Field(description="折叠行上方的一句话：展开能看到什么、谁需要看；没有就空字符串")
    label: str = Field(description="折叠行上的字，如「深入一点：为什么是 1/2」；空字符串时显示「深入一点」")
    paragraphs: list[str] = Field(description="展开后的几段话（纯文字），讲正文略过的推导、例外或来龙去脉")


class Control(_T):
    name: str = Field(description="状态名，小写字母开头，如 deg")
    label: str = Field(description="滑块上方的说明，可以用 {表达式} 显示当前值")
    min: float
    max: float
    step: float
    value: float = Field(description="初始值，在 min 和 max 之间")


class Derived(_T):
    name: str = Field(description="算出来的量的名字")
    expr: str = Field(description="表达式（EvoUI 表达式语言），可以用状态和前面算出来的量")


class Readout(_T):
    text: str = Field(description="显示的一行字，用 {表达式} 插入数值")
    tone: Literal["default", "secondary", "accent", "success", "warning", "danger"]


class Series(_T):
    key: str = Field(description="data 里每个对象的数值字段名")
    label: str


class Chart(_T):
    show: bool = Field(description="要不要画图；false 时下面的字段随便给空值")
    chart: Literal["line", "area", "bar", "scatter"]
    data: str = Field(description="表达式，结果是对象数组，如 map(range(21), i => {\"x\": i, \"y\": a * i})")
    x: str = Field(description="data 里做横轴的字段名")
    series: list[Series] = Field(description="1–4 个系列")
    x_label: str = Field(description="横轴名，没有就空字符串")
    y_label: str = Field(description="纵轴名，没有就空字符串")


class LabSpec(_Head):
    kind: Literal["lab"]
    intro: str = Field(description="块顶上的一句说明：拖什么、看什么；没有就空字符串")
    controls: list[Control] = Field(description="1–4 个滑块")
    derived: list[Derived] = Field(description="由滑块算出的量，按依赖顺序")
    chart: Chart = Field(description="随滑块变的图（看一个量怎么随另一个变）；不需要就 show=false")
    readouts: list[Readout] = Field(description="拖动时跟着变的几行字")


BlockSpec = QuizSpec | FlashcardsSpec | FillBlankSpec | SentenceSpec | StepsSpec | LabSpec | TimelineSpec | DeepDiveSpec
KINDS: tuple[str, ...] = ("quiz", "flashcards", "fill_blank", "sentence", "steps", "lab", "timeline", "deep_dive")

GUIDE = """块按下面的类型之一写（kind）：
- quiz：选择题测验（questions；layout paged 一页一题当场判，list 一次全给）
- flashcards：记忆闪卡（cards，至少 2 张）
- fill_blank：填空，点一个候选填进空里（items，每道正好一个 ___）
- sentence：拼句 / 配对，按顺序点词填满所有空（items，每道 1–8 个 ___，answer 按空的顺序给词的序号）
- steps：一步一步揭开的讲解（steps）
- lab：拖滑块看数值怎么变（controls 是滑块，derived 用表达式由滑块算出的量，chart 是随滑块变的图，readouts 用 {表达式} 显示）
- timeline：按先后排的事件或阶段（events；顺序本身就是要记的东西时用：历史事件、发展阶段、一个过程的几个环节）
- deep_dive：折叠起来的「深入一点」（paragraphs）：正文不需要、但有人想知道的推导、例外、来龙去脉；读者自己点开才看，
  所以必须读的内容不要放进去
所有文字都是纯文字（不是 HTML、不是 Markdown）。没有的可选文字给空字符串。"""


def guide() -> str:
    """What a model reads about the templates: `GUIDE` plus the spec's own expression section (lab)."""
    from evowiki.deep.evoui import spec_text
    spec = spec_text()
    start = spec.index("## 表达式")
    end = spec.index("\n## ", start + 1)
    return GUIDE + "\n\nlab 的" + spec[start + 3:end].strip()


def _cell(node: dict[str, Any], cell: str) -> dict[str, Any]:
    return {**node, "cell": cell.strip()} if cell.strip() else node


def _paged(nodes: list[dict[str, Any]], pid: str) -> dict[str, Any]:
    return nodes[0] if len(nodes) == 1 else {"kind": "pager", "id": pid, "gate": True, "children": nodes}


def compile_block(spec: BlockSpec) -> dict[str, Any]:
    """The EvoUI block of a template (not validated here: `deep.evoui.validate` does that, so a template that compiles
    to an invalid block is reported like any other invalid block)."""
    head = {"type": EVOUI_TYPE, "title": spec.title.strip(), "question": spec.question.strip(),
            "fallback": spec.fallback.strip()}
    state: dict[str, Any] = {}
    derived: dict[str, str] = {}
    if isinstance(spec, QuizSpec):
        qs = []
        for x in spec.questions:
            q: dict[str, Any] = {"q": x.q, "options": x.options, "correct": x.correct, "why": x.why}
            if x.hint.strip():
                q["hint"] = x.hint
            qs.append(_cell(q, x.cell))
        body: dict[str, Any] = {"kind": "quiz", "id": "quiz", "layout": spec.layout, "questions": qs}
    elif isinstance(spec, FlashcardsSpec):
        cards = []
        for c in spec.cards:
            card: dict[str, Any] = {"front": c.front, "back": c.back}
            if c.hint.strip():
                card["hint"] = c.hint
            cards.append(_cell(card, c.cell))
        body = {"kind": "flashcards", "id": "cards", "cards": cards}
    elif isinstance(spec, FillBlankSpec):
        body = _paged([_cell({"kind": "fill-blank", "id": f"blank{i}", "sentence": x.sentence,
                              "options": [{"label": o.label, **({"why": o.why} if o.why.strip() else {})}
                                          for o in x.options],
                              "correct": x.correct, "why": x.why}, x.cell)
                       for i, x in enumerate(spec.items, 1)], "blanks")
    elif isinstance(spec, SentenceSpec):
        body = _paged([_cell({"kind": "sentence", "id": f"pair{i}", "sentence": x.sentence, "words": x.words,
                              "answer": x.answer, "why": x.why}, x.cell)
                       for i, x in enumerate(spec.items, 1)], "pairs")
    elif isinstance(spec, TimelineSpec):
        evs = [{"title": x.title, **({"when": x.when} if x.when.strip() else {}),
                **({"text": x.text} if x.text.strip() else {}), **({"tone": x.tone} if x.tone != "default" else {})}
               for x in spec.events]
        kids = [{"kind": "caption", "text": spec.intro}] if spec.intro.strip() else []
        return {**head, "state": state, "derived": derived,
                "body": {"kind": "box", "gap": 3, "children": [*kids, {"kind": "timeline", "events": evs}]}}
    elif isinstance(spec, DeepDiveSpec):
        dd: dict[str, Any] = {"kind": "deep-dive", "children": [{"kind": "text", "text": t} for t in spec.paragraphs]}
        if spec.label.strip():
            dd["label"] = spec.label
        kids = [{"kind": "caption", "text": spec.intro}] if spec.intro.strip() else []
        return {**head, "state": state, "derived": derived, "body": {"kind": "box", "children": [*kids, dd]}}
    elif isinstance(spec, StepsSpec):
        body = {"kind": "reveal", "id": "steps", "mode": "steps",
                "children": [{"kind": "text", "text": s} for s in spec.steps]}
    else:
        state = {c.name: c.value for c in spec.controls}
        derived = {d.name: d.expr for d in spec.derived}
        kids: list[dict[str, Any]] = []
        if spec.intro.strip():
            kids.append({"kind": "caption", "text": spec.intro})
        kids += [{"kind": "slider", "bind": c.name, "min": c.min, "max": c.max, "step": c.step, "label": c.label}
                 for c in spec.controls]
        if spec.chart.show:
            ch = spec.chart
            kids.append({"kind": "chart", "chart": ch.chart, "data": ch.data, "x": ch.x,
                         "series": [{"key": s.key, "label": s.label} for s in ch.series],
                         **({"x_label": ch.x_label} if ch.x_label.strip() else {}),
                         **({"y_label": ch.y_label} if ch.y_label.strip() else {})})
        kids += [{"kind": "text", "text": r.text, **({"tone": r.tone} if r.tone != "default" else {})}
                 for r in spec.readouts]
        return {**head, "state": state, "derived": derived, "body": {"kind": "box", "gap": 3, "children": kids}}
    return {**head, "state": state, "derived": derived, "body": {"kind": "box", "children": [body]}}
