"""「看这一段」 (reading-mode spec §3.5): when the learner enters a unit of a 读懂 goal that is not
analyzed yet, ONE call (purpose `goals.reading.unit`, reading-unit@v1, step 「看这一段讲什么」)
reads the unit's own text and says what it explains (`teaches`), what it uses without explaining
(`uses`: the goal's prerequisite blocks, existing nodes, or a prerequisite found now) and what the
page should explain closely (`focus`).

Only 「怎么做」 is in the prompt (§11: the text moves into a read-paper skill later); what each
field holds is the schema's (`_UnitOut`, strict: a malformed field fails the call in the model
step). Code checks what code can (§2.3), then asks once more with the problems, then IntakeError
(nothing written, the unit stays unanalyzed): at least one teaches; every `existing_id` the wiki
has, every `block` one of the goal's prerequisite blocks; teaches unique across the reading's
units (a point an earlier unit teaches is a use here); teaches ∩ uses = ∅; a teaches point is
never one of the goal's prerequisite blocks (those are on the map; teaches never are). What code
can settle it settles without asking again: a teaches point on the map or taught earlier becomes a
use, a point both taught and used here stays taught. Names are
compared normalized (`norm_name`), as `wikiapp.goal._block_ids` resolves a new name — so a name
that passes here resolves to an id no other unit teaches. Pure: the LLM is injected."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from evowiki.goals.intake import (
    KNOWN_BLOCK,
    ConceptRef,
    IntakeError,
    IntakeRelation,
    TopicRef,
    _fence,
    _known_rows,
    _refs,
)
from evowiki.goals.language import Language, language_rule
from evowiki.goals.observe import JsonLLM, refused
from evowiki.plain_text import norm_name

PURPOSE = "goals.reading.unit"
STEP = "reading_unit"


# --- LLM output (nothing trusted yet) -------------------------------------------------------

class _RawTeach(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    name: str = Field(default="", description="这个知识点的名字（简短的名词短语）")
    summary: str = Field(default="", description="一句话：这一段把它讲成了什么")
    atomic: bool = Field(default=False, description=(
        "它本身就是一件能独立学习、独立检验的事：true；包含几件可以分开学的事：false"))
    existing_id: str = Field(default="", description="知识图谱里已有同一个点时，写它的 ID；新的点不写")


class _RawUse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    block: str = Field(default="", description="是这个目标的前置块时，照原样写块名")
    existing_id: str = Field(default="", description="是知识图谱里已有的节点时，写它的 ID")
    name: str = Field(default="", description="都不是（新发现的前置）时写名字")
    summary: str = Field(default="", description="新发现的前置：一句话说它是什么")
    atomic: bool = Field(default=False, description="新发现的前置：本身就是一件能独立学的事时 true")


class _UnitOut(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    teaches: list[_RawTeach] = Field(default=[], description="这一段讲清楚的知识点，至少一个")
    uses: list[_RawUse] = Field(default=[], description="这一段用到、但没有讲的知识点；没有就是 []")
    focus: list[str] = Field(default=[], description=(
        "写这一页时要讲细的地方，每条一个短句；引用原文用「」"))


# --- input and result -----------------------------------------------------------------------

@dataclass(frozen=True)
class UnitRow:
    """A unit of the reading as the prompt lists it (its place in the whole text)."""

    order: int
    title: str
    about: str = ""
    teaches: tuple[str, ...] = ()      # names of what an analyzed unit teaches


@dataclass(frozen=True)
class UnitInput:
    """What the 「看这一段」 call reads. `text`: the unit's own text (`SourceExcerpts.for_spans`;
    a summary when it is too long), already joined; `units`: every unit of the reading in order;
    `order`: this one's; `blocks`: the goal's prerequisites (id, name): every node of its map,
    an entered block's parts too (a point on the map is never taught, §2.3);
    `taught`: what the reading's other units teach (id → name); the existing graph as S1 sees it
    (`existing`, `annotations`: K's evidence, `topics`, `graph_relations`)."""

    title: str
    text: str
    units: tuple[UnitRow, ...]
    order: int
    blocks: tuple[tuple[str, str], ...] = ()
    taught: Mapping[str, str] = field(default_factory=dict)
    existing: Sequence[ConceptRef] = ()
    annotations: Mapping[str, str] = field(default_factory=dict)
    topics: Sequence[TopicRef] = ()
    graph_relations: Sequence[IntakeRelation] = ()
    language: Language = "zh"


@dataclass(frozen=True)
class UnitPoint:
    """One point the call named: an existing node (`existing_id`), one of the goal's prerequisite
    blocks (`block_id`, uses only) or a new node by `name` (resolved to an id by the writer:
    `wikiapp.goal._block_ids`). `summary` / `atomic`: the model's, for a new node."""

    name: str
    summary: str = ""
    atomic: bool = False
    existing_id: str = ""
    block_id: str = ""


@dataclass(frozen=True)
class UnitAnalysis:
    teaches: tuple[UnitPoint, ...]
    uses: tuple[UnitPoint, ...]
    focus: tuple[str, ...]


# --- prompt -----------------------------------------------------------------------------------

# How to read one unit (step 「看这一段讲什么」, marked so the 参数 tab can edit it). Only how to do
# it (spec §11); the fields are explained in the schema.
UNIT_GUIDE = """⟦g:reading_unit⟧- 只看这一段原文本身：它讲清楚了哪些知识点（通常是这篇自己的方法、模型、结论里的概念），用到了哪些它没有讲的知识点。
- 前面的段已经讲过的点，这一段再用到时算用到，不算讲清楚。
- 用到的点是这个目标的前置块或图谱里已有的节点时，指向它；都不是时才算新发现的前置，只列这一段真正离不开的。
- 讲解重点挑学习者读原文时最可能卡住的地方：难读的句子、关键的论证或推导，几条就够。⟦/g⟧"""

UNIT_PROMPT = """你在带学习者读《{title}》，一段一段读。现在进入第 {order} 段，先看这一段讲什么。
全文的段落安排：
{units}
这一段的原文：
{text}
这个目标的前置块：{blocks}
{known}{guide}
{retry}{language_rule}"""
UNIT_ROW = "{order}. {title}{about}{taught}{mark}"
THIS_UNIT = " ← 这一段"
RETRY = "上一次的回答不能用：{problems}。请重新回答。\n"


def _row(u: UnitRow, order: int) -> str:
    return UNIT_ROW.format(order=u.order, title=u.title, about=f"：{u.about}" if u.about else "",
                           taught=f"（讲过：{'、'.join(u.teaches)}）" if u.teaches else "",
                           mark=THIS_UNIT if u.order == order else "")


def unit_prompt(inp: UnitInput, problems: Sequence[str] = ()) -> str:
    """The 「看这一段」 prompt (reading-unit@v1): the reading's units (this one marked, what the
    analyzed ones teach), this unit's text fenced as untrusted data, the goal's prerequisite
    blocks, the existing graph with K's evidence (only when the wiki has nodes), the guide;
    `problems`: why the last answer could not be used."""
    refs = _refs(inp.existing, inp.topics)
    rows = "\n".join(_known_rows(refs, inp.annotations, inp.topics, inp.graph_relations))
    return UNIT_PROMPT.format(
        title=inp.title, order=inp.order,
        units="\n".join(_row(u, inp.order) for u in inp.units),
        text=_fence(inp.text), blocks="、".join(f"「{n}」" for _, n in inp.blocks) or "无",
        known=KNOWN_BLOCK.format(rows=rows) if rows else "", guide=UNIT_GUIDE,
        retry=RETRY.format(problems="；".join(problems)) if problems else "",
        language_rule=language_rule(inp.language))


# --- parsing and the checks code can do (§2.3) ------------------------------------------------

def _parse(out: _UnitOut, inp: UnitInput) -> tuple[UnitAnalysis | None, list[str]]:
    known = {c.id: c.name for c in _refs(inp.existing, inp.topics)}
    blocks = {norm_name(n): (i, n) for i, n in inp.blocks}
    block_ids = {i for i, _ in inp.blocks}
    taught = {i: norm_name(n) for i, n in inp.taught.items()}
    problems: list[str] = []
    teaches: list[UnitPoint] = []
    moved: list[UnitPoint] = []     # a teaches point that is on the map or taught before: a use
    for t in out.teaches:
        eid, name = t.existing_id.strip(), t.name.strip()
        if eid and eid not in known:
            problems.append(f"teaches 的 existing_id「{eid}」图谱里没有")
            continue
        name = name or known.get(eid, "")
        if not name:
            problems.append("teaches 里有一项没有 name")
            continue
        key = norm_name(name)
        if eid in block_ids and key not in blocks:
            eid = ""                 # a narrower point of that block, named by the block's id: a new point
        if (eid and eid in block_ids) or (not eid and key in blocks):
            hit = next(b for b in inp.blocks if b[0] == eid or norm_name(b[1]) == key)
            moved.append(UnitPoint(name=hit[1], block_id=hit[0]))
            continue
        if (eid and eid in taught) or (not eid and key in taught.values()):
            moved.append(UnitPoint(name=name, existing_id=eid or next(
                i for i, n in taught.items() if n == key)))
            continue
        if any((eid and p.existing_id == eid) or norm_name(p.name) == key for p in teaches):
            continue                                     # the same point twice: once
        teaches.append(UnitPoint(name=name, summary=t.summary.strip(), atomic=t.atomic,
                                 existing_id=eid))
    if not teaches and not problems:
        problems.append("teaches 至少要有一个：这一段讲清楚了什么")
    uses = _once(moved)
    for u in out.uses:
        eid, block, name = u.existing_id.strip(), u.block.strip(), u.name.strip()
        point: UnitPoint | None = None
        if eid:
            if eid not in known and eid not in block_ids:
                problems.append(f"uses 的 existing_id「{eid}」图谱里没有")
                continue
            hit = next((b for b in inp.blocks if b[0] == eid), None)
            point = (UnitPoint(name=hit[1], block_id=eid) if hit is not None
                     else UnitPoint(name=known[eid], existing_id=eid))
        elif block:
            hit = blocks.get(norm_name(block))
            if hit is None:
                problems.append(f"uses 的 block「{block}」不是这个目标的前置块")
                continue
            point = UnitPoint(name=hit[1], block_id=hit[0])
        elif name:
            hit = blocks.get(norm_name(name))
            point = (UnitPoint(name=hit[1], block_id=hit[0]) if hit is not None else
                     UnitPoint(name=name, summary=u.summary.strip(), atomic=u.atomic))
        else:
            problems.append("uses 里有一项既没有 block、existing_id，也没有 name")
            continue
        if any(_same(point, p) for p in uses):
            continue
        uses.append(point)
    uses = [p for p in uses if not any(_same(p, t) for t in teaches)]    # taught here: not a use
    if problems:
        return None, problems
    return UnitAnalysis(teaches=tuple(teaches), uses=tuple(uses),
                        focus=tuple(dict.fromkeys(f.strip() for f in out.focus if f.strip()))), []


def _once(points: Sequence[UnitPoint]) -> list[UnitPoint]:
    out: list[UnitPoint] = []
    for p in points:
        if not any(_same(p, q) for q in out):
            out.append(p)
    return out


def _same(a: UnitPoint, b: UnitPoint) -> bool:
    ida, idb = a.existing_id or a.block_id, b.existing_id or b.block_id
    if ida or idb:
        return ida == idb
    return norm_name(a.name) == norm_name(b.name)


def analyze_unit(llm: JsonLLM, inp: UnitInput) -> UnitAnalysis:
    """「看这一段」: one call; an answer code cannot use (module doc) is asked once more with the
    problems; unusable again is an IntakeError (fail closed: nothing is returned). Call errors
    propagate (the gateway's parse retry covers a malformed field)."""
    problems: list[str] = []
    for _ in range(2):
        out = llm("generator", PURPOSE, unit_prompt(inp, problems), _UnitOut)
        got, problems = _parse(out, inp)
        if got is not None:
            return got
        refused(llm, "；".join(problems))
    raise IntakeError("看这一段的结果用不了：" + "；".join(problems))
