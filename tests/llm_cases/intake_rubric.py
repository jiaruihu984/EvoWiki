"""Rubric judging for a goal decomposition (`goals.intake`), TutorBench method: a yes / no verdict
with a one-line reason per rubric item, all items in ONE judge call (purpose
`llm_cases.rubric_judge`, the same judge purpose and reply schema as tests/llm_cases/rubric.py).
Test-only harness (user ruling 2026-10-14: held-out goals that appear in no prompt, judged item by
item: the backbone chain, every part the refined goal names, a beginner's prerequisites, the refine
step did not narrow the scope).

Review 2026-10-14 (accepted): every case also gets a critical false-dependency item and a
non-critical leaf-quality item. Relation layers (intake@v20, user ruling 2026-10-14): intake writes
no prerequisite, only optional `recommended` edges, so `FALSE_DEPENDENCY` now reads 「recommended
关系两端都是具体知识点，没有把一整个领域写成另一个点的推荐前置」 (still critical; the old tools /
deployment extra item of the live cases is dropped) and a non-critical leaf-quality item (`LEAF`: every bottom node can be taught,
practised and asked about on its own) — leaf quality is judged, never checked in code.

`structure_text(res)`: the decomposition as the judge sees it — every node in learning order
(intake@v19+: the outline's depth-first order, as the prototype's judged runs saw it) with what it
contains, in learning order (part_of), what is recommended before it (「（建议先看：…）」) and,
for a prerequisite from elsewhere, what comes before it, by name (no ids).

Code metrics for the live report (no judge): `duplicate_rate(res)` and
`evidence_overreach(res, description)`."""

from dataclasses import dataclass

from evowiki.goals.intake import IntakeResult
from evowiki.llm.models import LLMRequest
from evowiki.plain_text import norm_name
from tests.llm_cases.rubric import (
    PROMPT_VERSION,
    PURPOSE,
    ItemResult,
    JudgeOut,
    RubricItem,
    RubricJudgeError,
)

SYSTEM = "你是严格的学习规划评审员。只输出要求的 JSON。"
FALSE_DEPENDENCY = "recommended 关系两端都是具体知识点，没有把一整个领域写成另一个点的推荐前置。"
LEAF = ("最底下的节点都能单独讲解、单独练习、单独出题；没有把一整个领域（例如一门学科）当成最底下的节点。")


@dataclass(frozen=True)
class DecompositionCase:
    case_id: str
    goal: str
    background: str
    backbone: str                     # the main line the goal's own content must cover
    prerequisites: tuple[str, ...]    # what a beginner needs before the goal (() = no item)
    extra: tuple[str, ...] = ()       # non-critical items (e.g. the core vs practice path)
    critical: tuple[str, ...] = ()    # case-specific critical items (e.g. a focused goal)

    def rubric(self, refined: str) -> tuple[RubricItem, ...]:
        crit = {"severity": "critical"}
        minor = {"severity": "non-critical"}
        pre = (RubricItem("一个完全零基础的学习者需要先会的前置没有缺：" + "、".join(self.prerequisites)
                          + " 每一项都有对应的节点（可以是同义的名字，或是一个包含它的更细的节点）。", crit),
               ) if self.prerequisites else ()
        return (
            RubricItem(f"主线完整：{self.backbone}这条主线上的每一步都有对应的节点（名字不必一样，意思对上即可）。",
                       crit),
            RubricItem(f"细化后的目标「{refined}」里明确说到的每一部分，都有对应的节点。", crit),
            *pre,
            RubricItem(f"细化后的目标没有缩小原目标「{self.goal}」的范围：没有限定成其中一部分、"
                       "某一类内容或某一种工具。只多了「基础」「入门」这类说法、内容仍覆盖这个领域的主要部分，"
                       "不算缩小范围（用户裁定 2026-10-14：「基础乐理」不算缩小「乐理」）。", crit),
            RubricItem(FALSE_DEPENDENCY, crit),
            *(RubricItem(text, crit) for text in self.critical),
            RubricItem(LEAF, minor),
            *(RubricItem(text, minor) for text in self.extra),
        )


