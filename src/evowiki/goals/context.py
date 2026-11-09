"""Token-budget context assembly (plan rev 3.2 §9.1 point 6). Shared by page generation
(organizer / writer) and the reading assistant. Pure and LLM-free: a conservative token estimate
with no tokenizer, a greedy packer that keeps required parts whole and trims the least important
items from the tail of each optional part, and short page digests built from recall cues."""

from collections.abc import Collection, Sequence

from pydantic import BaseModel, ConfigDict

from evowiki.goals.contracts import SectionRole
from evowiki.goals.fold import Page
from evowiki.tokens import estimate_tokens

PAGE_SUMMARY_MAX = 160
ELLIPSIS = "…"


class Part(BaseModel):
    """One named block of context. `items` run from most to least important; a non-required part
    is trimmed from the tail."""

    model_config = ConfigDict(frozen=True)

    name: str
    header: str = ""
    items: tuple[str, ...]
    required: bool = False


class Packed(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    header: str = ""
    text: str
    tokens: int
    kept: int
    dropped: int


class ContextOverBudget(ValueError):
    """The required parts alone exceed the budget (fail closed: the caller shortens them)."""


class ContextPack(BaseModel):
    model_config = ConfigDict(frozen=True)

    parts: tuple[Packed, ...]
    tokens: int
    budget: int

    def _get(self, name: str) -> Packed | None:
        return next((p for p in self.parts if p.name == name), None)

    def text(self, name: str) -> str:
        p = self._get(name)
        return p.text if p else ""

    def kept(self, name: str) -> int:
        p = self._get(name)
        return p.kept if p else 0

    def render(self) -> str:
        blocks = [f"{p.header}\n{p.text}" if p.header else p.text for p in self.parts if p.kept]
        return "\n\n".join(blocks)


def _item_cost(item: str) -> int:
    return estimate_tokens(item) + 1


def _packed(part: Part, n: int) -> Packed:
    items = part.items[:n]
    tokens = (estimate_tokens(part.header) + sum(_item_cost(i) for i in items)) if n else 0
    return Packed(name=part.name, header=part.header, text="\n".join(items), tokens=tokens,
                  kept=n, dropped=len(part.items) - n)


def assemble(parts: Sequence[Part], budget: int) -> ContextPack:
    """Place every required part whole, then fill the optional parts in order, item by item,
    stopping a part at the first item that does not fit (no skipping ahead, so the important
    items always win). A part's header is only paid for when at least one item is kept.
    Guarantees `pack.tokens <= budget`."""
    kept: dict[int, int] = {}
    used = 0
    for idx, part in enumerate(parts):
        if part.required:
            kept[idx] = len(part.items)
            used += _packed(part, kept[idx]).tokens
    if used > budget:
        raise ContextOverBudget(f"required context needs {used} tokens, budget is {budget}")
    for idx, part in enumerate(parts):
        if part.required:
            continue
        n = 0
        cost = estimate_tokens(part.header)
        for item in part.items:
            step = cost + _item_cost(item)
            if used + step > budget:
                break
            used += step
            cost = 0
            n += 1
        kept[idx] = n
    packed = tuple(_packed(part, kept[idx]) for idx, part in enumerate(parts))
    return ContextPack(parts=packed, tokens=sum(p.tokens for p in packed), budget=budget)


class PageDigest(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_id: str
    title: str
    concepts: tuple[str, ...] = ()
    summary: str = ""


def rank_pages(pages: Sequence[PageDigest], focus: Collection[str],
               prereqs: Collection[str] = (), *,
               exclude: Collection[str] = ()) -> list[PageDigest]:
    """Score = 2 x shared concepts + 1 x concepts that are prerequisites; highest first, stable.
    Zero-score pages stay (last) — the budget and the caller's top-k decide whether they fit."""
    focus_set, prereq_set, skip = set(focus), set(prereqs), set(exclude)

    def score(d: PageDigest) -> int:
        cs = set(d.concepts)
        return 2 * len(cs & focus_set) + len(cs & prereq_set)

    return sorted((d for d in pages if d.page_id not in skip), key=score, reverse=True)


def page_summary(cues: Sequence[str], max_chars: int = PAGE_SUMMARY_MAX) -> str:
    """Non-empty recall cues joined with 「；」, capped at `max_chars` (ending in 「…」 if cut)."""
    text = "；".join(c.strip() for c in cues if c.strip())
    if len(text) <= max_chars:
        return text
    return text[:max(max_chars - 1, 0)] + ELLIPSIS


def page_summary_of(page: Page) -> str:
    """Per written section in outline order: its recall cue, or its heading when it has none."""
    return page_summary([page.texts[s.id].cue.strip() or s.heading
                         for s in page.outline.sections
                         if s.id in page.texts and s.role is not SectionRole.CHECK])


def tail(text: str, max_chars: int) -> str:
    """The last `max_chars` characters, prefixed with 「…」 when anything was cut."""
    if len(text) <= max_chars:
        return text
    return ELLIPSIS + text[len(text) - max(max_chars, 0):]


def digest_line(d: PageDigest) -> str:
    """`- [title](#p:id)：概念 A、B；讲了：summary` (each clause only when non-empty)."""
    clauses = []
    if d.concepts:
        clauses.append("概念 " + "、".join(d.concepts))
    if d.summary:
        clauses.append("讲了：" + d.summary)
    line = f"- [{d.title}](#p:{d.page_id})"
    return f"{line}：{'；'.join(clauses)}" if clauses else line
