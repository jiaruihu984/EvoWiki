"""Understanding one page feedback (Obsidian flow design §4.1, B1): ONE LLM call reads the user's own
words, the picked feelings, the page's sections (heading + opening), K's bands for the page's concepts,
the teaching rules in force and the component library, and returns `FeedbackReading`:

- `reading`: the user's meaning in one sentence — it must restate their words (a written feedback's
  reading has to share at least one two-character piece with it, else it is not trusted);
- `aspects`: one feedback can carry several aspects; each separates the issue (问题: what is wrong for
  the learner, in their words, with an optional verbatim `quote`) from the action (改法). An aspect
  without both, or whose quote is not verbatim, is dropped and named in `dropped`. Consumers that
  read one summary get the actions in order (`page.summary` falls back to them);
- `missing`: concepts the user said they do not know — only this page's or its path's concepts;
- `page`: paragraphs to add to named sections, at most one library component (never invented);
- `style`: problem type + the rule as it reads now + the rule it should become (RSI-B's seed).

Anything unusable fails closed (`NotUnderstood`): the caller then only records the signal and says the
feedback was not understood. Unknown sections / concepts are dropped; an unknown component is dropped
and named in `dropped`."""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, ValidationError, WithJsonSchema, create_model

from evowiki.deep.components import ConfigInvalid, cell_problems, validate_config
from evowiki.deep.evoui import TIERS_RULE
from evowiki.deep.evoui import validate as validate_block
from evowiki.deep.evoui.typed import BlockSpec, compile_block, guide
from evowiki.deep.library import catalog_line
from evowiki.goals.contracts import SectionRole
from evowiki.goals.guardrails import BAND_ZH, page_bands
from evowiki.llm.gateway import LLMParseError
from evowiki.llm.models import LLMRequest
from evowiki.llm.run_context import reject
from evowiki.teach.contracts import (
    CHIP_LABEL,
    FeedbackReading,
    PageFeedbackIn,
    PagePlan,
    SectionAddition,
    StylePlan,
)
from evowiki.teach.standard import section, sections
from evowiki.wikiapp.component_fill import fill_config
from evowiki.wikiapp.kwrite import concept_name, page_spec
from evowiki.wikiapp.llm import JSON_PROMPT_VERSION, CountingGateway
from evowiki.wikiapp.repo import Snapshot
from evowiki.wikiapp.teachspec import page_teach

PURPOSE = "wiki.feedback_understand"
# v6: issue (问题) and action (改法) written apart, several aspects per feedback; v7: + EvoUI v2 four tiers (app) and templates;
# v8: interactives have three uses (看懂 / 练熟 / 辨析), standard@v9; v9: component_config and block
# are JSON strings (an open object is rejected by Anthropic structured outputs); v10: summary names sections by title, never by id;
# v11: block is a typed template (deep.evoui.typed), component / config typed per call (`reply_schema`), repair turns;
# v12: the config is a call of its own with the chosen component's schema (the library's configs together exceed
#      Anthropic's 16 union-typed parameters)
PROMPT_VERSION = JSON_PROMPT_VERSION[PURPOSE]
# Teaching standard injected into the prompt: the self-check ids (the style proposal says which one it
# touches) and the writing rules the `add` paragraphs must follow (they are appended verbatim).
# (standard@v14: visual_question is one paragraph now, so it goes in whole; order likewise)
STANDARD = "\n\n".join([section("self_check"), sections("terms_first", "tables_after", "order",
                                                          "spoken_first", "feedback_split",
                                                          "visual_question", "interactive_tiers"),
                         # the one component rule visual_question carried before standard@v14
                         "只用组件库里的组件，不能编造库里没有的组件。"])
SYSTEM = "只回复 JSON。"
OPENING = 160
MAX_ADDS = 4
MAX_ASPECTS = 4
REPAIR_TURNS = 2        # repair turns for a block / config the validators refuse (a harness bound)
MAX_PROBLEMS = 8


class NotUnderstood(Exception):
    """The understanding call's answer cannot be used (fail closed)."""


class _Sec(BaseModel):
    section_id: str
    add: str


# The block is a typed template (deep.evoui.typed: closed objects, one union), so a structured-output
# provider cannot answer with a block of the wrong shape. The component config is typed per call
# (`reply_schema`): the union of the configurable components' own config schemas (library schemas are
# closed and list every property, `deep.components.check_schema`), and `component` an enum of the ids.
class _Page(BaseModel):
    summary: str = ""
    sections: list[_Sec] = []
    component: str | None = None
    component_after: str | None = None
    component_config: dict[str, Any] | None = None
    component_why: str = ""
    block: BlockSpec | None = None
    block_why: str = ""


