"""Outline guardrails (plan rev 3 Phase 1; the page writer's plan_page since stage 4, 底线 ①–⑤): deterministic checks on an outline draft.
They never arrange a page; they list violations, which go back to the LLM verbatim. Behaviour of
the accepted prototype (evowiki-mini-v5 `guardrails`):
  ① every target cell and every shaky / gap cell is covered (a section or `assumed`), and a
    shaky / gap cell sits in a recap, explain or compare section; `assumed` holds known cells only;
  ② analogy sources are known cells or his own words from the profile note ("背景：…");
  ③ a known cell is never explained in full (expertise reversal);
  ④ needs_lookup items are well-formed; the LLM lists version-dependent claims itself, the writer
    marks them ⟦待查证⟧;
  ⑤ at most `PageLimits` sections, recap cards and check questions.
Also `shape`: roles from the fixed list, cells of the page, a well-formed check item exactly on a
check section. `PageSpec` is the outline's page input (filled by intake later)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evowiki.goals.contracts import (
    FOLDABLE_ROLES,
    Aspect,
    Band,
    CheckItem,
    ConceptRole,
    KView,
    OutlineSection,
    PageOutline,
    SectionRole,
    cell_id,
    split_cell,
)
from evowiki.goals.diagnostic import LATEX_WHY, latex_in
from evowiki.goals.language import Language

WEAK = frozenset({Band.SHAKY, Band.GAP})
ASPECT_ZH = {Aspect.TERM: "术语 / 定义", Aspect.PURPOSE: "用途 / 何时用",
             Aspect.NOTATION: "公式 / 记号", Aspect.PROCEDURE: "计算过程",
             Aspect.INTERPRET: "解读结果", Aspect.MECHANISM: "原理 / 推导"}
BAND_ZH = {Band.KNOWN: "确定会", Band.LIKELY: "可能会", Band.SHAKY: "模糊", Band.GAP: "不会",
           Band.NONE: "没证据"}
# what the learner reads for a band (the plugin's format.ts BAND_WORD says the same)
BAND_WORD = {Band.KNOWN: "掌握", Band.LIKELY: "熟悉", Band.SHAKY: "熟悉", Band.GAP: "不会", Band.NONE: "还没学"}
ROLE_ZH = {SectionRole.ORGANIZER: "开篇", SectionRole.LINE: "一行带过",
           SectionRole.RECAP: "提醒卡", SectionRole.EXPLAIN: "讲透",
           SectionRole.COMPARE: "对比 / 类比", SectionRole.CHECK: "确认一下"}
BACKGROUND_PREFIX = "背景"
LOOKUP_MAX = 120
_ROLES = frozenset(r.value for r in SectionRole)
# deep@v32: roles no longer offered to the model; a reply (or a recorded script) that still names
# one is read as the plain section it now is (the schema lists only the offered roles)
RETIRED_ROLES = {"organizer": "line", "compare": "explain"}   # an opener was one or two lines
_TAUGHT = frozenset(r.value for r in FOLDABLE_ROLES)


class PageConcept(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    prereqs: tuple[str, ...] = ()
    subitems: dict[Aspect, tuple[str, ...]] = Field(default_factory=dict)
    # what the goal's split defined the concept as (the intake summary, the node's definition);
    # with its aspects in K it is the concept's scope (goals.scope.concept_scope)
    definition: str = ""


class OtherPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_id: str
    title: str
    concepts: tuple[str, ...] = ()
    summary: str = ""


class PageSpec(BaseModel):
    """What the organizer needs about one page besides K."""

    model_config = ConfigDict(frozen=True)

    page_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    concepts: tuple[PageConcept, ...] = Field(min_length=1)
    targets: tuple[str, ...] = Field(min_length=1)
    other_pages: tuple[OtherPage, ...] = ()
    guidelines: tuple[str, ...] = ()
    roles: dict[str, ConceptRole] = Field(default_factory=dict)
    language: Language = "zh"

    @model_validator(mode="after")
    def _targets_on_page(self) -> "PageSpec":
        ids = [c.id for c in self.concepts]
        if len(set(ids)) != len(ids):
            raise ValueError("page concepts repeat")
        if not set(self.targets) <= set(ids):
            raise ValueError("targets must be page concepts")
        return self

    def concept(self, cid: str) -> PageConcept:
        for c in self.concepts:
            if c.id == cid:
                return c
        raise KeyError(cid)


class PageLimits(BaseModel):
    """Size of the deterministic fallback outline only (`fallback._compact`). An LLM outline has no
    section / recap / check cap: its length follows the topic (user ruling 2026-10-10)."""

    model_config = ConfigDict(frozen=True)

    sections: int = Field(default=8, ge=3)
    recap: int = Field(default=3, ge=1)
    check: int = Field(default=2, ge=0)


DEFAULT_LIMITS = PageLimits()  # frozen, so a shared default is safe


class DraftCheck(BaseModel):
    cell: str = Field(default="", description=(
        "考的一个格子 id：这一页负责讲的知识点，并且在某个 explain 段的 cells 里"))
    stem: str = Field(default="", description="题干，纯文本")
    options: list[str] = Field(default_factory=list, description="2–8 个互不相同的选项，纯文本")
    answer: int = Field(default=0, description="正确选项的下标（从 0 开始）")


class DraftSection(BaseModel):
    id: str = Field(default="", description="段 id，如 s1；页内唯一")
    heading: str = Field(default="", description="小节标题，用主题自然的说法")
    # deep@v32: the model picks how much room a section gets, not a fixed page shape (organizer /
    # compare are gone from the choice; stored outlines keep them, SectionRole is unchanged)
    role: Literal["explain", "recap", "line", "check"] = Field(default="explain", description=(
        "explain：正常讲解（默认）；recap：学过但模糊的内容，简短回顾；line：确定会的，一句带过；"
        "check：确认题段"))
    cells: list[str] = Field(default_factory=list, description="这一段负责讲的 K 格子 id（只用这一页 K 里的）")

    @field_validator("role", mode="before")
    @classmethod
    def _retired(cls, v: object) -> object:
        return RETIRED_ROLES.get(v, v) if isinstance(v, str) else v
    why: str = Field(default="", description="这一段要回答的学习问题")
    notes: str = Field(default="", description="必要的讲解安排，例如例子、计算、回顾或交互")
    analogy_sources: list[str] = Field(default_factory=list, description=(
        "（可选）用来类比的、他确定会的格子 id，或「背景：」加他档案里的原话"))
    needs_lookup: list[str] = Field(default_factory=list, description=(
        "（可选）可能依赖版本或具体数字、需要查证的说法，每条一句"))
    check: DraftCheck | None = Field(default=None, description="只有 role=check 的段给")

    @model_validator(mode="after")
    def _check_cell(self) -> "DraftSection":
        """A question without `cell` asks about its section's one cell: the section already says it."""
        if self.check is not None and not self.check.cell and len(self.cells) == 1:
            self.check.cell = self.cells[0]
        return self


