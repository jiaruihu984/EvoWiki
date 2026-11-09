"""A page's outline as the writer plans it (LLM-call consolidation stage 4): the page inputs the
writer's first message carries when the page has no outline yet (K summary — band and evidence,
no m / u —, the cells `assumed` may hold, the cells a check must not ask, the related pages, the
user's principles; the outline and check rules are the write-page skill's outline and assessment
references, deep.skill), and `accept_draft`, the code checks the
writer's plan_page tool runs on its outline (they were the organizer's, whose LLM passes are gone).

Only an outline that cannot be used goes back to the model (`check_outline`: unrenderable or
ungradable), plus its misaligned / flawed check sections (`check_problems`). A check on a cell K
already settles (`settled_cells`) cannot change the page: it is taken out and reported, never sent
back. How the outline fits K (`diagnose_outline`) is returned as notes for the model's own review,
never a refusal (user ruling 2026-10-10). Kept check items get their options shuffled
(`diagnostic._shuffled`, deterministic) like every generated check."""

from dataclasses import dataclass

from evowiki.goals.context import (
    ContextPack,
    PageDigest,
    Part,
    assemble,
    digest_line,
    estimate_tokens,
    rank_pages,
)
from evowiki.goals.contracts import Band, KView, PageOutline, SectionRole, cell_id, split_cell
from evowiki.goals.diagnostic import _shuffled
from evowiki.goals.guardrails import (
    ASPECT_ZH,
    DIRECT_EVIDENCE_MIN,
    DraftOutline,
    PageSpec,
    Violation,
    cell_label,
    check_outline,
    diagnose_outline,
    drop_settled_checks,
    misaligned_checks,
    page_bands,
    settled_cells,
    to_outline,
)
from evowiki.goals.itemflaws import judge_check

PAGE_CONTEXT_BUDGET = 1500
TOP_K_PAGES = 8
CONCEPT_ROLE_ZH = {"learn": "在学", "support": "支撑", "reference": "参考"}

# Each band in the K summary (deep@v32): a short label; what each band means for the outline is
# said once, in the write-page skill's outline reference
BAND_LABEL = {
    Band.KNOWN: "确定会",
    Band.LIKELY: "可能会（有迹象，没确认）",
    Band.SHAKY: "学过但模糊",
    Band.GAP: "不会或没学过",
    Band.NONE: "没有证据",
}

# what `check_outline` refuses as LaTeX (`diagnostic.LATEX`), said up front (organizer@v13: a check
# stem with `^\\top` sent a whole outline back, 89 s on the 2026-10-21 live run). The plugin renders
# check stems, options and review cards as plain text (no MathJax there), so this stays.
PLAIN_CHECK = ("题干和选项是纯文本：公式照纯文本写（例如 x^2、(1, 1)、A 的转置写 Aᵀ），"
               "不要用 LaTeX（不要 $…$、\\(…\\)、\\frac、\\top、^{…}、_{…} 这类记号）")


def assumable_text(spec: PageSpec, bands: dict[str, Band]) -> str:
    """The cells `assumed` (默认你会) may hold: known and not a learning target (data, said once;
    `diagnose_outline` records an assumed cell outside them)."""
    cells = [c for c, b in bands.items() if b is Band.KNOWN and split_cell(c)[0] not in spec.targets]
    shown = "、".join(f"{c}（{cell_label(spec, c)}）" for c in cells)
    return "- 可以放进「默认你会」（assumed）的格子：" + (shown or "（没有，assumed 留空）")


def check_plan(spec: PageSpec, settled: frozenset[str]) -> str:
    """The cells a check must not go on (rule ASK): K already holds them as known on direct
    evidence, so the answer cannot change the page; `drop_settled_checks` takes such checks out."""
    shown = "、".join(f"{c}（{cell_label(spec, c)}）" for c in sorted(settled))
    return ("- 不要出确认题的格子（他已经有直接证据确定会，答案改变不了这页；出了会被去掉）："
            + (shown or "（没有）"))


def k_summary(spec: PageSpec, k: KView) -> str:
    lines: list[str] = []
    for c in spec.concepts:
        role = spec.roles.get(c.id)
        lines.append(f"- {c.id} {c.name}"
                     + (f"【在 Wiki 里：{CONCEPT_ROLE_ZH[role]}】" if role else "")
                     + (f"，前置：{'、'.join(c.prereqs)}" if c.prereqs else ""))
        notes = k.note(c.id)
        for a in k.aspects(c.id):
            x = cell_id(c.id, a)
            b = k.cell(x)
            sub = "、".join(c.subitems.get(a, ()))
            ev = "；".join([n.text for n in notes if n.cell == x][-2:])
            line = (f"  - {x} {ASPECT_ZH[a]}" + (f"（细项：{sub}）" if sub else "")
                    + f"：{BAND_LABEL[b.band]}")
            if b.declared:
                line += f"，用户声明{'会' if b.declared == 'know' else '不会'}"
            if b.direct < DIRECT_EVIDENCE_MIN and b.inferred_pos + b.inferred_neg > 0:
                line += "，仅推断"
            if ev:
                line += f"；证据：{ev}"
            elif b.direct < DIRECT_EVIDENCE_MIN and b.inferred_pos + b.inferred_neg == 0 and not b.declared:
                line += "；没有任何证据"
            lines.append(line)
    return "\n".join(lines)