class _Style(BaseModel):
    kind: str
    before: str
    after: str


class _Aspect(BaseModel):
    aspect: str = ""
    quote: str = ""
    issue: str = ""
    action: str = ""


class FeedbackAspect(BaseModel):
    """One aspect of a feedback: what is wrong for the learner (`issue`, in their words; `quote` is
    their verbatim words or empty) kept apart from how the page changes (`action`)."""

    model_config = ConfigDict(frozen=True)

    aspect: str
    quote: str = ""
    issue: str
    action: str


class UnderstandOut(BaseModel):
    """What the model answers (lenient); `validate` turns it into the contract's FeedbackReading."""

    reading: str
    aspects: list[_Aspect] = []
    missing: list[str] = []
    page: _Page
    style: _Style


@dataclass(frozen=True)
class Context:
    """What the call sees and what validation checks against."""

    page_id: str
    title: str
    sections: tuple[tuple[str, str, str], ...]          # (id, heading, opening), page order
    concepts: dict[str, str]                             # concept id → name (page + its path)
    bands: tuple[str, ...]                               # "方差：术语 / 定义 确定会；…"
    rules: tuple[str, ...]                               # teaching rules in force on the page
    components: dict[str, str]                           # component id → one line
    schemas: dict[str, dict[str, Any] | None] = field(default_factory=dict)  # id → config schema
    cells: tuple[str, ...] = ()                          # the page's K cells (a graded component's items name them)


@dataclass(frozen=True)
class Understood:
    reading: FeedbackReading
    dropped: tuple[str, ...] = ()                        # what the model named but does not exist
    aspects: tuple[FeedbackAspect, ...] = ()             # issue / action pairs, in the model's order


def _path_concepts(snap: Snapshot, page_id: str) -> tuple[str, ...]:
    out: list[str] = []
    for rec in snap.state.paths.values():
        if page_id in rec.page_ids:
            out.extend(c.id for c in rec.path.concepts)
    return tuple(out)


def context(snap: Snapshot, page_id: str, now: datetime, components: Mapping[str, str],
            schemas: Mapping[str, dict[str, Any] | None] | None = None) -> Context:
    """LookupError: unknown page; ValueError: the page is not organized yet."""
    state = snap.state
    rec = state.pages.get(page_id)
    if rec is None:
        raise LookupError(f"没有这个页面：{page_id}")
    if rec.page is None:
        raise ValueError("页面还没生成")
    page = rec.page
    secs = tuple((s.id, s.heading, " ".join(page.texts[s.id].body.split())[:OPENING])
                 for s in page.outline.sections
                 if s.role is not SectionRole.CHECK and s.id in page.texts)
    ids = dict.fromkeys([*rec.concepts, *_path_concepts(snap, page_id)])
    concepts = {c: concept_name(state, c, user_id=snap.user_id) for c in ids}
    spec = page_spec(state, snap.k, page_id, user_id=snap.user_id, bands_only=True)
    bands = page_bands(spec, snap.kview(now))
    lines = []
    for c in spec.concepts:
        cells = [f"{x.split('.', 1)[1]} {BAND_ZH[b]}" for x, b in bands.items()
                 if x.split(".", 1)[0] == c.id]
        lines.append(f"{c.name}：{'，'.join(cells) or '没有证据'}")
    try:
        rules = tuple(i.text for i in page_teach(state, page_id).items)
    except ValueError:
        rules = ()
    return Context(page_id=page_id, title=rec.title, sections=secs, concepts=concepts,
                   bands=tuple(lines), rules=rules, components=dict(components),
                   schemas=dict(schemas or {}), cells=tuple(bands))