class DraftOutline(BaseModel):
    """The page outline the writer proposes (plan_page): lenient, so a malformed field is a
    violation the tool sends back, not a parse error."""

    profile: str = Field(default="", description="（可选）一句话说他的情况，显示在页面顶部，用第二人称「你」")
    assumed: list[str] = Field(default_factory=list, description="默认你会：只放用户消息里列出的可以默认会的格子 id")
    sections: list[DraftSection] = Field(default_factory=list, description="按讲解顺序排的段")


# check_alignment / item_flaw: a check section plan_page sends back (goals.outline_context.accept_draft)
# with its problems; settled_check: a check on a cell K already settles (taken out, reported)
RuleName = Literal["shape", "coverage", "analogy", "known_explained", "lookup", "check_alignment",
                   "item_flaw", "settled_check"]
# Direct evidence that counts as an answer on the cell (the organizer's K summary uses the same bar
# for 「仅推断」 / 「没有任何证据」)
DIRECT_EVIDENCE_MIN = 0.25


class Violation(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: RuleName
    section: str | None = None
    message: str


def page_bands(spec: PageSpec, k: KView) -> dict[str, Band]:
    return {x: k.cell(x).band for c in spec.concepts
            for x in (cell_id(c.id, a) for a in k.aspects(c.id))}


def cell_label(spec: PageSpec, cell: str) -> str:
    cid, aspect = split_cell(cell)
    name = next((c.name for c in spec.concepts if c.id == cid), cid)
    return f"{name}·{ASPECT_ZH[aspect]}"


def _label(spec: PageSpec, bands: dict[str, Band], x: str) -> str:
    return cell_label(spec, x) if x in bands else x


def _check_problem(chk: DraftCheck | None, bands: dict[str, Band]) -> str | None:
    """What is wrong with a check section's question, named so that the retry fixes that and not
    something else (live run 2026-11-04: a question without `cell`, told only that it was not
    「合格」, was rewritten three times, its stem and options changed, the cell still missing)."""
    if chk is None:
        return "没有 check 字段"
    if not chk.cell:
        return "check.cell 没写：写这道题考的一个格子 id"
    if chk.cell not in bands:
        return f"check.cell 的 {chk.cell} 不是这一页的格子"
    if not chk.stem.strip():
        return "题干是空的"
    if not 2 <= len(chk.options) <= 8:
        return f"选项要 2–8 个，现在是 {len(chk.options)} 个"
    if not 0 <= chk.answer < len(chk.options):
        return f"答案序号 {chk.answer} 不在选项范围内（从 0 开始）"
    if found := latex_in(chk.stem, *chk.options):
        return LATEX_WHY.format(found=found)
    return None


def _lookup(s: DraftSection, tag: str) -> list[Violation]:
    items = [x.strip() for x in s.needs_lookup]
    sid = s.id or None
    out: list[Violation] = []
    if any(not x or len(x) > LOOKUP_MAX for x in items):
        out.append(Violation(rule="lookup", section=sid,
                             message=f"{tag} 的 needs_lookup 条目要非空且不超过 {LOOKUP_MAX} 字"))
    if len(set(items)) != len(items):
        out.append(Violation(rule="lookup", section=sid, message=f"{tag} 的 needs_lookup 有重复条目"))
    return out


def check_outline(draft: DraftOutline, spec: PageSpec, bands: dict[str, Band]) -> list[Violation]:
    """What makes an outline unusable: it cannot be rendered or graded (no sections, an unknown
    role, a cell not on this page, a malformed check, a check on a non-check section, malformed
    needs_lookup). Only these send a draft back (user ruling 2026-10-10: no caps or coverage
    floors as refusal conditions — some topics have more to say, some less). How well the
    outline fits K is `diagnose_outline`, recorded, never refused."""
    if not draft.sections:
        return [Violation(rule="shape", message="没有任何段落")]
    out: list[Violation] = []
    for i, s in enumerate(draft.sections):
        tag, sid = f"第 {i + 1} 段「{s.heading}」", s.id or None
        if s.role not in _ROLES:
            out.append(Violation(rule="shape", section=sid,
                                 message=f"{tag} 的角色 {s.role} 不在允许列表里"))
        out += [Violation(rule="shape", section=sid, message=f"{tag} 引用了不存在的格子 {c}")
                for c in s.cells if c not in bands]
        if s.role == SectionRole.CHECK and (why := _check_problem(s.check, bands)):
            out.append(Violation(rule="shape", section=sid, message=f"{tag} 的确认题不合格：{why}"))
        if s.role != SectionRole.CHECK and s.check is not None:
            out.append(Violation(rule="shape", section=sid, message=f"{tag} 不是确认题却带了题目"))
        out += _lookup(s, tag)
    return out


def misaligned_checks(draft: DraftOutline) -> list[tuple[int, Violation]]:
    """Check sections whose `check.cell` is not in the cells of any explain or compare section of
    the same outline (spec 2026-10-11-llm-wheels-reuse 三: a page's confirm question asks about
    what the page explains), as (section index, violation with rule check_alignment). The
    organizer drops them; it never sends a draft back for this."""
    explained = {c for s in draft.sections
                 if s.role in (SectionRole.EXPLAIN.value, SectionRole.COMPARE.value) for c in s.cells}
    out: list[tuple[int, Violation]] = []
    for i, s in enumerate(draft.sections):
        if s.role != SectionRole.CHECK.value or s.check is None or s.check.cell in explained:
            continue
        out.append((i, Violation(rule="check_alignment", section=s.id or None,
                                 message=f"第 {i + 1} 段「{s.heading}」的确认题考的 {s.check.cell} "
                                         "不在任何 explain / compare 段里讲过")))
    return out


def diagnose_outline(draft: DraftOutline, spec: PageSpec, bands: dict[str, Band]) -> list[Violation]:
    """How the outline departs from the organizer's guidance (a known cell explained at length, an
    analogy from something not known, a non-known cell assumed, a weak or target cell left out or
    not taught). Diagnostics only: logged and shown in calibration, never a reason to refuse or
    retry (user ruling 2026-10-10)."""
    out: list[Violation] = []
    known = {c for c, b in bands.items() if b is Band.KNOWN}
    covered = set(draft.assumed)
    for i, s in enumerate(draft.sections):
        tag, sid = f"第 {i + 1} 段「{s.heading}」", s.id or None
        covered.update(c for c in s.cells if c in bands)
        if s.role == SectionRole.EXPLAIN:
            out += [Violation(rule="known_explained", section=sid,
                              message=f"{tag} 展开讲了 {cell_label(spec, c)}，但用户确定会它")
                    for c in s.cells if c in known]
        out += [Violation(rule="analogy", section=sid,
                          message=f"{tag} 用 {_label(spec, bands, x)} 做类比，但用户并不确定会它")
                for x in s.analogy_sources
                if not x.startswith(BACKGROUND_PREFIX) and x not in known]
    out += [Violation(rule="coverage", message=f"“默认你会”里的 {_label(spec, bands, a)} 并不是确定会的")
            for a in draft.assumed if a not in known]
    taught = {c for s in draft.sections if s.role in _TAUGHT for c in s.cells}
    for c, b in bands.items():
        weak = b in WEAK
        if (weak or split_cell(c)[0] in spec.targets) and c not in covered:
            out.append(Violation(rule="coverage",
                                 message=f"{cell_label(spec, c)}（{BAND_ZH[b]}）没有被任何段覆盖"))
        elif weak and c not in taught:
            out.append(Violation(rule="coverage",
                                 message=f"{cell_label(spec, c)} 是{BAND_ZH[b]}，只出现在一行带过 / 开篇 / "
                                         "确认题 / 默认你会里，没有被提醒或讲解"))
    return out


def settled_cells(spec: PageSpec, k: KView) -> frozenset[str]:
    """Cells K holds as known on direct evidence (an answer, not only a declaration or an
    inference): a check there cannot change how the page is written (rule ASK)."""
    return frozenset(x for x, b in page_bands(spec, k).items()
                     if b is Band.KNOWN and k.cell(x).direct >= DIRECT_EVIDENCE_MIN)


def drop_settled_checks(draft: DraftOutline, spec: PageSpec,
                        settled: frozenset[str]) -> tuple[DraftOutline, list[Violation]]:
    """An evidence filter, like the probe's `already_evidenced`: a check section on a settled cell
    is taken out of the draft (re-asking what the answers already show), and each one taken out is
    reported (`settled_check`), never kept silently."""
    keep: list[DraftSection] = []
    dropped: list[Violation] = []
    for i, s in enumerate(draft.sections):
        if s.role == SectionRole.CHECK and s.check is not None and s.check.cell in settled:
            dropped.append(Violation(
                rule="settled_check", section=s.id or None,
                message=f"第 {i + 1} 段「{s.heading}」的确认题测的是 {cell_label(spec, s.check.cell)}，"
                        "他已经有直接证据确定会，答案改变不了这页：已去掉"))
        else:
            keep.append(s)
    if not dropped:
        return draft, []
    return draft.model_copy(update={"sections": keep}), dropped


def to_outline(draft: DraftOutline, spec: PageSpec, bands: dict[str, Band], *,
               source: Literal["organizer", "fallback"] = "organizer") -> PageOutline:
    """A draft that passed `check_outline` → PageOutline. Ids are made unique (empty → s<n>,
    repeats get an "x" suffix), as in the prototype's `normalize`."""
    seen: set[str] = set()
    sections: list[OutlineSection] = []
    for i, s in enumerate(draft.sections):
        sid = s.id.strip() or f"s{i + 1}"
        while sid in seen:
            sid += "x"
        seen.add(sid)
        role = SectionRole(s.role)
        check = (CheckItem(cell=s.check.cell, stem=s.check.stem, options=tuple(s.check.options),
                           answer=s.check.answer)
                 if role is SectionRole.CHECK and s.check is not None else None)
        sections.append(OutlineSection(
            id=sid, heading=s.heading.strip() or f"第 {i + 1} 段", role=role,
            cells=tuple(c for c in s.cells if c in bands), why=s.why, notes=s.notes,
            analogy_sources=tuple(s.analogy_sources),
            needs_lookup=tuple(x.strip() for x in s.needs_lookup), check=check))
    return PageOutline(page_id=spec.page_id, goal=spec.goal,
                       concepts=tuple(c.id for c in spec.concepts), targets=spec.targets,
                       profile=draft.profile,
                       assumed=tuple(a for a in dict.fromkeys(draft.assumed) if a in bands),
                       sections=tuple(sections), source=source)


def draft_of(outline: PageOutline) -> DraftOutline:
    return DraftOutline(profile=outline.profile, assumed=list(outline.assumed), sections=[
        DraftSection(id=s.id, heading=s.heading, role=s.role.value, cells=list(s.cells), why=s.why,
                     notes=s.notes, analogy_sources=list(s.analogy_sources),
                     needs_lookup=list(s.needs_lookup),
                     check=(DraftCheck(cell=s.check.cell, stem=s.check.stem,
                                       options=list(s.check.options), answer=s.check.answer)
                            if s.check is not None else None))
        for s in outline.sections])


def section_of(outline: PageOutline, sid: str) -> OutlineSection:
    for s in outline.sections:
        if s.id == sid:
            return s
    raise KeyError(sid)
