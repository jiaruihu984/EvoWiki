"""Phase 1/3 shared contracts (plan rev 3 / 3.1). Pure data and one Protocol — no IO, no LLM.

Lanes build against these types in parallel:
  K lane          implements `KView` (concept × aspect cells) and applies `Observation`s;
  organizer lane  reads a `KView`, emits a `PageOutline`, checks it, writes `SectionText`s;
  intake lane     emits `Observation`s and diagnostic `CheckItem`s.
Changing a type here is a cross-lane change: do it in its own commit and tell the other lanes."""

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Aspect(StrEnum):
    """Fixed aspect list of a concept (plan §8.B). LLM observations may only use these."""

    TERM = "term"              # 术语 / 定义
    PURPOSE = "purpose"        # 用途 / 何时用
    NOTATION = "notation"      # 公式 / 记号
    PROCEDURE = "procedure"    # 计算过程
    INTERPRET = "interpret"    # 解读结果
    MECHANISM = "mechanism"    # 原理 / 推导


def cell_id(concept_id: str, aspect: Aspect | str) -> str:
    return f"{concept_id}.{Aspect(aspect).value}"


def split_cell(cell: str) -> tuple[str, Aspect]:
    concept_id, _, aspect = cell.rpartition(".")
    if not concept_id:
        raise ValueError(f"not a cell id: {cell!r}")
    return concept_id, Aspect(aspect)


class Band(StrEnum):
    KNOWN = "known"      # 确定会
    LIKELY = "likely"    # 可能会
    SHAKY = "shaky"      # 模糊
    GAP = "gap"          # 不会
    NONE = "none"        # 没证据


class BandParams(BaseModel):
    """How (m, u, evidence) maps to a band. Values are the prototype's (artifact v5), which the user
    accepted ("就挺好"); they are θ_rep,u numeric parameters, clamped, tunable by RSI-B only within
    these bounds."""

    model_config = ConfigDict(frozen=True)

    # 2026-11-04: one right answer after he said 熟悉 / 掌握 is known; from nothing it takes two (m .533, then .650)
    known_min_m: float = Field(default=0.60, ge=0.6, le=0.95)
    known_max_u: float = Field(default=0.24, ge=0.05, le=0.3)
    likely_min_m: float = Field(default=0.50, ge=0.4, le=0.7)
    shaky_min_m: float = Field(default=0.28, ge=0.15, le=0.45)
    min_direct_evidence: float = Field(default=0.25, ge=0.0, le=1.0)
    inferred_likely_min: float = Field(default=0.50, ge=0.2, le=1.5)
    # Forgetting (plan rev 3.2 §9.3): a tested, undeclared cell whose recall probability is below
    # this drops from known/likely to shaky. Same value as the Tracer mastery bar.
    recall_min: float = Field(default=0.60, ge=0.3, le=0.9)

    @model_validator(mode="after")
    def _ordered(self) -> "BandParams":
        if not self.shaky_min_m <= self.likely_min_m <= self.known_min_m:
            raise ValueError("need shaky_min_m <= likely_min_m <= known_min_m")
        return self


Declaration = Literal["know", "dont"]


class CellBelief(BaseModel):
    """One concept × aspect cell. Direct and inferred pseudo-counts are kept apart (inferred
    evidence is revocable); a user declaration is kept apart from both."""

    model_config = ConfigDict(frozen=True)

    cell: str
    m: float = Field(ge=0.0, le=1.0)
    u: float = Field(ge=0.0, le=1.0)
    recall: float = Field(default=1.0, ge=0.0, le=1.0)
    direct: float = Field(default=0.0, ge=0.0)
    inferred_pos: float = Field(default=0.0, ge=0.0)
    inferred_neg: float = Field(default=0.0, ge=0.0)
    declared: Declaration | None = None
    band: Band


class KView(Protocol):
    """Read side of aspect K, as every consumer sees it."""

    def aspects(self, concept_id: str) -> tuple[Aspect, ...]: ...

    def cell(self, cell: str) -> CellBelief: ...

    def cells(self, concept_ids: tuple[str, ...]) -> dict[str, CellBelief]: ...

    def note(self, concept_id: str) -> tuple["NoteEntry", ...]: ...


class NoteEntry(BaseModel):
    """One line of a concept's natural-language note. Always cites its evidence event; never read
    for numbers (plan §8.B: the numbers stay authoritative)."""

    model_config = ConfigDict(frozen=True)

    entry_id: str
    cell: str
    text: str = Field(min_length=1, max_length=240)
    evidence_ref: str = Field(min_length=1)
    label: str | None = None        # display sentence (quiz/check: 题目：…；选了「…」); else None