def prompt(ctx: Context, body: PageFeedbackIn) -> str:
    secs = "\n".join(f"- [{sid}] {h}｜{o}" for sid, h, o in ctx.sections) or "（无）"
    concepts = "、".join(f"{n}（{c}）" for c, n in ctx.concepts.items()) or "（无）"
    comps = "\n".join(f"- {cid}：{line}" for cid, line in ctx.components.items()) or "（组件库是空的，不要插组件）"
    rules = "\n".join(f"- {r}" for r in ctx.rules) or "（无）"
    picked = "、".join(CHIP_LABEL[c] for c in body.chips) or "（没点）"
    where = f"（他是对小节 [{body.section_id}] 说的）" if body.section_id else ""
    return f"""你是一个个人学习 Wiki 的编辑。学习者读完一页后留下了反馈。请理解他真正想要什么，再给出修改计划。只依据他的原话，不要套模板。
先复述他的话（用他自己的词）；再把他说的拆成一个或几个方面（一条反馈可以同时说几件事），每个方面把「问题」和「改法」分开写：
 - 问题：对他来说哪里不对，用他自己的词说（quote 原样抄他的原话片段，一个字都不改；没有对得上的原话就空字符串）；
 - 改法：这一页 / K / 讲法具体怎么改，一句话。不要把改法写进问题里，也不要把问题当改法。
然后判断这条反馈走哪几条路，可以同时走几条：
 (1) 这一页：给哪几节补一段，或放一个交互组件；
 (2) K：他明确说自己不会哪些概念（missing）；
 (3) 讲法：这是不是一种以后所有页都该换的讲法（style）。

页面：{ctx.title}
页面的小节（[小节 id] 标题｜开头）：
{secs}
这一页和它所在学习路径的概念（名字（id））：{concepts}
系统目前对他的判断：
{chr(10).join(ctx.bands) or "（无）"}
这一页现在遵守的讲法规则：
{rules}
可用的交互组件（只能从这里选，不能编造；不合适就不选）：
{comps}
[可配置] 的组件是通用的：选了它，下一步会让你按它的 config 形状填一份讲这一页主题的 config（标题、问题和数据都写这一页的内容）；
[固定内容] 的组件只讲它自己那个主题。component_config 这里一律给 null。
{TIERS_RULE}
没有对得上的组件、又确实需要动手看的时候，可以在 block 里写一个块（组件和块最多给一个）。
{guide()}

{STANDARD}

⟦g:feedback⟧回应“没懂”“太快”这类反馈时，先判断是不是先修缺口：是，就把那个概念填进 missing（只补它）；不是，就把卡住的那一步拆细，add 写得短而具体，不要在原文后面追加大段、不要重复讲过的内容。选了组件时，component_why 写它解决的那个理解障碍（他卡在哪里、看见什么就懂了），并说明“怎么动、看什么”。⟦/g⟧

补进页面的 add 段落会被原样接在那一节正文后面，所以必须是写给学习者的正文，守上面的写法规则：先用人话说，新词先讲后用，术语放在例子之后，不用表格或箭头流程当主要解释，不出现“反馈”“你说”这类系统口吻。

他点的感觉：{picked}
他写的话{where}：「{body.text.strip() or "（没写）"}」

只回复一个 JSON 对象：
{{"reading": "用一句中文复述你理解的他的意思，要能对上他的原话（引用他用过的词）",
 "aspects": [{{"aspect": "这个方面是什么，几个字", "quote": "他的原话片段，原样照抄，或空字符串", "issue": "问题：对他来说哪里不对，用他的词", "action": "改法：怎么改，一句话"}}],
 "missing": ["他明确说自己不会的概念，只能填上面列出的概念 id，没有就空数组"],
 "page": {{"summary": "这一页会怎么改，一句给学习者看的话；提到小节时用小节标题，不写小节 id",
          "sections": [{{"section_id": "上面列出的小节 id", "add": "要补进这一节的一段中文正文，讲人话，直接回应他的话"}}],
          "component": "组件 id 或 null", "component_after": "插在哪个小节 id 后面，或 null",
          "component_config": null,
          "component_why": "为什么选这个组件、它帮他看见什么（它解决的理解障碍），一句话；不选组件就空字符串",
          "block": "块（按上面的类型之一），只在没有合适组件时给；否则 null",
          "block_why": "这个块解决他的哪个理解障碍，一句话；不给块就空字符串"}},
 "style": {{"kind": "问题类型，四个字以内", "before": "现在的讲法规则，一句话（尽量引用上面的规则），并写明它碰到上面自查清单的哪几条，例如（coherence）", "after": "以后的讲法规则，一句话，要直接回应他的原话（用他的词），不是泛泛的建议"}}}}
aspects 最多 {MAX_ASPECTS} 条，按他说的先后排；sections 最多 {MAX_ADDS} 条，只补和他的话有关的小节；不需要改就给空数组。"""


_NOISE = re.compile(r"[\s，。、；：！？,.;:!?“”\"'「」（）()…—-]+")