def structure_text(res: IntakeResult) -> str:
    name = {c.id: c.name for c in res.concepts}
    parts: dict[str, list[str]] = {}
    before: dict[str, list[str]] = {}
    recommended: dict[str, list[str]] = {}
    for r in res.relations:
        if r.kind == "part_of":
            parts.setdefault(r.dst, []).append(name.get(r.src, r.src))
        elif r.kind == "prerequisite":
            before.setdefault(r.dst, []).append(name.get(r.src, r.src))
        elif r.kind == "recommended":
            recommended.setdefault(r.dst, []).append(name.get(r.src, r.src))
    lines = []
    for c in res.concepts:
        extra = []
        if parts.get(c.id):
            extra.append("包含，按学习顺序：" + "、".join(parts[c.id]))
        if before.get(c.id):
            extra.append("之前要会：" + "、".join(before[c.id]))
        mark = "（目标）" if c.target else ""
        rec = f"（建议先看：{'、'.join(recommended[c.id])}）" if recommended.get(c.id) else ""
        lines.append(f"- {c.name}{mark}" + (f"（{'；'.join(extra)}）" if extra else "") + rec)
    return "\n".join(lines) + "\n（顺序即学习顺序：同一层里排在前面的先学。）"


def judge_prompt(case: DecompositionCase, refined: str, structure: str,
                 items: tuple[RubricItem, ...]) -> str:
    rubric = "\n".join(f"{i}. {x.text}" for i, x in enumerate(items, 1))
    return f"""下面是一个学习目标被拆成知识图谱节点的结果。请按评分表逐条判断这个拆解是否满足每一条标准。
学习目标：「{case.goal}」
学习者的自我描述：「{case.background or '（空）'}」
细化后的目标：「{refined}」

拆解出的节点（缩进不代表层级，层级看「包含」）：
{structure}

评分表（共 {len(items)} 条，编号从 1 开始）：
{rubric}

判断规则：
- 每一条单独判断，只看这一条说的事。
- 只有拆解明确做到了这一条才算通过；缺了其中任何一项就不通过，理由里说出缺了什么。
- 理由用简体中文写，一句话。

输出格式：只回复 JSON {{"items": [{{"index": 1, "passed": true, "reason": "…"}}, …]}}，每条标准恰好一项，按编号顺序。"""


def judge(gateway, case: DecompositionCase, refined: str, structure: str
          ) -> list[ItemResult]:
    items = case.rubric(refined)
    req = LLMRequest(purpose=PURPOSE, prompt_version=PROMPT_VERSION, subject=case.case_id,
                     system=SYSTEM, prompt=judge_prompt(case, refined, structure, items))
    out, _ = gateway.call("verifier", req, JudgeOut)
    got = sorted(v.index for v in out.items)
    if got != list(range(1, len(items) + 1)):
        raise RubricJudgeError(f"case {case.case_id}: judge gave indices {got}")
    by = {v.index: v for v in out.items}
    return [ItemResult(item=x, passed=by[i].passed, reason=by[i].reason.strip())
            for i, x in enumerate(items, 1)]


def item_verdict(results: list[ItemResult], text: str) -> dict[str, object] | None:
    """The judge's verdict on one rubric item (e.g. FALSE_DEPENDENCY) for the report."""
    r = next((r for r in results if r.item.text == text), None)
    return None if r is None else {"passed": r.passed, "reason": r.reason}


def duplicate_rate(res: IntakeResult) -> float:
    """Share of nodes that repeat an earlier node's normalized name (NFKC, casefold, no
    whitespace): (nodes - distinct names) / nodes; 0.0 for no node."""
    names = [norm_name(c.name) for c in res.concepts]
    return 0.0 if not names else round((len(names) - len(set(names))) / len(names), 4)


def evidence_overreach(res: IntakeResult, description: str) -> dict[str, object]:
    """Observations on nodes the learner's words do not name (the node's normalized name is not in
    the normalized description). `observed`: nodes with an observation; `unnamed`: those the
    words do not name; `unnamed_whole`: unnamed nodes with every aspect cell observed (an
    area-level sentence on a whole node); `unnamed_per_part`: unnamed nodes that are a part
    (part_of child) of another node — evidence spread onto parts nobody named; `partial`:
    observed nodes with only some aspect cells observed; `rate`: unnamed / observed."""
    words = norm_name(description)
    by_id = {c.id: c for c in res.concepts}
    cells: dict[str, set[str]] = {}
    for o in res.observations:
        concept, _, aspect = o.cell.rpartition(".")
        cells.setdefault(concept, set()).add(aspect)
    parts = {r.src for r in res.relations if r.kind == "part_of"}
    observed = [c for c in cells if c in by_id]
    unnamed = [c for c in observed if norm_name(by_id[c].name) not in words]

    def whole(c: str) -> bool:
        return {a.value for a in by_id[c].aspects} <= cells[c]

    return {"observed": len(observed), "unnamed": len(unnamed),
            "unnamed_whole": sum(whole(c) for c in unnamed),
            "unnamed_per_part": sum(c in parts for c in unnamed),
            "partial": sum(not whole(c) for c in observed),
            "unnamed_names": [by_id[c].name for c in unnamed],
            "rate": round(len(unnamed) / len(observed), 4) if observed else 0.0}