Polarity = Literal["+", "-", "~"]          # knows / does not know / knows but fuzzy
ObservationKind = Literal["quiz", "check", "self", "feedback", "ask", "recall"]


class Observation(BaseModel):
    """What an LLM (or a deterministic grader) may report. Only a deterministic update turns it into
    numbers; `quote` must be the user's own words for kind="self"."""

    model_config = ConfigDict(frozen=True)

    cell: str
    polarity: Polarity
    strength: float = Field(ge=0.0, le=1.0)
    kind: ObservationKind
    evidence_ref: str = Field(min_length=1)
    quote: str | None = None

    @model_validator(mode="after")
    def _valid(self) -> "Observation":
        split_cell(self.cell)
        if self.kind == "self" and not self.quote:
            raise ValueError("a self-report observation must quote the user")
        return self


class CheckItem(BaseModel):
    """A single-answer question that measures exactly one cell. Gradable by structure (there is no
    separate verification call): a stem, 2–8 distinct options (as EvoUI quizzes; two equal options
    would be two right answers), the key in range. `verified`: the producer accepted it as gradable."""

    model_config = ConfigDict(frozen=True)

    cell: str
    stem: str = Field(min_length=1)
    options: tuple[str, ...] = Field(min_length=2, max_length=8)
    answer: int = Field(ge=0)
    verified: bool = False

    @model_validator(mode="after")
    def _key_in_range(self) -> "CheckItem":
        split_cell(self.cell)
        if self.answer >= len(self.options):
            raise ValueError("answer index out of range")
        if len({" ".join(o.split()).casefold() for o in self.options}) != len(self.options):
            raise ValueError("options repeat")
        return self


class SectionRole(StrEnum):
    ORGANIZER = "organizer"   # 开篇
    LINE = "line"             # 一行带过
    RECAP = "recap"           # 提醒卡
    EXPLAIN = "explain"       # 讲透
    COMPARE = "compare"       # 对比 / 类比
    CHECK = "check"           # 嵌入确认题


FOLDABLE_ROLES = frozenset({SectionRole.RECAP, SectionRole.EXPLAIN, SectionRole.COMPARE})


class OutlineSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    heading: str = Field(min_length=1)
    role: SectionRole
    cells: tuple[str, ...] = ()
    why: str = ""
    notes: str = ""
    analogy_sources: tuple[str, ...] = ()   # known cells, or "背景：<user's words>"
    needs_lookup: tuple[str, ...] = ()
    check: CheckItem | None = None

    @model_validator(mode="after")
    def _check_only_on_check(self) -> "OutlineSection":
        if (self.role is SectionRole.CHECK) != (self.check is not None):
            raise ValueError("a check item is required on, and only on, a check section")
        return self


class PageOutline(BaseModel):
    """The organizer's output for one page; lands in V (SET_GOAL_PAGE_PLAN)."""

    model_config = ConfigDict(frozen=True)

    page_id: str
    goal: str
    concepts: tuple[str, ...]
    targets: tuple[str, ...] = Field(min_length=1)
    profile: str = ""
    assumed: tuple[str, ...] = ()
    sections: tuple[OutlineSection, ...] = Field(min_length=1)
    source: Literal["organizer", "fallback"] = "organizer"

    @model_validator(mode="after")
    def _unique(self) -> "PageOutline":
        ids = [s.id for s in self.sections]
        if len(set(ids)) != len(ids):
            raise ValueError("section ids repeat")
        if not set(self.targets) <= set(self.concepts):
            raise ValueError("targets must be page concepts")
        return self


class SectionText(BaseModel):
    """Written text of one section. `cue` is the one-line recall cue shown when the section is folded
    (plan rev 3.1: K up → fold, never cut) and the question of its review card; `answer` is that
    card's answer, one or two sentences written with the cue ("": no card, the cue is only the fold
    title)."""

    model_config = ConfigDict(frozen=True)

    section_id: str
    cue: str = Field(default="", max_length=60)
    answer: str = Field(default="", max_length=200)
    body: str
    cache_key: str
    # a hand edit: the section's text exactly as the user typed it in his note (his own links, tags
    # and escapes). The note shows this; `body` is the same text as everything else reads it. Any
    # write of a new body drops it.
    note_body: str | None = None


class SectionState(BaseModel):
    """Per-section display state kept in V. `fold_locked`: the user folded/unfolded it by hand, so
    automatic folding leaves it alone. `pinned`: the section is locked (by 「锁定这一节」 or a hand
    edit): no system write changes its text and automatic folding leaves it alone."""

    model_config = ConfigDict(frozen=True)

    section_id: str
    folded: bool = False
    fold_locked: bool = False
    pinned: bool = False


ConceptRole = Literal["learn", "support", "reference"]