def restates(reading: str, text: str) -> bool:
    """A written feedback's reading must share at least one 2-character piece with the user's words
    (cheap guard against a reading that ignores what was said); nothing written → always true."""
    t = _NOISE.sub("", text)
    if len(t) < 2:
        return True
    r = _NOISE.sub("", reading)
    return any(t[i:i + 2] in r for i in range(len(t) - 1))


def _aspects(raw: Sequence[_Aspect], text: str, dropped: list[str]) -> list[FeedbackAspect]:
    """Keep an aspect with both an issue and an action; a non-empty quote must be the user's words
    verbatim (OWN_WORDS), else the aspect is dropped (never kept with an invented quote)."""
    kept: list[FeedbackAspect] = []
    for a in raw:
        name, quote = " ".join(a.aspect.split())[:40], a.quote.strip()
        issue, action = " ".join(a.issue.split()), " ".join(a.action.split())
        label = f"方面 {name or '（没写）'}"
        if not issue or not action:
            dropped.append(f"{label}（缺问题或改法）")
        elif quote and quote not in text:
            dropped.append(f"{label}（引用不是原话）")
        elif len(kept) >= MAX_ASPECTS:
            dropped.append(f"{label}（超过 {MAX_ASPECTS} 个方面）")
        else:
            kept.append(FeedbackAspect(aspect=name or issue[:20], quote=quote,
                                       issue=issue[:300], action=action[:300]))
    return kept


def validate(out: UnderstandOut, ctx: Context, body: PageFeedbackIn) -> Understood:
    reading = " ".join(out.reading.split())
    if not reading:
        raise NotUnderstood("没有给出理解")
    if not restates(reading, body.text):
        raise NotUnderstood("理解没有对上原话")
    dropped: list[str] = []
    aspects = _aspects(out.aspects, body.text, dropped)
    by_name = {n: c for c, n in ctx.concepts.items()}
    missing = []
    for m in out.missing:
        cid = m if m in ctx.concepts else by_name.get(m)
        if cid is None:
            dropped.append(f"概念 {m}")
        elif cid not in missing:
            missing.append(cid)
    known = {sid for sid, _, _ in ctx.sections}
    adds = []
    for s in out.page.sections:
        if s.section_id not in known:
            dropped.append(f"小节 {s.section_id}")
        elif s.add.strip() and len(adds) < MAX_ADDS:
            adds.append(SectionAddition(section_id=s.section_id, add=s.add.strip()[:1500]))
    comp = out.page.component or None
    config = None
    if comp is not None and comp not in ctx.components:
        dropped.append(f"组件 {comp}")
        comp = None
    if comp is not None and ctx.schemas.get(comp) is not None:
        try:
            config = validate_config(ctx.schemas[comp], out.page.component_config)
        except (ConfigInvalid, ValueError) as e:      # fail closed: no component without a config
            dropped.append(f"组件 {comp}（config 不合法：{e}）"[:300])
            comp = None
    block = compile_block(out.page.block) if out.page.block is not None else None
    if block is not None and comp is not None:          # tier 1 wins: never both
        dropped.append("EvoUI 块（同时给了组件，只用组件）")
        block = None
    if block is not None and (problems := validate_block(block)):
        dropped.append(f"EvoUI 块（不合法：{'；'.join(problems[:3])}）"[:300])
        block = None
    after = out.page.component_after if out.page.component_after in known else None
    try:
        summary = (out.page.summary.strip() or "；".join(a.action for a in aspects)
                   or "按你的反馈补了几处")
        plan = PagePlan(summary=summary[:200],
                        sections=tuple(adds), component=comp, component_after=after,
                        component_config=config,
                        component_why=out.page.component_why.strip()[:300] if comp else "",
                        block=block,
                        block_why=out.page.block_why.strip()[:300] if block else "")
        style = StylePlan(kind=out.style.kind.strip()[:20], before=out.style.before.strip()[:300],
                          after=out.style.after.strip()[:300])
        reading_ = FeedbackReading(reading=reading[:300], missing=tuple(missing[:8]), page=plan,
                                   style=style)
    except ValidationError as e:
        raise NotUnderstood(f"理解结果不完整：{e.errors()[0]['msg']}") from e
    return Understood(reading_, tuple(dropped), tuple(aspects))