def page_context(spec: PageSpec) -> ContextPack:
    """The user's principles (numbered) and the most related other pages (ranked by shared and
    prerequisite concepts, at most TOP_K_PAGES); the pages get PAGE_CONTEXT_BUDGET tokens."""
    digests = [PageDigest(page_id=p.page_id, title=p.title, concepts=p.concepts,
                          summary=p.summary) for p in spec.other_pages]
    prereqs = {x for c in spec.concepts for x in c.prereqs}
    ranked = rank_pages(digests, [c.id for c in spec.concepts], prereqs,
                        exclude={spec.page_id})[:TOP_K_PAGES]
    # required: principles_hash covers every principle, so none may be dropped. They are bounded by
    # the spec itself (30 items x 500 characters), so they always fit: the budget is theirs plus
    # PAGE_CONTEXT_BUDGET for the related pages (a full spec no longer fails every page).
    guidelines = Part(name="guidelines", required=True,
                      items=tuple(f"{i}. {g}" for i, g in enumerate(spec.guidelines, 1)))
    own = sum(estimate_tokens(g) + 1 for g in guidelines.items)
    return assemble([guidelines, Part(name="pages", items=tuple(digest_line(d) for d in ranked))],
                    own + PAGE_CONTEXT_BUDGET)


def other_pages_text(spec: PageSpec) -> str:
    return page_context(spec).text("pages") or "（无）"


def guidelines_text(spec: PageSpec) -> str:
    return page_context(spec).text("guidelines") or "（暂无）"


def flawed_checks(draft: DraftOutline) -> list[tuple[int, Violation]]:
    """Check sections with item-writing flaws (`itemflaws.judge_check`: `item_flaws`, or options
    that repeat), as (section index, violation with rule item_flaw). Any other malformed check is
    not judged here (`check_outline` is)."""
    out: list[tuple[int, Violation]] = []
    for i, s in enumerate(draft.sections):
        if s.role != SectionRole.CHECK.value or s.check is None:
            continue
        _, flaws = judge_check(s.check.cell, s.check.stem, s.check.options, s.check.answer)
        if flaws:
            out.append((i, Violation(rule="item_flaw", section=s.id or None,
                                     message=f"第 {i + 1} 段「{s.heading}」的确认题有出题缺陷："
                                             + "；".join(flaws))))
    return out


def untaught_checks(draft: DraftOutline, spec: PageSpec) -> list[tuple[int, Violation]]:
    """Check sections on a cell of a concept this page does not teach (deep@v32, skills design
    A7 #3): a page's checks collect evidence on what it teaches (`spec.targets`, the path's
    teaches); a prerequisite it only mentions gets no check. Sent back, like a misaligned one."""
    out: list[tuple[int, Violation]] = []
    for i, s in enumerate(draft.sections):
        if s.role != SectionRole.CHECK.value or s.check is None or not s.check.cell:
            continue
        if split_cell(s.check.cell)[0] not in spec.targets:     # a cell check_outline accepted
            out.append((i, Violation(rule="check_alignment", section=s.id or None,
                                     message=f"第 {i + 1} 段「{s.heading}」的确认题考的 {s.check.cell} "
                                             "不是这一页负责讲的知识点（只是用到的前置），不出确认题")))
    return out


def check_problems(draft: DraftOutline, spec: PageSpec) -> list[tuple[int, Violation]]:
    """Misaligned, untaught and flawed check sections of a usable draft, in section order."""
    return sorted([*misaligned_checks(draft), *untaught_checks(draft, spec), *flawed_checks(draft)],
                  key=lambda x: x[0])


def shuffle_checks(outline: PageOutline) -> PageOutline:
    secs = tuple(s.model_copy(update={"check": _shuffled(s.check)}) if s.check is not None else s
                 for s in outline.sections)
    return outline.model_copy(update={"sections": secs})


@dataclass(frozen=True)
class DraftChecked:
    """`accept_draft`'s verdict. `outline` is None when `problems` sent the draft back; `dropped`:
    the check sections on settled cells taken out (reported, never sent back); `notes`: how the
    outline departs from the cell plan (`diagnose_outline`), for the model's own review."""

    outline: PageOutline | None
    problems: tuple[Violation, ...] = ()
    dropped: tuple[Violation, ...] = ()
    notes: tuple[Violation, ...] = ()


def accept_draft(draft: DraftOutline, spec: PageSpec, k: KView) -> DraftChecked:
    """The outline the writer proposed, through the code checks (module doc). Pure: the same draft,
    spec and K give the same outline (check shuffles included)."""
    bands = page_bands(spec, k)
    draft, dropped = drop_settled_checks(draft, spec, settled_cells(spec, k))
    problems = check_outline(draft, spec, bands)
    if not problems:
        problems = [v for _, v in check_problems(draft, spec)]
    if problems:
        return DraftChecked(outline=None, problems=tuple(problems), dropped=tuple(dropped))
    return DraftChecked(outline=shuffle_checks(to_outline(draft, spec, bands)),
                        dropped=tuple(dropped), notes=tuple(diagnose_outline(draft, spec, bands)))