def reply_schema(ctx: Context) -> type[UnderstandOut]:
    """The response schema of the reading: `UnderstandOut` with `page.component` an enum of the library's
    component ids. The config is NOT in it (always null here): the library's configs together are far over
    what Anthropic structured outputs take in one request (16 union-typed parameters), so a configurable
    component's config is a call of its own with that component's schema (`wikiapp.component_fill`)."""
    ids = sorted(ctx.components)
    comp_schema = {"anyOf": [{"type": "string", "enum": ids}, {"type": "null"}]} if ids else {"type": "null"}
    page = create_model(
        "_PageTyped", __base__=_Page,
        component=(Annotated[str | None, WithJsonSchema(comp_schema)], None),
        component_config=(Annotated[dict[str, Any] | None, WithJsonSchema({"type": "null"})], None))
    return create_model("UnderstandReply", __base__=UnderstandOut, page=(page, ...))


def _repairable(out: UnderstandOut, ctx: Context) -> list[str]:
    """What is wrong with the answer's block that a repair turn can fix (the schema cannot say it: the
    expression grammar)."""
    problems: list[str] = []
    comp = out.page.component
    if out.page.block is not None and comp is None:
        problems += [f"EvoUI 块：{x}" for x in validate_block(compile_block(out.page.block))[:MAX_PROBLEMS]]
    return problems


def understand(gw: CountingGateway, ctx: Context, body: PageFeedbackIn, *,
               subject: str) -> Understood:
    """The call, typed by `reply_schema`; a block or config the validators refuse goes back to the
    model with what is wrong, at most REPAIR_TURNS times (after that it is dropped and named in
    `dropped`, as before). A reply that does not fit the schema (a provider without structured
    outputs, after the gateway's own retries) is asked for again within the same turns.
    NotUnderstood on an unusable answer; LLM errors propagate (the caller fails closed on them too)."""
    schema = reply_schema(ctx)
    first = prompt(ctx, body)
    text = first
    for turn in range(1 + REPAIR_TURNS):
        req = LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=subject,
                         system=SYSTEM, prompt=text)
        try:
            out, call = gw.call("generator", req, schema)
        except LLMParseError:
            if turn == REPAIR_TURNS:
                raise
            text = f"{first}\n\n上一次的回答不是要求的 JSON 结构；严格按字段给出完整的 JSON（第 {turn + 1} 次）。"
            continue
        problems = _repairable(out, ctx)
        if not problems or turn == REPAIR_TURNS:
            return validate(_with_config(gw, out, ctx, first, subject), ctx, body)
        # asked again to repair it: a rejection on the step's run (plan 2026-10-29)
        reject("repair: " + "；".join(problems), call, turn=turn + 1)
        text = (f"{first}\n\n你上一次的回答：\n{json.dumps(out.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                "其中这些地方没法用：\n" + "\n".join(f"- {x}" for x in problems)
                + f"\n\n改好这些地方，重新给出完整的 JSON（第 {turn + 1} 次修改）。")
    raise AssertionError("unreachable")


def _with_config(gw: CountingGateway, out: UnderstandOut, ctx: Context, first: str,
                 subject: str) -> UnderstandOut:
    """`out` with the chosen configurable component's config filled by a call of its own (its schema, checked
    and repaired there); none usable → null, and `validate` drops the component saying why."""
    comp = out.page.component
    schema = ctx.schemas.get(comp) if comp is not None else None
    if comp is None or comp not in ctx.components or schema is None:
        return out
    context = (f"{first}\n\n你选了组件 {comp}，插在小节 [{out.page.component_after}] 后面。"
               f"它要帮他看见的：{out.page.component_why}"
               + (f"\n这一页的知识格子：{'、'.join(ctx.cells)}" if ctx.cells else ""))
    filled = fill_config(gw, comp, schema, ctx.components[comp], context, purpose=PURPOSE,
                         prompt_version=PROMPT_VERSION, subject=subject,
                         check=lambda config: cell_problems(config, ctx.cells))
    page = out.page.model_copy(update={"component_config": filled.config})
    return out.model_copy(update={"page": page})


def component_lines(components: Sequence) -> dict[str, str]:
    """Library components (`deep.contracts.LibraryComponent`) as prompt lines (the catalog line
    without the id: name, description, scenarios, params, [可配置] config shape / [固定内容])."""
    return {c.component_id: catalog_line(c).split("：", 1)[1] for c in components}


def component_schemas(components: Sequence) -> dict[str, dict[str, Any] | None]:
    return {c.component_id: c.config_schema for c in components}
